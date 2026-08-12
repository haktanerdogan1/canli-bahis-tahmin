"""Flashscore'dan canli mac verisi (skor/dakika/istatistik) ceken scraping
yardimci fonksiyonlari. Bu dosya DOGRUDAN CALISTIRILMAZ - saf scraping
mantigini icerir, gercek dongu flashscore_xg_client.py'de (YEREL makinede
calisir) yasar.

NEDEN VAR / TARIHCE: Once sadece xG icin yazildi (TheSports'un Basic paketi
xG icermiyor - Advanced pakette, $1200/ay, sadece ust duzey ligler, bkz. Chris
Yao ile yazisma). 2026-08-12'de TheSports hesabi "yetkisiz" hatasi vermeye
basladi (abonelik/plan sorunu, kod tarafinda hicbir sey degismedi) ve TUM
canli veri kaynagi durdu - bu yuzden kapsam xG'den TUM canli mac senkronuna
(skor/dakika/durum + detayli istatistikler) genisletildi; artik thesports_bot.py
ile AYNI rolü Flashscore'dan dolduruyor (bkz. api.py: /api/admin/live-sync,
/api/admin/live-stats-update).

Flashscore'un mac detay sayfasi bu verileri CANLI macta bile gosteriyor ve
Mackolik'in aksine (Akamai WAF ile korunuyor) duz Playwright istegiyle
erisilebiliyor durumda.

IKI KATMANLI TASARIM (kapsam vs. hiz caniskisi):
  1) get_live_summary(): TEK sayfa yuklemesiyle TUM canli maclarin skor/
     dakika/lig/takim bilgisini verir - ucuz, her dongude calisir.
  2) _scrape_stats(): BIR macin detay/istatistik sayfasini ziyaret eder
     (~15-25sn/mac) - pahali, bu yuzden kucuk bir grup (BATCH_SIZE) uzerinde
     donen bir siraya sokulur, en uzun suredir taranmayan mac once islenir.
Sunucu tarafinda bu ikisi FARKLI uclara gider (bkz. api.py) ve farkli
kolonlari yazar - boylece biri digerinin verisini ezmez (ayni "UPDATE, sparse
INSERT degil" ilkesi, bkz. api.py docstring'leri).
"""
import re
import shutil
import difflib

BATCH_SIZE = 6
CYCLE_PAUSE_SECONDS = 15
MIN_MINUTE = 8
MAX_MINUTE = 88
MIN_MATCH_SCORE = 0.6  # takim adi fuzzy-eslesme esigi

LIVE_LIST_URL = "https://www.flashscore.com/football/"
MATCH_URL_TMPL = "https://www.flashscore.com/match/{mid}/#/match-summary/match-statistics/0"

# Flashscore'un istatistik kategori metnini (kucuk harfe cevrilmis, ICINDE
# ARANAN alt-dize) bizim alan adimiza esler. SIRA ONEMLI: "dangerous attack"
# "attack"tan ONCE kontrol edilmeli, yoksa "attack" alt-dizesi "dangerous
# attack" kategorisini de yanlislikla "attacks" olarak damgalar.
_STAT_CATEGORY_MAP = [
    ("expected goals", "xg"),
    ("ball possession", "possession"),
    ("shots on target", "shots_on_target"),
    ("shots off target", "shots_off_target"),
    ("total shots", "shots"),
    ("dangerous attack", "dangerous_attacks"),
    ("attack", "attacks"),
    ("corner", "corners"),
    ("red card", "red_cards"),
    ("big chance", "big_chances"),
]


def _normalize(name: str) -> str:
    name = name.lower()
    aski = {
        "ı": "i", "ş": "s", "ğ": "g", "ü": "u", "ö": "o", "ç": "c",
        "á": "a", "à": "a", "ä": "a", "â": "a",
        "é": "e", "è": "e", "ë": "e", "ê": "e",
        "í": "i", "ì": "i", "ï": "i", "î": "i",
        "ó": "o", "ò": "o", "ô": "o",
        "ú": "u", "ù": "u", "û": "u",
        "ñ": "n",
    }
    for a, b in aski.items():
        name = name.replace(a, b)
    for n in [" fc", "fc ", " sk", " ac", " cf", " sc", " if", " bk",
              " w", "(w)", " women", " ii", " u21", " u23", ".", "-"]:
        name = name.replace(n, " ")
    name = re.sub(r"[^a-z0-9\s]", "", name)
    return re.sub(r"\s+", " ", name).strip()


