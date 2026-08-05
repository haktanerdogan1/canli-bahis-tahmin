"""TheSports API'sinden canli mac verisi ceken bot (RapidAPI'nin yerine).

NEDEN VAR: RapidAPI'nin ucretsiz katmani (free-api-live-football-data) kotasi
tukendi (bkz. git log - "kota doldu"). TheSports, tek istekte TUM canli
maclarin skor+istatistik+olay verisini donduren cok daha zengin bir API
sunuyor (dakikada 1000 istek siniri - bizim icin fazlasiyla yeterli).

MIMARI KARAR: Bu dosya v4_api_bot.py ile AYNI veritabani semasina (matches,
live_snapshots) yaziyor. Boylece orchestrator.py, botlar, api.py HIC
DEGISMIYOR - sadece veri kaynagi degisiyor. supervisor.py bu dosyayi
v4_api_bot.py YERINE calistiracak sekilde guncellenecek.

BILINMEYEN ALANLAR HAKKINDA: TheSports'un stats[].type ve score[1] (status_id)
kodlarinin TAM referans tablosu dokumantasyonda bulunamadi (bkz. proje sohbet
gecmisi). Onceki RapidAPI oturumundaki "istatistik anahtari kesfi" desenini
aynen uyguluyoruz: SADECE guclu kanitla dogrulanmis kodlari (type=25 -> top
hakimiyeti, ev+deplasman HER ZAMAN 100'e tamamlaniyor; status_id=8 -> bitmis,
yuksek skorlu tamamlanmis bir macla eslesti) isliyoruz, geri kalanini TAHMIN
ETMEDEN loglayip stat_key_discovery tablosuna yaziyoruz - gercek trafikte
gozlemleyip zamanla netlestirecegiz.
"""
import time
import os
import requests

from db_config import connect
from match_filter import is_known_match

USER = os.environ.get("THESPORTS_USER")
SECRET = os.environ.get("THESPORTS_SECRET")
if not USER or not SECRET:
    raise RuntimeError("THESPORTS_USER/THESPORTS_SECRET ortam degiskenleri tanimli degil. Railway'de Variables'a ekle.")

BASE_URL = "https://api.thesports.com"
LIVE_PATH = "/v1/football/match/detail_live"
DIARY_PATH = "/v1/football/match/diary"

NORMAL_CYCLE_SECONDS = 10  # dakikada 1000 istek sinirimiz var, 2sn onerdiler - 10sn cok temkinli
DIARY_REFRESH_SECONDS = 3 * 3600  # takim/lig ad+logo onbellegi 3 saatte bir tazelenir
MISSING_GRACE_MINUTES = 5
STALE_PROGRESS_MINUTES = 15
MAX_PLAUSIBLE_LIVE_AGE_SECONDS = 4 * 60 * 60

# --- Sadece GUCLU KANITLA dogrulanmis kodlar (bkz. dosya basi aciklama) ---
STAT_TYPE_POSSESSION = 25
# type 24, stat_type_samples kanitinda (193 gozlem, 69 mac) HER ZAMAN type 23'un
# alt kumesi (193'te sadece 10 ihlal, ~%5 - canli veri jitter'iyla aciklanabilir):
# "toplam atak / tehlikeli atak" iliskisiyle birebir ortusuyor.
STAT_TYPE_ATTACKS = 23
STAT_TYPE_DANGEROUS_ATTACKS = 24
# type 3, ayni kanitta neredeyse hep sifir (%89) ve nadiren kucuk pozitif deger
# (max 5) - kirmizi kartin seyrek-olay imzasiyla ortusuyor.
STAT_TYPE_RED_CARDS = 3
STATUS_FINISHED_CONFIRMED = {8}
STATUS_LIVE_CONFIRMED = {2}  # gozlemlenen tum ornekler ilk yaridaydi (dk<=45)

# --- Takim/lig ad+logo onbellegi (diary'den periyodik yenilenir) ---
_team_cache = {}
_competition_cache = {}
# detail_live yanitinda SADECE mac id'si var (takim id'si yok!) - hangi
# maçin hangi iki takim arasinda oldugunu diary'nin 'results' listesinden
# (mac id -> home_team_id/away_team_id/competition_id) coziyoruz.
_match_info_cache = {}
_last_diary_refresh = 0.0

