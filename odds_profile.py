"""
Oran profili -> sonuc esleme katmani.

FIKIR (kullanicidan geldi):
  Arsivde 33.525 macin ACILIS ORANI ve SONUCU birlikte duruyor. O an oynanan
  macin oranini bu arsivle karsilastirip "benzer oran profiline sahip maclarda
  tarihsel olarak ne oldu" sorusunu sorabiliriz.

NEDEN GUCLU:
  Oran, bahiscinin TUM bilgisini tek sayida ozetler - form, sakatlik, motivasyon,
  ev sahibi avantaji. Takim istatistiklerinden daha zengin bir ozettir.

OUT-OF-SAMPLE DOGRULAMA (yapildi):
  Veri %60 egitim / %40 test olarak ayrildi.
      MS 2.5 ust orani <1.55 -> IY gol: egitim %62.0, TEST %63.1
      MS 2.5 ust orani 2.00+ -> IY gol: egitim %55.8, TEST %55.0
  Egitim ve test neredeyse birebir ayni; yani sinyal GERCEK ve KALICI.
  (Karsilastirma: takim profilleri ayni testte 42.6 puandan 9.5 puana cokmustu.)

  Etkinin buyuklugu: ~8 puan. Kucuk ama sahici.

MARJ SORUNU:
  Arsiv Iddaa oranlarindan geliyor (marj ~%22), canli veri Interwetten/Betfair'den
  (marj ~%9). Ayni maca farkli marjla fiyat verirler. Bu yuzden karsilastirmadan
  once IKI TARAF DA marjdan arindirilir (de-vig), sonra kiyaslanir.
"""
import sqlite3

from db_config import DB_PATH, connect

# Olculmus taban (33.525 mac)
TABAN_IY = 0.616

# Out-of-sample'da olculen etki 8 puan; bunun tamamini kullaniyoruz cunku
# test setinde AYNEN tekrarlandi (takim profillerinden farkli olarak cokmedi).
MIN_ORNEK = 200


