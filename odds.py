"""
Canli oran kaydi ve piyasa karsilastirmasi.

NEDEN KRITIK:
  Bir tahmin sisteminin "%72 tutturuyorum" demesi tek basina hicbir sey ifade etmez.
  Onemli olan PIYASANIN AYNI OLAYA verdigi fiyati yenip yenmedigimizdir. Arsiv
  verisinde olculdu: marj cikarildiginda bahisci olasiliklari neredeyse kusursuz
  dogruydu (ornek: piyasa %60.0 diyor, gercek %60.3). Yani asil soru "kac tuttu"
  degil, "oranin ustunde mi tuttu".

KESFEDILEN VERI:
  RapidAPI'nin football-event-odds ucu ust/alt orani VERMIYOR; ancak
  "Next to score" marketi donuyor. Bu marketin 'x' secenegi = "daha gol olmaz".
  Bizim sinyalimiz ise tam tersi: "gol gelecek". Yani:

      P(bizim sinyal tutar) = 1 - P(next to score = x)

  1-0 skorda verdigimiz "Mac Sonu 1.5 Ust" sinyali ile "daha gol gelir mi"
  MATEMATIKSEL OLARAK AYNI OLAYDIR. Bu sayede birebir karsilastirma yapabiliyoruz.

NOT: Ilk yari marketleri icin bu esitlik TAM DEGILDIR (sonraki gol ikinci yarida
     gelebilir), o yuzden ilk yari sinyalleri ayri degerlendirilmelidir.
"""
import json
import os
import sqlite3
import urllib.request
from datetime import datetime, timezone

from db_config import DB_PATH

HOST = "free-api-live-football-data.p.rapidapi.com"

# Hangi ulke kodlari denenecek. Farkli ulkeler farkli saglayici/market donduruyor:
#   DE -> Interwetten (Next to score veriyor - bizim icin en degerlisi)
#   GB -> Betfair (dusuk marj, Both Teams To Score)
# Sirali denenir, ilk dolu yanit kullanilir.
ULKE_SIRASI = ["DE", "GB", "BR"]