# --- Kesif mekanizmasi: bilinmeyen stat tipi / status_id kodlarini logla ---
_gorulen_stat_tipleri = set()
_gorulen_status_id = set()

# --- Kanit biriktirme: her (stat tipi, mac, 15dk dilimi) icin TEK ornek.
# NEDEN: tek anlik ornek (ilk gorulen deger) hangi kodun sut/korner/kirmizi
# kart oldugunu ayirt etmeye yetmiyor - zamanla ARTIP ARTMADIGINI (korner/sut
# gibi) ya da neredeyse hep 0 KALIP KALMADIGINI (kirmizi kart gibi) gormek
# lazim. Mac+15dk dilimi basina sinirlandirmak yaziyi orantisiz artirmadan
# (ayni maci her 10sn'de tekrar tekrar yazmadan) zaman-serisi kaniti biriktirir.
_gorulen_ornek_anahtari = set()


def _stat_tipi_kaydet(type_code, home, away):
    if type_code in _gorulen_stat_tipleri:
        return
    _gorulen_stat_tipleri.add(type_code)
    print(f"🔍 YENI STAT TIPI: {type_code} -> ornek home={home} away={away}", flush=True)


def _status_id_kaydet(status_id, ornek):
    if status_id in _gorulen_status_id:
        return
    _gorulen_status_id.add(status_id)
    print(f"🔍 YENI STATUS_ID: {status_id} -> ornek: {ornek}", flush=True)


