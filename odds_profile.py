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
    """Canli oranlardan arsiv profilini bulur.

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