def _chromium_executable_path():
    """Railway/Nixpacks'ta Playwright'in kendi indirdigi Chromium yerine
    nixpkgs'in sagladigi sistem chromium'unu kullaniyoruz (nixpacks build
    sandbox'inda apt-get yok, playwright'in `install --with-deps` mekanizmasi
    calismaz). Yerelde (macOS gelistirme) bu bulunamaz, o zaman Playwright'in
    kendi indirdigi tarayiciya (varsayilan) dusulur."""
    for name in ("chromium", "chromium-browser"):
        path = shutil.which(name)
        if path:
            return path
    return None  # None -> playwright kendi varsayilan (indirilmis) tarayicisini kullanir


def get_live_summary(page):
    """Flashscore canli liste sayfasindan TUM canli maclarin ozet durumunu
    (lig, takimlar, logo, skor, dakika/durum metni) TEK sayfa yuklemesiyle
    ceker. Selector'lar dogrudan Playwright ile bu sayfa yerelde acilip DOM'u
    incelenerek DOGRULANDI (2026-08-12) - tahmin degil.

    DETAYLI ISTATISTIKLER (top hakimiyeti, sut, korner vb.) BURADA YOK -
    bunlar mac-basina ayri bir sayfa ziyareti gerektiriyor (bkz. _scrape_stats),
    cok daha yavas oldugu icin ayri/batch'li bir dongude cekiliyor.

    "stage" alani ya sadece rakamlardan olusan bir dakika metni ("47") ya da
    bir durum metni ("Half Time", "Penalties" vb.) - yorumlanmasi (minute'e
    cevirme, bilinmeyen metinleri loglama) SUNUCU tarafinda yapiliyor (bkz.
    api.py: _fs_parse_stage) cunku "bilinmeyen kod kesfi" deseni DB'ye
    yaziyor, istemcinin DB erisimi yok.
    """
    page.goto(LIVE_LIST_URL, timeout=30000)
    try:
        page.locator("#onetrust-accept-btn-handler").click(timeout=3000)
    except Exception:
        pass
    page.wait_for_timeout(5000)
    try:
        page.get_by_text("LIVE", exact=True).first.click(timeout=3000)
        page.wait_for_timeout(3000)
    except Exception:
        pass

    raw = page.evaluate("""
    () => {
        const results = [];
        let currentLeague = null;
        const walk = (node) => {
            if (!node || !node.children) return;
            for (const child of node.children) {
                if (child.matches && child.matches('[data-testid="wcl-headerLeague"]')) {
                    currentLeague = child.innerText.split(String.fromCharCode(10))[0];
                } else if (child.id && child.id.indexOf('g_1_') === 0) {
                    const homeEl = child.querySelector('[data-testid="wcl-tableScore"][data-side="1"]');
                    const awayEl = child.querySelector('[data-testid="wcl-tableScore"][data-side="2"]');
                    const stage = child.querySelector('.event__stage--block');
                    const parts = child.querySelectorAll('[data-testid="wcl-matchRow-participant"]');
                    const homeName = parts[0] ? parts[0].querySelector('[data-testid="wcl-scores-simple-text-01"]') : null;
                    const awayName = parts[1] ? parts[1].querySelector('[data-testid="wcl-scores-simple-text-01"]') : null;
                    const homeLogo = parts[0] ? parts[0].querySelector('img') : null;
                    const awayLogo = parts[1] ? parts[1].querySelector('img') : null;
                    results.push({
                        league: currentLeague,
                        home: homeName ? homeName.innerText.trim() : null,
                        away: awayName ? awayName.innerText.trim() : null,
                        home_logo: homeLogo ? homeLogo.src : '',
                        away_logo: awayLogo ? awayLogo.src : '',
                        score_h: homeEl ? homeEl.innerText : null,
                        score_a: awayEl ? awayEl.innerText : null,
                        stage: stage ? stage.innerText.trim() : null,
                        mid: child.id.replace('g_1_', '')
                    });
                } else {
                    walk(child);
                }
            }
        };
        walk(document.body);
        return results;
    }
    """)

    def _score_int(v):
        try:
            return int(str(v).split("\n")[0].strip())
        except Exception:
            return 0

    out = []
    for row in raw:
        if not row.get("home") or not row.get("away") or not row.get("mid"):
            continue
        out.append({
            "mid": row["mid"], "home": row["home"], "away": row["away"],
            "league": row.get("league") or "Unknown League",
            "home_logo": row.get("home_logo") or "", "away_logo": row.get("away_logo") or "",
            "score_h": _score_int(row.get("score_h")), "score_a": _score_int(row.get("score_a")),
            "stage": row.get("stage") or "",
        })
    return out


