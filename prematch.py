"""
Mac oncesi takim profili katmani.

NEDEN GEREKLI:
  Mevcut 18 botun HEPSI canli momentum'a (son 5-10 dk'daki sut/korner artisi) bakiyor.
  Bunun yapisal bir kor noktasi var: macin ilk 15-20 dakikasinda henuz momentum verisi
  YOKTUR (sut yok, korner yok, karsilastirilacak eski snapshot yok). Bu yuzden sistem
  macin basinda hicbir sinyal uretemiyor - veride de gorunuyor: en erken sinyal 15'.

  Oysa "Ilk Yari 0.5 Ust" gibi en degerli marketler tam da macin basinda oynanir.
  Rakip uygulamalarin 6. dakikada alert girebilmesinin sebebi canli veri degil,
  MAC ONCESI takim profilidir: "bu iki takim tarihsel olarak cok gollu mac oynuyor".

BU MODUL:
  Arsivdeki 33 binden fazla bitmis mactan her takim icin gol profili cikarir.
  Boylece botlar, canli veri henuz yokken bile bilgiye dayali karar verebilir.
"""
import sqlite3

import team_matcher
from db_config import DB_PATH, connect

MIN_MATCHES = 5  # bu sayidan az maci olan takim icin profil guvenilmez


def ensure_schema():
    conn = connect()
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS team_profiles (
            team_id TEXT PRIMARY KEY,
            matches_played INTEGER,
            fh_goal_rate REAL,      -- ilk yarida en az 1 gol olan mac orani
            over_15_rate REAL,      -- mac sonu 1.5 ust orani
            over_25_rate REAL,      -- mac sonu 2.5 ust orani
            avg_total_goals REAL,   -- mac basina toplam gol
            avg_fh_goals REAL,      -- mac basina ilk yari golu
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # Canli API adi -> arsiv adi eslestirme onbellegi.
    # Eslestirme pahali (3.500 kayit uzerinde arama), her sinyalde tekrar
    # hesaplanmasin diye sonuc burada saklanir. method='yok' olanlar da
    # yazilir ki ayni isim icin bosuna tekrar aranmasin.
    cur.execute('''
        CREATE TABLE IF NOT EXISTS team_aliases (
            live_name TEXT PRIMARY KEY,
            archive_name TEXT,
            method TEXT,
            score REAL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()


_index_cache = {"idx": None}


def _get_index():
    idx = _index_cache["idx"]
    if idx is None or not idx["names"]:
        conn = connect()
        names = [r[0] for r in conn.execute("SELECT team_id FROM team_profiles")]
        conn.close()
        idx = team_matcher.build_index(names)
        # team_profiles henuz bossa (v4_api_bot ile orchestrator ayri surecler,
        # digeri henuz build_profiles() calistirmamis olabilir) bos indeksi
        # ONBELLEKLEME - bir sonraki cagrida tekrar denesin. Aksi halde ilk
        # cagrida bos gelen indeks kalici olarak yapisip tum eslesmeler
        # sonsuza kadar basarisiz oluyordu.
        if idx["names"]:
            _index_cache["idx"] = idx
    return idx


def resolve_team(live_name):
    """Canli API adini arsiv adina cevirir (onbellekli). Bulunamazsa None."""
    if not live_name:
        return None
    conn = connect()
    cur = conn.cursor()
    cur.execute("SELECT archive_name FROM team_aliases WHERE live_name = ?", (live_name,))
    row = cur.fetchone()
    if row:
        conn.close()
        return row[0]

    name, method, score = team_matcher.match(live_name, _get_index())
    cur.execute(
        "INSERT OR REPLACE INTO team_aliases (live_name, archive_name, method, score) VALUES (?,?,?,?)",
        (live_name, name, method, score),
    )
    conn.commit()
    conn.close()
    return name


def build_profiles(verbose=True):
    """Arsivdeki bitmis maclardan takim profillerini yeniden hesaplar."""
    ensure_schema()
    conn = connect()
    cur = conn.cursor()

    # Her takimin hem ev sahibi hem deplasman maclarini tek listede topla
    cur.execute('''
        WITH oyunlar AS (
            SELECT home_team_id AS team,
                   home_score + away_score AS toplam,
                   COALESCE(first_half_home_score,0) + COALESCE(first_half_away_score,0) AS iy,
                   first_half_home_score IS NOT NULL AS iy_var
            FROM matches
            WHERE status='FINISHED' AND home_score IS NOT NULL
            UNION ALL
            SELECT away_team_id,
                   home_score + away_score,
                   COALESCE(first_half_home_score,0) + COALESCE(first_half_away_score,0),
                   first_half_home_score IS NOT NULL
            FROM matches
            WHERE status='FINISHED' AND home_score IS NOT NULL
        )
        SELECT team,
               COUNT(*),
               AVG(CASE WHEN iy_var AND iy > 0 THEN 1.0 ELSE 0.0 END),
               AVG(CASE WHEN toplam > 1.5 THEN 1.0 ELSE 0.0 END),
               AVG(CASE WHEN toplam > 2.5 THEN 1.0 ELSE 0.0 END),
               AVG(toplam * 1.0),
               AVG(CASE WHEN iy_var THEN iy * 1.0 END)
        FROM oyunlar
        WHERE team IS NOT NULL AND team != ''
        GROUP BY team
        HAVING COUNT(*) >= ?
    ''', (MIN_MATCHES,))
    rows = cur.fetchall()

    cur.executemany('''
        INSERT INTO team_profiles
            (team_id, matches_played, fh_goal_rate, over_15_rate, over_25_rate,
             avg_total_goals, avg_fh_goals, updated_at)
        VALUES (?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
        ON CONFLICT(team_id) DO UPDATE SET
            matches_played=excluded.matches_played,
            fh_goal_rate=excluded.fh_goal_rate,
            over_15_rate=excluded.over_15_rate,
            over_25_rate=excluded.over_25_rate,
            avg_total_goals=excluded.avg_total_goals,
            avg_fh_goals=excluded.avg_fh_goals,
            updated_at=CURRENT_TIMESTAMP
    ''', rows)
    conn.commit()
    conn.close()

    if verbose:
        print(f"[prematch] {len(rows)} takim profili guncellendi.", flush=True)
    return len(rows)


def get_profile(team_id):
    """Tek takimin profilini doner; yoksa None."""
    conn = connect()
    cur = conn.cursor()
    cur.execute('''SELECT matches_played, fh_goal_rate, over_15_rate, over_25_rate,
                          avg_total_goals, avg_fh_goals
                   FROM team_profiles WHERE team_id = ? COLLATE NOCASE''', (team_id,))
    r = cur.fetchone()
    conn.close()
    if not r:
        return None
    return {
        "matches_played": r[0], "fh_goal_rate": r[1], "over_15_rate": r[2],
        "over_25_rate": r[3], "avg_total_goals": r[4], "avg_fh_goals": r[5],
    }


def match_expectation(home_team, away_team):
    """Iki takimin profilini birlestirip mac oncesi gol beklentisi uretir.

    Doner: dict veya None (iki takim icin de yeterli veri yoksa).
    Yorum: 'guven' alani, elimizdeki mac sayisina dayali bir veri kalitesi olcusudur -
    az macli takimlarda tahmine az guvenilmelidir.
    """
    # Canli API adlarini arsiv adlarina cevir (Bayern München -> bayern münih)
    h_arch = resolve_team(home_team) or home_team
    a_arch = resolve_team(away_team) or away_team

    h = get_profile(h_arch)
    a = get_profile(a_arch)
    if not h or not a:
        return None

    n = min(h["matches_played"], a["matches_played"])
    # 5 mac -> dusuk guven, 20+ mac -> yuksek guven
    guven = max(0.3, min(1.0, n / 20.0))

    return {
        "fh_goal_rate": (h["fh_goal_rate"] + a["fh_goal_rate"]) / 2,
        "over_15_rate": (h["over_15_rate"] + a["over_15_rate"]) / 2,
        "over_25_rate": (h["over_25_rate"] + a["over_25_rate"]) / 2,
        "beklenen_gol": (h["avg_total_goals"] + a["avg_total_goals"]) / 2,
        "beklenen_iy_gol": ((h["avg_fh_goals"] or 0) + (a["avg_fh_goals"] or 0)) / 2,
        "guven": round(guven, 2),
        "veri_mac_sayisi": n,
    }


if __name__ == "__main__":
    build_profiles()
