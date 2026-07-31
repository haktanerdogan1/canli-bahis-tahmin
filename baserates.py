"""
Olculmus taban oranlar.

NEDEN VAR:
  Uzman botlarin ic formullerindeki katsayilar (0.38 + sut_hizi*0.075 gibi) ELLE
  KONULMUS tahminlerdi - veriden gelmiyordu. Makul gorunseler de dogrulanmamislardi.

  Bu modul ayni soruyu VERIYE sorar:
      "60. dakikada, skor 1-0 iken, tarihsel olarak maclarin yuzde kacinda
       bir gol daha geldi?"

  Cevap uydurma degil olculmus bir sayidir. Kendi arsivimizden (bitmis maclar +
  canli snapshot'lar) hesaplanir ve veri biriktikce tazelenir.

ISTATISTIKSEL YAKLASIM:
  Hucreler (dakika x mevcut gol) hizli seyreliyor. Bu yuzden HIYERARSIK GERI
  CEKILME uygulanir: hucrede yeterli ornek varsa o kullanilir, yoksa daha genis
  bir kirilima (sadece dakika), o da yetmezse genel ortalamaya dusulur.
  Boylece az veriyle uc tahmin yapilmaz.
"""
import sqlite3

from db_config import DB_PATH

MIN_ORNEK = 60          # bir hucreye guvenmek icin gereken en az ornek
DAKIKA_KOVASI = 15


def ensure_schema():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS base_rates (
            kirilim TEXT,          -- 'dakika' | 'dakika_gol' | 'genel'
            anahtar TEXT,          -- '60' | '60|1' | 'all'
            ornek INTEGER,
            oran REAL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (kirilim, anahtar)
        )
    ''')
    conn.commit()
    conn.close()


def build(verbose=True):
    """Arsiv + snapshot verisinden taban oranlari yeniden hesaplar."""
    ensure_schema()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # Her snapshot bir gozlem: o andaki durum -> mac sonunda gol geldi mi?
    cur.execute('''
        SELECT s.minute,
               s.home_score + s.away_score,
               m.home_score + m.away_score
        FROM live_snapshots s JOIN matches m ON m.id = s.match_id
        WHERE m.status = 'FINISHED' AND m.home_score IS NOT NULL
          AND s.home_score IS NOT NULL AND s.away_score IS NOT NULL
          AND s.minute BETWEEN 1 AND 95
    ''')
    gozlemler = cur.fetchall()

    dakika = {}
    dakika_gol = {}
    genel = [0, 0]

    for mn, anki, final in gozlemler:
        if anki is None or final is None:
            continue
        geldi = 1 if final > anki else 0
        dk = (mn // DAKIKA_KOVASI) * DAKIKA_KOVASI

        genel[0] += 1
        genel[1] += geldi

        d = dakika.setdefault(dk, [0, 0])
        d[0] += 1; d[1] += geldi

        k = (dk, min(anki, 3))
        g = dakika_gol.setdefault(k, [0, 0])
        g[0] += 1; g[1] += geldi

    satirlar = [("genel", "all", genel[0], genel[1] / genel[0] if genel[0] else 0.5)]
    for dk, (n, h) in dakika.items():
        satirlar.append(("dakika", str(dk), n, h / n if n else 0.5))
    for (dk, gol), (n, h) in dakika_gol.items():
        satirlar.append(("dakika_gol", f"{dk}|{gol}", n, h / n if n else 0.5))

    cur.executemany(
        "INSERT INTO base_rates (kirilim, anahtar, ornek, oran, updated_at) "
        "VALUES (?,?,?,?,CURRENT_TIMESTAMP) "
        "ON CONFLICT(kirilim, anahtar) DO UPDATE SET "
        "ornek=excluded.ornek, oran=excluded.oran, updated_at=CURRENT_TIMESTAMP",
        satirlar,
    )
    conn.commit()
    conn.close()

    if verbose:
        print(f"[baserates] {len(gozlemler):,} gozlemden {len(satirlar)} taban orani hesaplandi.",
              flush=True)
    return len(satirlar)


_onbellek = {"veri": None}


def _yukle():
    if _onbellek["veri"] is None:
        conn = sqlite3.connect(DB_PATH)
        try:
            rows = conn.execute("SELECT kirilim, anahtar, ornek, oran FROM base_rates").fetchall()
        except sqlite3.OperationalError:
            rows = []
        conn.close()
        _onbellek["veri"] = {(k, a): (n, o) for k, a, n, o in rows}
    return _onbellek["veri"]


def tazele():
    _onbellek["veri"] = None


def oran(minute, mevcut_gol):
    """Olculmus 'bir gol daha gelir' olasiligi.

    Doner: (olasilik, ornek_sayisi, kullanilan_kirilim) veya (None, 0, 'yok')
    """
    veri = _yukle()
    if not veri:
        return None, 0, "yok"

    dk = (int(minute) // DAKIKA_KOVASI) * DAKIKA_KOVASI

    # 1) En dar kirilim: dakika + mevcut gol
    hit = veri.get(("dakika_gol", f"{dk}|{min(int(mevcut_gol), 3)}"))
    if hit and hit[0] >= MIN_ORNEK:
        return hit[1], hit[0], "dakika+gol"

    # 2) Geri cekil: sadece dakika
    hit = veri.get(("dakika", str(dk)))
    if hit and hit[0] >= MIN_ORNEK:
        return hit[1], hit[0], "dakika"

    # 3) Son care: genel ortalama
    hit = veri.get(("genel", "all"))
    if hit:
        return hit[1], hit[0], "genel"

    return None, 0, "yok"