def _find_match(home_team, away_team, live_matches):
    target_home, target_away = _normalize(home_team), _normalize(away_team)
    best, best_score = None, 0.0
    for m in live_matches:
        h, a = _normalize(m["home"]), _normalize(m["away"])
        straight = (difflib.SequenceMatcher(None, target_home, h).ratio()
                    + difflib.SequenceMatcher(None, target_away, a).ratio()) / 2
        # kaynaklar arasi ev/deplasman sirasi bazen ters olabilir; iki yonu de dene
        swapped = (difflib.SequenceMatcher(None, target_home, a).ratio()
                   + difflib.SequenceMatcher(None, target_away, h).ratio()) / 2
        combined = max(straight, swapped)
        if combined > best_score:
            best_score, best = combined, m
    if best and best_score >= MIN_MATCH_SCORE:
        return best, best_score
    return None, best_score


def _scrape_stats(page, mid):
    """Bir macin istatistik sayfasindaki TUM kategorileri ceker (sadece xG
    degil). Flashscore'un istatistik widget'i lig/maca gore DEGISKEN sayida
    kategori gosteriyor - yerelde canli maclar uzerinde dogrulandi (2026-08-12):
    bazi maclarda sadece 5 kategori (xG/top hakimiyeti/toplam sut/buyuk sans/
    ceza sahasi dokunuslari) var, bazilarinda korner/kart/atak HIC YOK.
    GORULMEYEN bir kategori icin deger UYDURMUYORUZ - sozlukte o alan hic
    olmuyor, sunucu (api.py: /api/admin/live-stats-update) sadece GELEN
    alanlari UPDATE ediyor, gerisini oldugu gibi birakiyor."""
    page.goto(MATCH_URL_TMPL.format(mid=mid), timeout=30000)
    try:
        page.locator("#onetrust-accept-btn-handler").click(timeout=3000)
    except Exception:
        pass
    page.wait_for_timeout(3000)
    try:
        page.get_by_text("Statistics", exact=False).first.click(timeout=3000)
    except Exception:
        pass
    try:
        page.wait_for_selector('[data-testid="wcl-statistics"]', timeout=10000)
    except Exception:
        return {}

    out = {}
    for row in page.locator('[data-testid="wcl-statistics"]').all():
        try:
            category = row.locator('[data-testid="wcl-statistics-category"]').inner_text().strip().lower()
        except Exception:
            continue
        field = None
        for needle, name in _STAT_CATEGORY_MAP:
            if needle in category:
                field = name
                break
        if not field or field in out:
            continue
        values = row.locator('[data-testid="wcl-statistics-value"]')
        try:
            v0 = values.nth(0).inner_text().strip().replace("%", "")
            v1 = values.nth(1).inner_text().strip().replace("%", "")
            out[field] = [float(v0), float(v1)] if field == "xg" else [int(v0), int(v1)]
        except Exception:
            continue
    return out