def ensure_schema():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS live_odds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id INTEGER,
            captured_at TIMESTAMP,
            minute INTEGER,
            provider TEXT,
            country TEXT,
            market TEXT,
            selection TEXT,
            odds REAL,
            FOREIGN KEY(match_id) REFERENCES matches(id)
        )
    ''')
    cur.execute("CREATE INDEX IF NOT EXISTS ix_live_odds_match ON live_odds(match_id, captured_at)")
    conn.commit()
    conn.close()


def _cek(url, anahtar, timeout=12):
    req = urllib.request.Request(
        url, headers={"x-rapidapi-host": HOST, "x-rapidapi-key": anahtar}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _marketleri_ayikla(payload):
    """API yanitindan (saglayici, market, secim, oran) listesi cikarir."""
    o = (payload.get("response") or {}).get("odds")
    if not o:
        return None, []

    saglayici = o.get("persistentKey")
    ic = o.get("odds") or {}
    cikti = []

    def market_ekle(m):
        hdr = m.get("header")
        for s in m.get("selections", []):
            try:
                oran = float(s.get("oddsDecimal"))
            except (TypeError, ValueError):
                continue
            cikti.append((hdr, s.get("name"), oran))

    for kat in (ic.get("oddsTabMarkets") or []):
        for m in kat.get("markets", []):
            market_ekle(m)
    for m in (ic.get("matchfactMarkets") or []):
        market_ekle(m)

    # Ayni market/secim birden fazla blokta gecebilir - tekille
    return saglayici, list(dict.fromkeys(cikti))


def kaydet(match_id_db, source_match_id, minute, anahtar=None):
    """Bir mac icin oranlari ceker ve kaydeder. Kaydedilen satir sayisini doner."""
    anahtar = anahtar or os.environ.get("RAPIDAPI_KEY")
    if not anahtar or not source_match_id:
        return 0

    event_id = str(source_match_id).replace("v4_", "")
    ensure_schema()
    simdi = datetime.now(timezone.utc).isoformat()

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    toplam = 0

    for cc in ULKE_SIRASI:
        try:
            payload = _cek(
                f"https://{HOST}/football-event-odds?eventid={event_id}&countrycode={cc}",
                anahtar,
            )
        except Exception:
            continue

        saglayici, satirlar = _marketleri_ayikla(payload)
        if not satirlar:
            continue

        cur.executemany(
            "INSERT INTO live_odds (match_id, captured_at, minute, provider, country,"
            " market, selection, odds) VALUES (?,?,?,?,?,?,?,?)",
            [(match_id_db, simdi, minute, saglayici, cc, mk, sc, od)
             for mk, sc, od in satirlar],
        )
        toplam += len(satirlar)
        break  # ilk dolu yanit yeterli, kota harcamayalim

    conn.commit()
    conn.close()
    return toplam


def piyasa_gol_olasiligi(match_id_db):
    """Piyasaya gore 'daha gol gelir' olasiligi (marj temizlenmis). Yoksa None.

    'Next to score' marketinin x secenegi 'daha gol olmaz' demektir.
    Marj (overround) cikarilarak gercek olasilik tahmini uretilir.
    """
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('''
        SELECT selection, odds FROM live_odds
        WHERE match_id = ? AND market = 'Next to score'
          AND captured_at = (SELECT MAX(captured_at) FROM live_odds
                             WHERE match_id = ? AND market = 'Next to score')
    ''', (match_id_db, match_id_db))
    satirlar = cur.fetchall()
    conn.close()

    if not satirlar:
        return None

    ters = {s: 1.0 / o for s, o in satirlar if o and o > 1.0}
    if "x" not in ters:
        return None

    toplam = sum(ters.values())  # 1'den buyuk: aradaki fark bahisci marji
    if toplam <= 0:
        return None

    gol_olmaz = ters["x"] / toplam        # marj temizlenmis
    return round(1.0 - gol_olmaz, 4)


def marj(match_id_db, market="Next to score"):
    """Bir marketteki bahisci marjini doner (0.14 = %14)."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('''
        SELECT odds FROM live_odds
        WHERE match_id = ? AND market = ?
          AND captured_at = (SELECT MAX(captured_at) FROM live_odds
                             WHERE match_id = ? AND market = ?)
    ''', (match_id_db, market, match_id_db, market))
    o = [r[0] for r in cur.fetchall() if r[0] and r[0] > 1.0]
    conn.close()
    if not o:
        return None
    return round(sum(1.0 / x for x in o) - 1.0, 4)


def hareket(match_id_db, market="Next to score", selection="x", dakika_penceresi=10):
    """Oran hareketi: son kayit ile penceredeki ilk kayit arasindaki degisim.

    Senin bahsettigin "ani dusus" tespiti icin. Oranin DUSMESI, piyasanin o
    olaya daha yuksek ihtimal verdigi anlamina gelir (para o yone akiyor).
    Doner: (eski_oran, yeni_oran, yuzde_degisim) veya None.
    """
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('''
        SELECT odds, minute FROM live_odds
        WHERE match_id = ? AND market = ? AND selection = ?
        ORDER BY captured_at DESC LIMIT 40
    ''', (match_id_db, market, selection))
    kayitlar = cur.fetchall()
    conn.close()

    if len(kayitlar) < 2:
        return None

    yeni, yeni_dk = kayitlar[0]
    eski, eski_dk = None, None
    for o, dk in kayitlar[1:]:
        if dk is not None and yeni_dk is not None and (yeni_dk - dk) >= dakika_penceresi:
            eski, eski_dk = o, dk
            break
    if eski is None:
        eski, eski_dk = kayitlar[-1]

    if not eski or eski <= 0:
        return None
    return (eski, yeni, round((yeni - eski) / eski * 100, 2))
