"""Flashscore'dan canli mac xG (Expected Goals) verisini cekip live_snapshots'a yazan bot.

NEDEN VAR: TheSports'un Basic paketi xG icermiyor (Advanced pakette, $1200/ay,
sadece ust duzey ligler - bkz. Chris Yao ile yazisma). Flashscore'un mac
detay sayfasi xG'yi CANLI macta bile gosteriyor ve Mackolik'in aksine (Akamai
WAF ile korunuyor) duz Playwright istegiyle erisilebiliyor durumda.

MIMARI KARAR: live_snapshots tablosuna YENI SATIR eklemiyoruz - orchestrator.py
"en son satir = guncel durum" varsayimiyla calisiyor (ORDER BY id DESC LIMIT 1),
sparse bir satir eklemek diger TUM botlarin (sut, korner, hakimiyet vb.) o
dongude veri kaybetmesine yol acar. Bunun yerine thesports_bot'un zaten yazdigi
EN SON satirin home_xg/away_xg kolonlarini UPDATE ediyoruz.

MALIYET/KAPSAM KARARI: Playwright ile gercek bir Chromium acmak agir (~15-25sn/
mac). Tum canli maclari her dongude taramak imkansiz - bunun yerine kucuk bir
grup (BATCH_SIZE) uzerinde donen bir siraya sokuyoruz, en uzun suredir
taranmayan maclar once islenir. Boylece zamanla tum canli maclar kapsanir.

BELLEK KARARI: Ayni Railway servisinde (web) baska 3 surec daha calisiyor,
toplam bellek limiti 1GB. Chromium'u dongu basina bir kez acip dongu sonunda
TAMAMEN kapatiyoruz (surekli acik birakmiyoruz) - bellek kullanimini gecici
tutmak icin bilincli tercih.
"""
import os
import re
import time
import shutil
import difflib

from playwright.sync_api import sync_playwright

from db_config import connect

BATCH_SIZE = 6
CYCLE_PAUSE_SECONDS = 15
MIN_MINUTE = 8
MAX_MINUTE = 88
MIN_MATCH_SCORE = 0.6  # takim adi fuzzy-eslesme esigi

LIVE_LIST_URL = "https://www.flashscore.com/football/"
MATCH_URL_TMPL = "https://www.flashscore.com/match/{mid}/#/match-summary/match-statistics/0"

# match_id (bizim DB) -> son taranma zamani (unix). Surec yeniden baslarsa
# sifirlanir - bu kabul edilebilir, thesports_bot'taki onbellekler de ayni sekilde davraniyor.
_last_scraped_at = {}


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


def _get_live_matches(page):
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
        const rows = document.querySelectorAll("[id^='g_1_']");
        return Array.from(rows).map(r => {
            const a = r.querySelector('a.eventRowLink');
            return { mid: r.id.replace('g_1_', ''), label: a ? a.getAttribute('aria-label') : null };
        });
    }
    """)
    out = []
    for row in raw:
        if not row["label"] or " - " not in row["label"]:
            continue
        home, away = row["label"].split(" - ", 1)
        out.append({"mid": row["mid"], "home": home.strip(), "away": away.strip()})
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


def _scrape_xg(page, mid):
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
        return None

    for row in page.locator('[data-testid="wcl-statistics"]').all():
        category = row.locator('[data-testid="wcl-statistics-category"]').inner_text().strip()
        if "Expected goals" in category or "Expected Goals" in category:
            values = row.locator('[data-testid="wcl-statistics-value"]')
            try:
                return float(values.nth(0).inner_text().strip()), float(values.nth(1).inner_text().strip())
            except ValueError:
                return None
    return None


def _hedef_maclari_getir(cursor):
    cursor.execute('''
        SELECT m.id, m.home_team_id, m.away_team_id, m.minute
        FROM matches m
        WHERE m.status = 'LIVE'
          AND m.minute BETWEEN ? AND ?
          AND EXISTS (SELECT 1 FROM live_snapshots s WHERE s.match_id = m.id)
    ''', (MIN_MINUTE, MAX_MINUTE))
    rows = cursor.fetchall()
    # en uzun suredir taranmayan (hic taranmayan dahil, oncelik 0) once gelsin
    rows.sort(key=lambda r: _last_scraped_at.get(r[0], 0))
    return rows[:BATCH_SIZE]


def _xg_yaz(cursor, match_db_id, home_xg, away_xg):
    cursor.execute('''
        UPDATE live_snapshots SET home_xg = ?, away_xg = ?
        WHERE id = (SELECT id FROM live_snapshots WHERE match_id = ? ORDER BY id DESC LIMIT 1)
    ''', (home_xg, away_xg, match_db_id))


def run_cycle():
    conn = connect()
    cursor = conn.cursor()
    hedefler = _hedef_maclari_getir(cursor)
    conn.close()

    if not hedefler:
        return 0, 0

    islenen = 0
    bulunan = 0
    exe_path = _chromium_executable_path()

    with sync_playwright() as p:
        launch_kwargs = {
            "headless": True,
            # Railway container'inda /dev/shm cok kucuk ve sandbox user-namespace
            # izinleri yok - bunlar olmadan Chromium "Target/Page crashed" ile
            # oluyor (yerelde macOS'ta bu sinirlar olmadigi icin sorun cikmadi).
            "args": ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        }
        if exe_path:
            launch_kwargs["executable_path"] = exe_path
        browser = p.chromium.launch(**launch_kwargs)
        page = browser.new_page()

        try:
            live_matches = _get_live_matches(page)
        except Exception as e:
            print(f"⚠️  Flashscore canli liste cekilemedi: {e}", flush=True)
            browser.close()
            return 0, 0

        conn = connect()
        cursor = conn.cursor()

        for match_db_id, home_team, away_team, minute in hedefler:
            _last_scraped_at[match_db_id] = time.time()
            islenen += 1
            if not home_team or not away_team:
                continue

            found, score = _find_match(home_team, away_team, live_matches)
            if not found:
                continue

            try:
                xg = _scrape_xg(page, found["mid"])
            except Exception as e:
                print(f"⚠️  {home_team} - {away_team} xG cekilemedi: {e}", flush=True)
                continue

            if xg is None:
                continue

            home_xg, away_xg = xg
            _xg_yaz(cursor, match_db_id, home_xg, away_xg)
            bulunan += 1
            print(f"✅ xG: {home_team} {home_xg} - {away_xg} {away_team} "
                  f"(flashscore eslesme skoru={score:.2f})", flush=True)

        conn.commit()
        conn.close()
        browser.close()

    return islenen, bulunan


def main():
    print("🚀 Starting Flashscore xG Bot...", flush=True)
    while True:
        start = time.time()
        try:
            islenen, bulunan = run_cycle()
        except Exception as e:
            print(f"⚠️  Flashscore xG dongu hatasi: {e}", flush=True)
            islenen, bulunan = 0, 0
        elapsed = time.time() - start
        print(f"📊 Flashscore xG dongusu: {islenen} mac denendi, {bulunan} xG bulundu "
              f"({elapsed:.1f}sn). {CYCLE_PAUSE_SECONDS}sn bekleniyor...", flush=True)
        time.sleep(CYCLE_PAUSE_SECONDS)


if __name__ == "__main__":
    main()