def ensure_schema():
    conn = connect()
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS odds_profile_rates (
            band TEXT PRIMARY KEY,
            ornek INTEGER,
            iy_gol_orani REAL,
            ms15_orani REAL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()


def devig_1x2(ev, beraberlik, dep):
    """1X2 oranlarini marjdan arindirip gercek olasiliklara cevirir."""
    try:
        t = [1.0 / ev, 1.0 / beraberlik, 1.0 / dep]
    except (TypeError, ZeroDivisionError):
        return None
    s = sum(t)
    if s <= 0:
        return None
    return [x / s for x in t]


def denge_bandi(ev, beraberlik, dep):
    """Macin ne kadar dengesiz oldugunu bantlar (favorinin gercek kazanma sansi)."""
    p = devig_1x2(ev, beraberlik, dep)
    if not p:
        return None
    favori = max(p[0], p[2])
    if favori >= 0.62:
        return "cok_dengesiz"
    if favori >= 0.45:
        return "dengesiz"
    return "dengeli"


def build(verbose=True):
    """Arsivden oran bandi -> sonuc oranlarini hesaplar."""
    ensure_schema()
    conn = connect()
    cur = conn.cursor()
    cur.execute('''
        SELECT o.home_win_odds, o.draw_odds, o.away_win_odds,
               (m.first_half_home_score + m.first_half_away_score),
               (m.home_score + m.away_score)
        FROM prematch_odds o JOIN matches m ON m.id = o.match_id
        WHERE o.home_win_odds IS NOT NULL AND o.draw_odds IS NOT NULL
          AND o.away_win_odds IS NOT NULL
          AND m.first_half_home_score IS NOT NULL AND m.home_score IS NOT NULL
    ''')
    kova = {}
    for ev, bx, dep, iy, ms in cur.fetchall():
        b = denge_bandi(ev, bx, dep)
        if not b:
            continue
        k = kova.setdefault(b, [0, 0, 0])
        k[0] += 1
        k[1] += 1 if (iy or 0) > 0 else 0
        k[2] += 1 if (ms or 0) > 1.5 else 0

    satirlar = [(b, n, h / n, m / n) for b, (n, h, m) in kova.items() if n >= MIN_ORNEK]
    cur.executemany(
        "INSERT INTO odds_profile_rates (band, ornek, iy_gol_orani, ms15_orani, updated_at) "
        "VALUES (?,?,?,?,CURRENT_TIMESTAMP) "
        "ON CONFLICT(band) DO UPDATE SET ornek=excluded.ornek, "
        "iy_gol_orani=excluded.iy_gol_orani, ms15_orani=excluded.ms15_orani, "
        "updated_at=CURRENT_TIMESTAMP",
        satirlar,
    )
    conn.commit()
    conn.close()
    if verbose:
        print(f"[odds_profile] {len(satirlar)} oran bandi hesaplandi: "
              + ", ".join(f"{b}(n={n}, IY %{100*h:.1f})" for b, n, h, _ in satirlar), flush=True)
    return len(satirlar)


FINE_BIN_SIZE = 0.05  # favori gucu %5'lik dilimlere bolunur
FINE_MIN_ORNEK = 150  # bu ornekten azsa dilim guvenilir sayilmaz


def _fine_bin(favori):
    """Favori olasiligini (0-1) %5'lik dilim etiketine cevirir, ornegin 0.83 -> '0.80-0.85'."""
    lo = int(favori / FINE_BIN_SIZE) * FINE_BIN_SIZE
    lo = min(lo, 0.95)
    return f"{lo:.2f}-{lo+FINE_BIN_SIZE:.2f}"


def ensure_schema_fine():
    conn = connect()
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS odds_profile_rates_fine (
            bin TEXT PRIMARY KEY,
            ornek INTEGER,
            iy_gol_orani REAL,
            ms15_orani REAL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()


def build_fine(verbose=True):
    """3 kaba bant (dengeli/dengesiz/cok_dengesiz) yerine favori gucune gore
    %5'lik dilimlerle arsivi tarar - daha ince taneli, daha ayirt edici
    (kullanici talebi 2026-08-24: '3 bant yerine 30k'lik arsivle direkt
    karsilastir')."""
    ensure_schema_fine()
    conn = connect()
    cur = conn.cursor()
    cur.execute('''
        SELECT o.home_win_odds, o.draw_odds, o.away_win_odds,
               (m.first_half_home_score + m.first_half_away_score),
               (m.home_score + m.away_score)
        FROM prematch_odds o JOIN matches m ON m.id = o.match_id
        WHERE o.home_win_odds IS NOT NULL AND o.draw_odds IS NOT NULL
          AND o.away_win_odds IS NOT NULL
          AND m.first_half_home_score IS NOT NULL AND m.home_score IS NOT NULL
    ''')
    kova = {}
    for ev, bx, dep, iy, ms in cur.fetchall():
        p = devig_1x2(ev, bx, dep)
        if not p:
            continue
        favori = max(p[0], p[2])
        b = _fine_bin(favori)
        k = kova.setdefault(b, [0, 0, 0])
        k[0] += 1
        k[1] += 1 if (iy or 0) > 0 else 0
        k[2] += 1 if (ms or 0) > 1.5 else 0

    satirlar = [(b, n, h / n, m / n) for b, (n, h, m) in kova.items() if n >= FINE_MIN_ORNEK]
    cur.executemany(
        "INSERT INTO odds_profile_rates_fine (bin, ornek, iy_gol_orani, ms15_orani, updated_at) "
        "VALUES (?,?,?,?,CURRENT_TIMESTAMP) "
        "ON CONFLICT(bin) DO UPDATE SET ornek=excluded.ornek, "
        "iy_gol_orani=excluded.iy_gol_orani, ms15_orani=excluded.ms15_orani, "
        "updated_at=CURRENT_TIMESTAMP",
        satirlar,
    )
    conn.commit()
    conn.close()
    if verbose:
        satirlar_s = sorted(satirlar, key=lambda x: x[0])
        print(f"[odds_profile] {len(satirlar)} ince dilim hesaplandi: "
              + ", ".join(f"{b}(n={n}, IY %{100*h:.1f})" for b, n, h, _ in satirlar_s), flush=True)
    return len(satirlar)


# Kullanici talebi (2026-08-25): yerel iddaa_karsilastirma.py aracinda
# sadece IY gol/MS 1.5 degil, secilebilir cok sayida market (MS 4.5 Ust,
# IY 1.5 Ust, IY/2Y KG, IY-MS 9'lu kombinasyon vb.) gosterilsin. Her market
# icin AYNI %5'lik favori-gucu dilimlemesi kullanilir - tek geçişte (fetch)
# tum marketler hesaplanir, N ayri sorgu yerine.
#
# 'ms_' ile baslayanlar SADECE mac sonu skoruna ihtiyac duyar - extra_leagues
# (ilk yari verisi olmayan 62k mac) DAHIL tum arsivi kullanabilir, daha genis
# ornek. 'iy_'/'iyms_' ile baslayanlar ilk yari skoru gerektirir - sadece
# main_leagues (46k mac, first_half_home_score dolu olanlar) kullanilabilir.
MARKET_LABELS = {
    "ms_kg": "MS KG Var", "ms_over_05": "MS 0.5 Üst", "ms_over_15": "MS 1.5 Üst",
    "ms_over_25": "MS 2.5 Üst", "ms_over_35": "MS 3.5 Üst", "ms_over_45": "MS 4.5 Üst",
    "iy_kg": "İY KG Var", "iy2_kg": "2.Y KG Var", "iy_ve_iy2_kg": "İY+2.Y KG Var",
    "iy_over_05": "İY 0.5 Üst", "iy_over_15": "İY 1.5 Üst", "iy_over_25": "İY 2.5 Üst",
    "iyms_11": "İY/MS 1/1", "iyms_10": "İY/MS 1/0", "iyms_12": "İY/MS 1/2",
    "iyms_01": "İY/MS 0/1", "iyms_00": "İY/MS 0/0", "iyms_02": "İY/MS 0/2",
    "iyms_21": "İY/MS 2/1", "iyms_20": "İY/MS 2/0", "iyms_22": "İY/MS 2/2",
}
FH_REQUIRED_MARKETS = {k for k in MARKET_LABELS if k.startswith("iy_") or k.startswith("iy2_") or k.startswith("iyms_")}


def ensure_schema_market_fine():
    conn = connect()
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS market_fine_bins (
            market TEXT, bin TEXT, ornek INTEGER, oran REAL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (market, bin)
        )
    ''')
    conn.commit()
    conn.close()


def _1x_sonuc(h, a):
    if h > a:
        return "1"
    if h == a:
        return "0"
    return "2"


def _market_outcomes(hs, aws, fhh, fha):
    """Tek bir macin skorlarindan TUM marketlerin (bilinen olanlar) 0/1
    sonucunu doner - fh skorlari yoksa (None) sadece 'ms_' marketleri doner."""
    out = {
        "ms_kg": 1 if (hs > 0 and aws > 0) else 0,
        "ms_over_05": 1 if (hs + aws) > 0.5 else 0,
        "ms_over_15": 1 if (hs + aws) > 1.5 else 0,
        "ms_over_25": 1 if (hs + aws) > 2.5 else 0,
        "ms_over_35": 1 if (hs + aws) > 3.5 else 0,
        "ms_over_45": 1 if (hs + aws) > 4.5 else 0,
    }
    if fhh is None or fha is None:
        return out
    ikinci_h, ikinci_a = hs - fhh, aws - fha
    out.update({
        "iy_kg": 1 if (fhh > 0 and fha > 0) else 0,
        "iy2_kg": 1 if (ikinci_h > 0 and ikinci_a > 0) else 0,
        "iy_ve_iy2_kg": 1 if (fhh > 0 and fha > 0 and ikinci_h > 0 and ikinci_a > 0) else 0,
        "iy_over_05": 1 if (fhh + fha) > 0.5 else 0,
        "iy_over_15": 1 if (fhh + fha) > 1.5 else 0,
        "iy_over_25": 1 if (fhh + fha) > 2.5 else 0,
    })
    iy_s, ms_s = _1x_sonuc(fhh, fha), _1x_sonuc(hs, aws)
    for k in MARKET_LABELS:
        if k.startswith("iyms_"):
            out[k] = 1 if k == f"iyms_{iy_s}{ms_s}" else 0
    return out


def build_market_fine(verbose=True):
    ensure_schema_market_fine()
    conn = connect()
    cur = conn.cursor()
    cur.execute('''
        SELECT o.home_win_odds, o.draw_odds, o.away_win_odds,
               m.home_score, m.away_score, m.first_half_home_score, m.first_half_away_score
        FROM prematch_odds o JOIN matches m ON m.id = o.match_id
        WHERE o.home_win_odds IS NOT NULL AND o.draw_odds IS NOT NULL
          AND o.away_win_odds IS NOT NULL AND m.home_score IS NOT NULL AND m.away_score IS NOT NULL
    ''')
    kova = {}  # {market: {bin: [n, hit]}}
    for ev, bx, dep, hs, aws, fhh, fha in cur.fetchall():
        p = devig_1x2(ev, bx, dep)
        if not p:
            continue
        favori = max(p[0], p[2])
        b = _fine_bin(favori)
        for market, sonuc in _market_outcomes(hs, aws, fhh, fha).items():
            k = kova.setdefault(market, {}).setdefault(b, [0, 0])
            k[0] += 1
            k[1] += sonuc

    satirlar = [
        (market, b, n, h / n)
        for market, dilimler in kova.items()
        for b, (n, h) in dilimler.items()
        if n >= FINE_MIN_ORNEK
    ]
    cur.executemany(
        "INSERT INTO market_fine_bins (market, bin, ornek, oran, updated_at) VALUES (?,?,?,?,CURRENT_TIMESTAMP) "
        "ON CONFLICT(market, bin) DO UPDATE SET ornek=excluded.ornek, oran=excluded.oran, updated_at=CURRENT_TIMESTAMP",
        satirlar,
    )
    conn.commit()
    conn.close()
    if verbose:
        print(f"[odds_profile] {len(MARKET_LABELS)} market x dilim -> {len(satirlar)} guvenilir hucre hesaplandi.", flush=True)
    return len(satirlar)


def fine_rate_for_favori(favori):
    """Bir favori gucu (0-1) icin ince dilimden (ornegi yeterliyse) IY gol
    oranini ve ornek sayisini doner, yoksa None."""
    conn = connect()
    try:
        row = conn.execute(
            "SELECT ornek, iy_gol_orani, ms15_orani FROM odds_profile_rates_fine WHERE bin=?",
            (_fine_bin(favori),),
        ).fetchone()
    except sqlite3.OperationalError:
        row = None
    conn.close()
    return row  # (ornek, iy_gol_orani, ms15_orani) veya None


_onbellek = {"v": None}


def tazele():
    _onbellek["v"] = None


def _yukle():
    if _onbellek["v"] is None:
        conn = connect()
        try:
            rows = conn.execute(
                "SELECT band, ornek, iy_gol_orani, ms15_orani FROM odds_profile_rates"
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
        conn.close()
        _onbellek["v"] = {b: (n, iy, ms) for b, n, iy, ms in rows}
    return _onbellek["v"]


def canli_1x2(match_id_db):
    """Kaydedilmis canli 1X2 oranlarini doner (en guncel). Yoksa None."""
    conn = connect()
    cur = conn.cursor()
    try:
        cur.execute('''
            SELECT selection, odds FROM live_odds
            WHERE match_id = ? AND market IN ('1x2','Match Odds')
              AND captured_at = (SELECT MAX(captured_at) FROM live_odds
                                 WHERE match_id = ? AND market IN ('1x2','Match Odds'))
        ''', (match_id_db, match_id_db))
        rows = cur.fetchall()
    except sqlite3.OperationalError:
        rows = []
    conn.close()
    if not rows:
        return None
    d = {str(s).lower(): o for s, o in rows if o}
    ev, bx, dep = d.get("1"), d.get("x"), d.get("2")
    if not (ev and bx and dep):
        return None
    return ev, bx, dep


def profil_orani(match_id_db):
    """Canli oranlardan arsiv profilini bulur (KABA 3 bant - dengeli/
    dengesiz/cok_dengesiz). Geriye donuk uyumluluk icin duruyor, bot_odds_profile
    artik profil_orani_fine kullaniyor (bkz. o fonksiyonun docstring'i).

    Doner: (band, ornek, iy_orani, ms15_orani) veya None
    """
    o = canli_1x2(match_id_db)
    if not o:
        return None
    band = denge_bandi(*o)
    if not band:
        return None
    veri = _yukle().get(band)
    if not veri:
        return None
    n, iy, ms = veri
    return band, n, iy, ms


def profil_orani_fine(match_id_db):
    """profil_orani ile AYNI is ama KABA 3 bant yerine favori gucune gore
    %5'lik ince dilimlerle (bkz. build_fine) - cok daha ayirt edici (kullanici
    talebi 2026-08-24: "IY gol olan maclara sinyal verip botu tetikleyip
    macı takip etmesini saglayalim" - 3 bantli sistemin tavani %65.4 idi,
    SINYAL_ESIGI olan %62'yi zar zor geciyordu; ince dilimlerle bircok
    net-favorili mac gercekte %72+ cikiyor).

    Doner: (bin_etiketi, ornek, iy_orani, ms15_orani) veya None
    """
    o = canli_1x2(match_id_db)
    if not o:
        return None
    p = devig_1x2(*o)
    if not p:
        return None
    favori = max(p[0], p[2])
    fine = fine_rate_for_favori(favori)
    if not fine:
        return None
    ornek, iy, ms = fine
    return _fine_bin(favori), ornek, iy, ms