def _kesif_ozeti_yaz():
    """Gorulen bilinmeyen kodlari veritabanina yazar (RapidAPI oturumundaki
    stat_key_discovery deseniyle ayni - tek yerde birikir)."""
    if not _gorulen_stat_tipleri and not _gorulen_status_id:
        return
    try:
        conn = connect()
        cur = conn.cursor()
        cur.execute("""CREATE TABLE IF NOT EXISTS stat_key_discovery (
            anahtar TEXT PRIMARY KEY, gorulme INTEGER, ilk_gorulme TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        for t in _gorulen_stat_tipleri:
            cur.execute("""INSERT INTO stat_key_discovery (anahtar, gorulme) VALUES (?,1)
                           ON CONFLICT(anahtar) DO UPDATE SET gorulme = gorulme + 1""",
                        (f"thesports_stat_type_{t}",))
        for s in _gorulen_status_id:
            cur.execute("""INSERT INTO stat_key_discovery (anahtar, gorulme) VALUES (?,1)
                           ON CONFLICT(anahtar) DO UPDATE SET gorulme = gorulme + 1""",
                        (f"thesports_status_id_{s}",))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"⚠️  Kesif ozeti yazilamadi: {e}", flush=True)


def _parse_stats(stats_list, match_id_api=None, minute=None, ornek_biriktir=None):
    """Sadece dogrulanmis alanlari (top hakimiyeti, atak, tehlikeli atak, kirmizi
    kart) cikarir, gerisini kesfe birakir.

    ornek_biriktir verilirse (bir liste), her stat tipi icin mac+15dk dilimi
    basina bir (tip, mac_id, dilim, home, away) ornegi ekler - bkz.
    _gorulen_ornek_anahtari aciklamasi.
    """
    h_pos = a_pos = 0
    h_atak = a_atak = 0
    h_teh = a_teh = 0
    h_kirmizi = a_kirmizi = 0
    for item in stats_list or []:
        t = item.get("type")
        h = item.get("home", 0)
        a = item.get("away", 0)
        _stat_tipi_kaydet(t, h, a)
        if t == STAT_TYPE_POSSESSION:
            h_pos, a_pos = h, a
        elif t == STAT_TYPE_ATTACKS:
            h_atak, a_atak = h, a
        elif t == STAT_TYPE_DANGEROUS_ATTACKS:
            h_teh, a_teh = h, a
        elif t == STAT_TYPE_RED_CARDS:
            h_kirmizi, a_kirmizi = h, a
        if ornek_biriktir is not None and match_id_api and minute is not None:
            dilim = (minute // 15) * 15
            anahtar = (t, match_id_api, dilim)
            if anahtar not in _gorulen_ornek_anahtari:
                _gorulen_ornek_anahtari.add(anahtar)
                ornek_biriktir.append((t, match_id_api, dilim, h, a))
    return h_pos, a_pos, h_atak, a_atak, h_teh, a_teh, h_kirmizi, a_kirmizi


def _diary_refresh(force=False):
    """Takim/lig isim+logo onbellegini TheSports'un diary endpoint'inden tazeler."""
    global _last_diary_refresh
    if not force and (time.time() - _last_diary_refresh) < DIARY_REFRESH_SECONDS:
        return
    try:
        r = requests.get(BASE_URL + DIARY_PATH, params={"user": USER, "secret": SECRET}, timeout=20)
        data = r.json()
        if data.get("code") != 0:
            print(f"⚠️  Diary hatasi: {data}", flush=True)
            return
        extra = data.get("results_extra", {}) or {}
        for t in extra.get("team", []):
            _team_cache[t["id"]] = {"name": t.get("name") or "Unknown", "logo": t.get("logo") or ""}
        for c in extra.get("competition", []):
            _competition_cache[c["id"]] = {"name": c.get("name") or "Unknown League", "logo": c.get("logo") or ""}
        for match in data.get("results", []) or []:
            mid = match.get("id")
            if not mid:
                continue
            _match_info_cache[mid] = {
                "home_team_id": match.get("home_team_id"),
                "away_team_id": match.get("away_team_id"),
                "competition_id": match.get("competition_id"),
            }
        _last_diary_refresh = time.time()
        print(f"📚 Onbellek tazelendi: {len(_team_cache)} takim, {len(_competition_cache)} lig, "
              f"{len(_match_info_cache)} mac eslesmesi", flush=True)
    except Exception as e:
        print(f"⚠️  Diary tazeleme hatasi: {e}", flush=True)


def _compute_minute(kickoff_ts, status_id, now=None):
    """TheSports'un dokumante ettigi formule gore dakikayi hesaplar.

    SADECE ilk yari icin (status_id=2, dk<=45 gozlemlendigi icin kanitli).
    Diger fazlar (devre arasi, ikinci yari, uzatma) icin dogru offset'i hangi
    status_id'nin temsil ettigini HENUZ bilmiyoruz (bkz. dosya basi aciklama) -
    TAHMIN ETMEK yerine minute_parsed_ok=False donup mevcut DB degerini
    korumayi tercih ediyoruz, tipki RapidAPI oturumundaki "DAKIKA
    AYRISTIRILAMADI" deseninde oldugu gibi.
    """
    now = now or time.time()
    if not kickoff_ts or status_id not in STATUS_LIVE_CONFIRMED:
        return 0, False
    elapsed_min = (now - kickoff_ts) / 60.0
    if 0 <= elapsed_min <= 130:
        return int(elapsed_min) + 1, True
    return 0, False


def _ensure_match_tracking_schema():
    conn = connect()
    try:
        conn.execute("ALTER TABLE matches ADD COLUMN last_seen_at TIMESTAMP")
    except Exception:
        pass
    try:
        conn.execute("ALTER TABLE matches ADD COLUMN last_progress_at TIMESTAMP")
    except Exception:
        pass
    conn.execute("""
        CREATE TABLE IF NOT EXISTS stat_type_samples (
            type_code INTEGER, match_id TEXT, minute_bucket INTEGER,
            home_val REAL, away_val REAL, captured_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(type_code, match_id, minute_bucket)
        )
    """)
    conn.execute("""
        UPDATE matches SET last_seen_at=CURRENT_TIMESTAMP
        WHERE last_seen_at IS NULL
          AND status NOT IN ('FINISHED','ABANDONED','Ended','FT','Canceled')
    """)
    conn.execute("""
        UPDATE matches SET last_progress_at=CURRENT_TIMESTAMP
        WHERE last_progress_at IS NULL
          AND status NOT IN ('FINISHED','ABANDONED','Ended','FT','Canceled')
    """)
    conn.commit()
    conn.close()


def _close_stale_missing(cursor, active_ids):
    params = []
    active_clause = ""
    if active_ids:
        placeholders = ','.join('?' for _ in active_ids)
        active_clause = f"AND source_match_id NOT IN ({placeholders})"
        params.extend(active_ids)
    cursor.execute(f'''
        UPDATE matches
        SET status = CASE WHEN COALESCE(minute, 0) >= 85
                          THEN 'FINISHED' ELSE 'ABANDONED' END
        WHERE status NOT IN ('FINISHED','ABANDONED','Ended','FT','Canceled')
          AND last_seen_at IS NOT NULL
          AND last_seen_at <= datetime('now', '-{MISSING_GRACE_MINUTES} minutes')
          {active_clause}
    ''', params)
    return cursor.rowcount


def _close_stale_progress(cursor):
    cursor.execute(f'''
        SELECT source_match_id FROM matches
        WHERE status IN ('LIVE','HT')
          AND last_progress_at IS NOT NULL
          AND last_progress_at <= datetime('now', '-{STALE_PROGRESS_MINUTES} minutes')
    ''')
    stale_ids = [row[0] for row in cursor.fetchall()]
    if stale_ids:
        placeholders = ','.join('?' for _ in stale_ids)
        cursor.execute(f'''
            UPDATE matches
            SET status = CASE WHEN COALESCE(minute, 0) >= 85
                              THEN 'FINISHED' ELSE 'ABANDONED' END
            WHERE source_match_id IN ({placeholders})
        ''', stale_ids)
    return stale_ids


def fetch_live():
    """TheSports'tan TUM canli maclarin skor+istatistik+olay verisini tek istekte ceker.

    Doner: (results_list, http_status). Hata durumunda ([], status_or_None).
    """
    try:
        r = requests.get(BASE_URL + LIVE_PATH, params={"user": USER, "secret": SECRET}, timeout=15)
        if r.status_code != 200:
            print(f"Failed to fetch live matches: {r.status_code}", flush=True)
            return [], r.status_code
        data = r.json()
        if data.get("code") != 0:
            print(f"TheSports API hatasi: {data}", flush=True)
            return [], None
        return data.get("results", []) or [], 200
    except Exception as e:
        print(f"Error fetching live matches: {e}", flush=True)
        return [], None


def process_matches(results):
    """Tek bir dongu: TheSports sonuclarini isleyip veritabanina yazar.

    UC ASAMALI YAPI (RapidAPI oturumundaki v4_api_bot.py'de kanitlanmis
    desenin AYNISI - bkz. git log "database is locked" duzeltmeleri):
    ASAMA 1'de KISA bir baglantiyla sadece temizlik+okuma yapilir, hemen
    kapatilir. ASAMA 2 HICBIR DB baglantisi ACIK DEGILKEN calisir - cunku
    is_known_match() -> prematch.resolve_team() KENDI AYRI baglantisini acip
    yazabiliyor (team_aliases onbellegi); bu ASAMA 1/3'un acik bir yazma
    transaction'iyla CAKISIRSA "database is locked" hatasi verir (ilk
    versiyonda TAM OLARAK bu oldu - orchestrator'da hata gorundu). ASAMA 3
    TEK KISA bir transaction'da hepsini yazar, hic disariya cagri yapmaz.
    """
    _diary_refresh()

    now = time.time()
    active_ids = ["ts_" + r["id"] for r in results if r.get("id")]

    # --- ASAMA 1: KISA baglanti, sadece temizlik + okuma ---
    conn = connect()
    cursor = conn.cursor()

    if active_ids:
        placeholders = ','.join('?' for _ in active_ids)
        cursor.execute(f'''
            UPDATE matches SET last_seen_at=CURRENT_TIMESTAMP
            WHERE status IN ('LIVE','HT') AND source_match_id IN ({placeholders})
        ''', active_ids)

    _close_stale_missing(cursor, active_ids)
    closed_stale = _close_stale_progress(cursor)
    if closed_stale:
        print(f"🧊 {len(closed_stale)} mac dakikasi ilerlemedigi icin kapatildi.", flush=True)
        closed_set = set(closed_stale)
        results = [r for r in results if ("ts_" + r.get("id", "")) not in closed_set]

    placeholders = ','.join('?' for _ in active_ids) if active_ids else ""
    existing_ids = set()
    if active_ids:
        cursor.execute(f"SELECT source_match_id FROM matches WHERE source_match_id IN ({placeholders})", active_ids)
        existing_ids = set(row[0] for row in cursor.fetchall())

    conn.commit()
    conn.close()

    # --- ASAMA 2: HANGI maclarin islenecegini belirle. HICBIR DB baglantisi
    # ACIK DEGIL - is_known_match() kendi baglantisini guvenle acabilir. ---
    to_process = []
    stat_feed_total = len(results)
    stat_skipped_unknown = 0
    stat_ornekleri = []

    for m in results:
        match_id_api = m.get("id")
        if not match_id_api:
            continue
        event_id = "ts_" + match_id_api

        score = m.get("score") or []
        if len(score) < 5:
            continue
        status_id = score[1]
        home_arr = score[2] if len(score) > 2 else [0] * 7
        away_arr = score[3] if len(score) > 3 else [0] * 7
        kickoff_ts = score[4]

        _status_id_kaydet(status_id, {"id": match_id_api, "score": score[:4]})

        # detail_live SADECE mac id'si veriyor - hangi takimlar oynuyor,
        # diary onbellegindeki mac->takim eslesmesinden coziliyor.
        info = _match_info_cache.get(match_id_api)
        if not info:
            # Bu mac henuz diary onbelleginde yok (yeni baslamis olabilir) -
            # atla, bir sonraki diary tazelemesinde (en fazla
            # DIARY_REFRESH_SECONDS sonra) cozulecek.
            continue

        home_team_id = info.get("home_team_id")
        away_team_id = info.get("away_team_id")
        competition_id = info.get("competition_id")

        home_name = _team_cache.get(home_team_id, {}).get("name") if home_team_id else None
        away_name = _team_cache.get(away_team_id, {}).get("name") if away_team_id else None
        home_logo = _team_cache.get(home_team_id, {}).get("logo", "") if home_team_id else ""
        away_logo = _team_cache.get(away_team_id, {}).get("logo", "") if away_team_id else ""

        if not home_name or not away_name:
            continue

        league_name = _competition_cache.get(competition_id, {}).get("name", "Unknown League") if competition_id else "Unknown League"
        league_logo = _competition_cache.get(competition_id, {}).get("logo", "") if competition_id else ""

        if event_id not in existing_ids:
            if not is_known_match(league_name, home_name, away_name):
                stat_skipped_unknown += 1
                continue

        minute, minute_parsed_ok = _compute_minute(kickoff_ts, status_id, now)

        if status_id in STATUS_FINISHED_CONFIRMED:
            match_status = "FINISHED"
        elif status_id in STATUS_LIVE_CONFIRMED:
            match_status = "LIVE"
        else:
            # Henuz dogrulanmamis bir status_id - "canli maclar" feed'inde
            # gorundugu icin guvenle LIVE'a alinabilir (TheSports zaten sadece
            # ilgili maclari bu ucta donduruyor); sadece dakikayi TAHMIN ETMIYORUZ.
            match_status = "LIVE"

        score_h = home_arr[0] if len(home_arr) > 0 else 0
        score_a = away_arr[0] if len(away_arr) > 0 else 0
        h_pos, a_pos, h_atak, a_atak, h_teh, a_teh, h_kirmizi, a_kirmizi = _parse_stats(
            m.get("stats") or [], match_id_api, minute, stat_ornekleri)

        to_process.append({
            "event_id": event_id, "home_name": home_name, "away_name": away_name,
            "match_status": match_status, "league_name": league_name,
            "league_logo": league_logo, "home_logo": home_logo, "away_logo": away_logo,
            "score_h": score_h, "score_a": score_a, "minute": minute,
            "minute_parsed_ok": minute_parsed_ok, "h_pos": h_pos, "a_pos": a_pos,
            "h_atak": h_atak, "a_atak": a_atak, "h_teh": h_teh, "a_teh": a_teh,
            "h_kirmizi": h_kirmizi, "a_kirmizi": a_kirmizi,
        })

    stat_processed = len(to_process)

    # --- ASAMA 3: TEK KISA transaction'da hepsini yaz. Disariya HIC cagri
    # yok (network, prematch vb.) - sadece INSERT/UPDATE. ---
    conn = connect()
    cursor = conn.cursor()

    for m in to_process:
        cursor.execute('''
            INSERT INTO matches
            (source_match_id, home_team_id, away_team_id, status, league_name, league_ccode, league_logo, home_score, away_score, minute, home_team_logo, away_team_logo, aggregate_score, last_seen_at, last_progress_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT(source_match_id) DO UPDATE SET
                minute=CASE WHEN ? THEN excluded.minute ELSE matches.minute END,
                status=excluded.status,
                home_score=excluded.home_score,
                away_score=excluded.away_score,
                league_name=excluded.league_name,
                league_ccode=excluded.league_ccode,
                league_logo=excluded.league_logo,
                home_team_logo=excluded.home_team_logo,
                away_team_logo=excluded.away_team_logo,
                last_seen_at=CURRENT_TIMESTAMP,
                last_progress_at=CASE
                    WHEN (CASE WHEN ? THEN excluded.minute ELSE matches.minute END) IS NOT matches.minute
                         OR excluded.home_score IS NOT matches.home_score
                         OR excluded.away_score IS NOT matches.away_score
                    THEN CURRENT_TIMESTAMP
                    ELSE matches.last_progress_at
                END
        ''', (m["event_id"], m["home_name"], m["away_name"], m["match_status"],
              m["league_name"], "INT", m["league_logo"],
              m["score_h"], m["score_a"], m["minute"], m["home_logo"], m["away_logo"], "",
              m["minute_parsed_ok"], m["minute_parsed_ok"]))

        cursor.execute('SELECT id FROM matches WHERE source_match_id = ?', (m["event_id"],))
        row = cursor.fetchone()
        if not row:
            continue
        match_id_db = row[0]

        cursor.execute('''
            INSERT INTO live_snapshots (
                match_id, minute, period, home_score, away_score,
                home_possession, away_possession,
                home_attacks, away_attacks,
                home_dangerous_attacks, away_dangerous_attacks,
                home_red_cards, away_red_cards
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (match_id_db, m["minute"], 'first_half' if m["minute"] <= 45 else 'second_half',
              m["score_h"], m["score_a"], m["h_pos"], m["a_pos"],
              m["h_atak"], m["a_atak"], m["h_teh"], m["a_teh"],
              m["h_kirmizi"], m["a_kirmizi"]))

    if stat_ornekleri:
        cursor.executemany('''
            INSERT OR IGNORE INTO stat_type_samples
            (type_code, match_id, minute_bucket, home_val, away_val)
            VALUES (?, ?, ?, ?, ?)
        ''', stat_ornekleri)

    cursor.execute("SELECT COUNT(*) FROM matches "
                   "WHERE status NOT IN ('FINISHED','ABANDONED','Ended','FT','Canceled')")
    still_open = cursor.fetchone()[0]

    conn.commit()
    conn.close()

    _kesif_ozeti_yaz()

    print(
        f"📊 feed={stat_feed_total} islenen={stat_processed} "
        f"atlanan(taninmayan)={stat_skipped_unknown} | DB'de hala acik(LIVE/HT)={still_open}",
        flush=True,
    )


def _ensure_team_profiles():
    try:
        import prematch
        conn = connect()
        row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='team_profiles'").fetchone()
        count = conn.execute("SELECT COUNT(*) FROM team_profiles").fetchone()[0] if row else 0
        conn.close()
        if count == 0:
            print("ℹ️  team_profiles bos, kuruluyor...", flush=True)
            prematch.build_profiles()
    except Exception as e:
        print(f"⚠️  team_profiles kurulamadi: {e}", flush=True)


def main():
    print("🚀 Starting TheSports Radar Bot...", flush=True)
    _ensure_match_tracking_schema()
    _ensure_team_profiles()
    _diary_refresh(force=True)

    while True:
        start_time = time.time()
        results, status = fetch_live()
        if status == 200:
            process_matches(results)
        elapsed = time.time() - start_time
        wait = max(1, NORMAL_CYCLE_SECONDS - elapsed)
        print(f"✅ TheSports Cycle completed in {elapsed:.2f}s. Waiting {wait:.1f}s...", flush=True)
        time.sleep(wait)


if __name__ == '__main__':
    main()
