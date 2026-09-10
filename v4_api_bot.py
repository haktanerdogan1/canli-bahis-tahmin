import asyncio
import time
import sqlite3
import os
import re
import aiohttp
from collections import deque

from db_config import DB_PATH, connect, measured_write  # Railway kalici disk destegi (bkz. db_config.py)
API_KEY = os.environ.get("RAPIDAPI_KEY")
if not API_KEY:
    raise RuntimeError("RAPIDAPI_KEY ortam degiskeni tanimli degil. Railway'de Variables'a ekle.")
HOST = "free-api-live-football-data.p.rapidapi.com"

HEADERS = {
    "x-rapidapi-host": HOST,
    "x-rapidapi-key": API_KEY
}

# Lig Haritası (leagueId -> {name, ccode, logo})
LEAGUES_CACHE = {}
try:
    import json
    with open(os.path.join(os.path.dirname(__file__), 'leagues.json'), 'r') as f:
        l_data = json.load(f)
        for lg in l_data.get("response", {}).get("leagues", []):
            LEAGUES_CACHE[str(lg["id"])] = {
                "name": lg.get("name", "Unknown"),
                "ccode": lg.get("ccode", "INT"),
                "logo": lg.get("logo", "")
            }
except Exception as e:
    print(f"Failed to load leagues.json: {e}")

# Manuel Lig Yamaları (API'nin tüm ligler listesinde olmayan ama canlı maçlarda dönen ID'ler)
LEAGUES_CACHE["937351"] = {"name": "UEFA Conference League", "ccode": "INT", "logo": "https://images.fotmob.com/image_resources/logo/leaguelogo/dark/10216.png"}
LEAGUES_CACHE["915708"] = {"name": "Club Friendlies", "ccode": "INT", "logo": ""}
LEAGUES_CACHE["251"] = {"name": "Ykkösliiga (Finlandiya)", "ccode": "FIN", "logo": ""}

# --- "Bilindik" lig/takım filtresi -------------------------------------------------------
# Amaç: RapidAPI sorgu kotasını, özellikle sezon dışı dönemde (çok sayıda alt lig / rezerv /
# altyapı maçı aynı anda oynanıyor) israf etmemek. Sadece tanınan büyük liglerin/takımların
# maçlarını takibe al. Gunluk YENI mac sayisina sabit bir tavan (DAILY_MATCH_CAP) eskiden
# vardi; kullanici istegiyle kaldirildi (yogun saatlerde canli mac akisini bogdugu icin).
MISSING_GRACE_MINUTES = 5
MAX_PLAUSIBLE_LIVE_AGE_SECONDS = 4 * 60 * 60
# Feed'de gorunmeye devam etse bile DAKIKASI ilerlemeyen bir mac, RapidAPI'nin
# o fikstur icin bayat/donmus veri dondurdugunun isaretidir (gercek bir canli
# macin dakikasi ~60 saniyede bir artar). MISSING_GRACE_MINUTES'ten (feed'den
# tamamen dusme) BAGIMSIZ bir esik - "gorunuyor ama ilerlemiyor" durumunu yakalar.
STALE_PROGRESS_MINUTES = 15

# RapidAPI'nin current-live cevabi bazen bir onceki gunden donup kalmis maclari
# da tasiyor. Servis yeniden baslayinca bunlar yeni canli mac sanilip tekrar
# sinyal uretiyordu. Baslangic zamani olmayan kayitlar icin iki sorgu arasinda
# dakika/skor ilerlemesi gorene kadar bekleyerek bunu genel olarak engelliyoruz.
_feed_observations = {}


def _plausibly_current_live_match(match, now=None):
    """Feed kaydi gercekten su an canli olabilir mi?

    API'nin ``halfs.firstHalfStarted`` alani varsa kesin zaman kontrolu yapilir.
    Alan yoksa yeni bir kayit tek goruntusune bakilarak canli sayilmaz; dakika,
    skor veya durum ikinci bir sorguda ilerlerse kabul edilir. Boylece servis
    restartinda donmus dünkü maclar yeniden sinyal uretemez.
    """
    now = time.time() if now is None else now
    status = match.get("status", {}) or {}
    halfs = status.get("halfs", {}) or {}
    started = halfs.get("firstHalfStarted")

    if started is not None:
        try:
            started = float(started)
            if started > 10_000_000_000:  # milisaniye timestamp
                started /= 1000
            age = now - started
            return -15 * 60 <= age <= MAX_PLAUSIBLE_LIVE_AGE_SECONDS
        except (TypeError, ValueError):
            pass

    event_id = str(match.get("id", ""))
    if not event_id:
        return False

    live_time = status.get("liveTime", {}) or {}
    signature = (
        live_time.get("short"),
        live_time.get("shortKey"),
        (match.get("home", {}) or {}).get("score"),
        (match.get("away", {}) or {}).get("score"),
        status.get("ongoing"),
    )
    previous = _feed_observations.get(event_id)
    if previous is None:
        _feed_observations[event_id] = {"signature": signature, "confirmed": False}
        return False

    if signature != previous["signature"]:
        previous["signature"] = signature
        previous["confirmed"] = True
    return previous["confirmed"]

from match_filter import is_known_match as _is_known_match


# --- Istatistik anahtari kesfi -------------------------------------------------
# Halihazirda ISLEDIGIMIZ anahtarlar. Bunun disinda gelen her anahtar loglanir ve
# bir tabloya yazilir; boylece API'nin sundugu ama kullanmadigimiz veriyi
# tahmin yurutmeden, olcerek ogreniriz.
BILINEN_ANAHTARLAR = {
    "BallPossesion", "expected_goals", "total_shots", "ShotsOnTarget", "corners",
}
_gorulen_anahtarlar = {}


def _bilinmeyen_anahtar_kaydet(key, vals):
    if not key or key in BILINEN_ANAHTARLAR:
        return
    # Ilk gorusunde logla, sonra sadece sayaci artir (log kirliligi olmasin)
    if key not in _gorulen_anahtarlar:
        _gorulen_anahtarlar[key] = 0
        print(f"🔍 YENI ISTATISTIK ANAHTARI: '{key}' -> ornek deger: {vals}", flush=True)
    _gorulen_anahtarlar[key] += 1


def _kesif_ozeti_yaz():
    """Gorulen bilinmeyen anahtarlari veritabanina yazar (tek yerde birikir)."""
    if not _gorulen_anahtarlar:
        return
    try:
        conn = connect()
        cur = conn.cursor()
        cur.execute("""CREATE TABLE IF NOT EXISTS stat_key_discovery (
            anahtar TEXT PRIMARY KEY, gorulme INTEGER, ilk_gorulme TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        for k, n in _gorulen_anahtarlar.items():
            cur.execute("""INSERT INTO stat_key_discovery (anahtar, gorulme) VALUES (?,?)
                           ON CONFLICT(anahtar) DO UPDATE SET gorulme = gorulme + excluded.gorulme""",
                        (k, n))
        conn.commit()
        conn.close()
        _gorulen_anahtarlar.clear()
    except Exception as e:
        print(f"⚠️  Kesif ozeti yazilamadi: {e}", flush=True)


# Hafıza havuzu
V4_HISTORY = {}

# ─────────────────────────────────────────────────────────────────────────
# KOTA BUTCESI (2026-09-10, kullanici karari: "%60'ini kullanabiliriz,
# 300K'ya ulasinca daha temkinli, aynen devam")
# ─────────────────────────────────────────────────────────────────────────
# RapidAPI abonelik yaniti X-RateLimit-Requests-Limit / -Remaining
# basliklarini gonderir (her istekte). Bunu her turda football-current-live
# yanitindan okuyoruz - restart-guvenli, bellekte sayac tutmaya gerek yok.
# used = limit - remaining. Kullanima gore MAX_STATS_PER_CYCLE kademesi:
#   used < 300K (%60)  -> 25  (agresif - tum takip edilen maclara istatistik)
#   300K-420K          -> 10  (temkinli)
#   >= 420K (%84)      -> 4   (minimum - sadece en kritik maclar)
# Baslik gelmezse (bazi planlar gondermez) orta kademe (10) - guvenli taraf.
QUOTA_LIMIT_VARSAYILAN = 500_000
QUOTA_ESIK_TEMKINLI = 300_000
QUOTA_ESIK_MINIMUM = 420_000
_quota_limit = None       # RapidAPI'nin bildirdigi aylik limit
_quota_remaining = None   # RapidAPI'nin bildirdigi kalan
_quota_baslik_uyarisi_verildi = False

# event_id -> son istatistik cekilen monotonik zaman. Rotasyon icin
# (Astra K2: eskiden hep "feed sirasindaki ilk 7" seciliyordu, ayni
# maclar surekli, gerisi hic istatistik almiyordu).
_son_stat_cekim = {}


def _kota_baslik_oku(headers):
    """RapidAPI'nin X-RateLimit-Requests-* basliklarini oku (buyuk/kucuk
    harf duyarsiz). Yoksa bir kez uyar, sessizce gec."""
    global _quota_limit, _quota_remaining, _quota_baslik_uyarisi_verildi
    lim = headers.get("x-ratelimit-requests-limit") or headers.get("X-RateLimit-Requests-Limit")
    rem = headers.get("x-ratelimit-requests-remaining") or headers.get("X-RateLimit-Requests-Remaining")
    try:
        if lim is not None:
            _quota_limit = int(lim)
        if rem is not None:
            _quota_remaining = int(rem)
    except (TypeError, ValueError):
        return
    if (_quota_limit is None or _quota_remaining is None) and not _quota_baslik_uyarisi_verildi:
        _quota_baslik_uyarisi_verildi = True
        print("⚠️  RapidAPI kota basligi (X-RateLimit-Requests-*) gelmiyor - "
              "istatistik butcesi orta kademede (10/tur) sabit tutulacak.", flush=True)


def _kota_kademesi():
    """Mevcut kota kullanimina gore tur basina istatistik butcesi."""
    if _quota_limit is not None and _quota_remaining is not None:
        used = _quota_limit - _quota_remaining
    else:
        return 10  # baslik yok - orta/guvenli kademe
    if used >= QUOTA_ESIK_MINIMUM:
        return 4
    if used >= QUOTA_ESIK_TEMKINLI:
        return 10
    return 25

def update_and_get_momentum(event_id, minute, shots, corners):
    current_ts = int(time.time())
    if event_id not in V4_HISTORY:
        V4_HISTORY[event_id] = deque(maxlen=20) 
        
    history = V4_HISTORY[event_id]
    history.append((minute, shots, corners, current_ts))
    
    target_minute = minute - 5
    best_snapshot = None
    min_diff = 999
    
    for snap in history:
        snap_min = snap[0]
        diff = abs(snap_min - target_minute)
        # 3 ile 8 dakika arası geçmişteki bir veriyi referans al
        if 3 <= (minute - snap_min) <= 8:
            if diff < min_diff:
                min_diff = diff
                best_snapshot = snap
                
    if best_snapshot:
        past_min, past_shots, past_corners, _ = best_snapshot
        delta_shots = max(0, shots - past_shots)
        delta_corners = max(0, corners - past_corners)
        return delta_shots, delta_corners, (minute - past_min)
        
    return 0, 0, 0

async def fetch_stats(session, match_id):
    """DUZELTME (2026-09-09/10, GPT-6 Astra ikinci-gorus - sinyal hacmi
    coktu, kok neden arastirmasi): basarisizlikta ESKIDEN [] donuyordu -
    _parse_stats([]) bunu "istek atildi, API sifir/bos stats dondu" ile
    AYNI sekilde isliyordu, yani "hic sormadik" ile "sorduk, gercekten
    sifir" birbirine karisiyordu (bkz. app/core/features.py:_g - ayni
    ayrimin ASAGI akista zaten TITIZLIKLE yapildigi, ustteki yorum
    2026-08-05 tarihli AYNI bug'a atif yapiyor). Artik basarisizlikta
    None donuyor - "bilmiyoruz" ile "sifir olcduk" ayri kalsin diye."""
    url = f"https://{HOST}/football-get-match-event-all-stats"
    params = {"eventid": match_id}
    try:
        async with session.get(url, params=params, headers=HEADERS, timeout=10) as resp:
            if resp.status == 200:
                data = await resp.json()
                if data.get("status") == "success":
                    return data.get("response", {}).get("stats", [])
    except Exception as e:
        print(f"Stats fetch error for {match_id}: {e}")
    return None

async def _no_stats():
    """MAX_STATS_PER_CYCLE butcesi disinda kalan maclar icin - ag istegi
    ATMADAN None doner (bkz. fetch_stats notu) - "hic sormadik" acikca
    isaretlenir, gather ayni sekle sahip kalir."""
    return None

_STAT_PCT_PREFIX = re.compile(r'^\s*(\d+(?:\.\d+)?)\s*(?:\(|$)')  # "12 (44%)" -> "12", "12" -> "12"
_STAT_PLAIN_NUM = re.compile(r'^\s*-?\d+(?:\.\d+)?\s*$')
_STAT_MISSING = {"", "-", "n/a", "na", "null", "none", "?"}


def _stat_one_side(v, want_float):
    """API'nin tutarsiz istatistik formatini TEK bir tarafin (ev VEYA
    deplasman) sayisal degerine cevirir. GEÇERSİZ veya EKSİK ise None
    doner - ASLA 0 (2026-09-10, GPT-6 Astra: sahte sifir = veri bozulmasi).

    Kabul:
      12, "12", "12.0", "12 (44%)"  -> sayi (yuzdeli formda BASTAKI sayi = adet)
    Ret (-> None):
      None, "", "-", "N/A"          -> eksik
      "NaN", "Infinity", "-5"       -> gecersiz (negatif sayim/xG olmaz)
      "12abc", "abc", "(44%)"       -> ayristirilamaz
      bool                          -> sayi degil (True/False sayi sayilmaz)
    """
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        x = float(v)
    elif isinstance(v, str):
        s = v.strip()
        if s.lower() in _STAT_MISSING:
            return None
        m = _STAT_PCT_PREFIX.match(s)
        if m:
            x = float(m.group(1))
        elif _STAT_PLAIN_NUM.match(s):
            x = float(s)
        else:
            return None
    else:
        return None
    # NaN / +-Inf / negatif ele
    if x != x or x in (float("inf"), float("-inf")) or x < 0:
        return None
    return x if want_float else int(x)


def _parse_stats(stats_groups):
    """API'nin istatistik yanitini (h_pos, a_pos, ..., h_big, a_big) 20'li
    tuple'ina cevirir. Saf fonksiyon - DB/ag yok, transaction disinda calisir.

    VERI BUTUNLUGU (2026-09-10, GPT-6 Astra kod incelemesi - onceki
    duzeltme [c116220] EKSIKTI):
      - TUM alanlar None baslar. Sadece DOGRULANMIS deger doldurulur.
        Desteklenmeyen/gelmeyen alan 0 DEGIL None kalir.
      - stats_groups None VEYA [] (istek atilmadi / bos yanit) -> hepsi None.
      - Iki taraf AYRI ayristirilir. Bir taraf eksik/gecersizse SADECE
        o taraf None, diger tarafin gecerli degeri KAYBOLMAZ.
      - "12 (44%)" -> 12 (adet). "NaN"/negatif/"12abc" -> None.
    features.py:_g() NULL kolonu zaten "veri yok" (insufficient_data)
    olarak okuyor - bu duzeltme yazici tarafindaki sahte sifiri kaldiriyor.
    """
    fields = ["h_pos", "a_pos", "h_xg", "a_xg", "h_shots", "a_shots",
              "h_sot", "a_sot", "h_sot_off", "a_sot_off", "h_danger", "a_danger",
              "h_atk", "a_atk", "h_cor", "a_cor", "h_red", "a_red", "h_big", "a_big"]
    f = {k: None for k in fields}

    if not stats_groups:  # None VEYA bos liste
        return tuple(f[k] for k in fields)

    # key -> (h_field, a_field, want_float)
    key_map = {
        "BallPossesion":  ("h_pos", "a_pos", False),
        "expected_goals": ("h_xg", "a_xg", True),
        "total_shots":    ("h_shots", "a_shots", False),
        "ShotsOnTarget":  ("h_sot", "a_sot", False),
        "corners":        ("h_cor", "a_cor", False),
    }

    for group in stats_groups:
        for item in group.get("stats", []):
            key = item.get("key")
            vals = item.get("stats")
            _bilinmeyen_anahtar_kaydet(key, vals)
            target = key_map.get(key)
            if not target or not isinstance(vals, (list, tuple)) or len(vals) < 2:
                continue
            hf, af, want_float = target
            hv = _stat_one_side(vals[0], want_float)
            av = _stat_one_side(vals[1], want_float)
            # Her tarafi BAGIMSIZ yaz - biri None olsa digeri kaydedilir.
            if hv is not None:
                f[hf] = hv
            if av is not None:
                f[af] = av

    return tuple(f[k] for k in fields)


def _ensure_match_tracking_schema(tries=5, pause=8):
    """Feed dalgalanmasinda maci tek turda kaybetmemek icin son gorulme alani.

    DUZELTME (2026-09-09): bu fonksiyon SADECE process baslarken bir kez
    calisir ama bugunku kronik yazma-kilidi altinda (bkz. CLAUDE.md kural 6b)
    ciplak UPDATE'leri hicbir koruma olmadan atiyordu - connect()'in 30sn'lik
    busy_timeout'u dahi dolunca sqlite3.OperationalError process'i CRASH
    ETTIRIYORDU (canli olcum: v4_api_bot her ~35sn'de bir crash-loop'a
    girdi, 3 ayri restart'ta da AYNI satirda). Baska her seyi (flashscore/
    sofascore) kapatip TEK yazici birakildiktan SONRA bile ayni sekilde
    coktu - yani sorun rakip yazici sayisindan degil, bu fonksiyonun hicbir
    tekrar deneme mantigi olmamasindan kaynaklaniyordu. Bugunku diger tum
    yazma noktalarinda (telegram_poster _mark_with_retry, vb.) zaten var
    olan "birkac kez dene, aralarda bekle" desenini burada da uyguluyoruz -
    tek seferlik bir acilis islemi oldugu icin birkac saniyelik ek bekleme
    zararsiz."""
    last_err = None
    for attempt in range(tries):
        try:
            conn = connect()
            try:
                conn.execute("ALTER TABLE matches ADD COLUMN last_seen_at TIMESTAMP")
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute("ALTER TABLE matches ADD COLUMN last_progress_at TIMESTAMP")
            except sqlite3.OperationalError:
                pass
            conn.execute("""
                UPDATE matches SET last_seen_at=CURRENT_TIMESTAMP
                WHERE last_seen_at IS NULL
                  AND status NOT IN ('FINISHED','ABANDONED','Ended','FT','Canceled')
            """)
            # last_progress_at'i simdi (deploy ani) ile baslatiyoruz - gecmise donuk
            # tahmin yapmiyoruz (hangi macin gercekten donuk oldugunu bilemeyiz, yanlis
            # damgalarsak duzgun ilerleyen bir maci da yanlislikla kapatiriz). Bu yuzden
            # halihazirda donmus maclar bu deploy'dan itibaren STALE_PROGRESS_MINUTES
            # sonra temizlenir - geriye degil, ileriye donuk bir koruma.
            conn.execute("""
                UPDATE matches SET last_progress_at=CURRENT_TIMESTAMP
                WHERE last_progress_at IS NULL
                  AND status NOT IN ('FINISHED','ABANDONED','Ended','FT','Canceled')
            """)
            conn.commit()
            conn.close()
            return
        except sqlite3.OperationalError as e:
            last_err = e
            try:
                conn.close()
            except Exception:
                pass
            print(f"⚠️  _ensure_match_tracking_schema deneme {attempt + 1}/{tries} "
                  f"basarisiz ({e}), {pause}sn sonra tekrar...", flush=True)
            if attempt < tries - 1:
                time.sleep(pause)
    print(f"❌ _ensure_match_tracking_schema {tries} denemede de basarisiz oldu, "
          f"bu turu atlıyorum (son hata: {last_err}). Process CRASH ETMEDEN "
          f"devam ediyor - bir sonraki dongude tekrar denenecek.", flush=True)


def _close_stale_missing(cursor, active_ids):
    """Sadece grace suresince feed'e donmeyen maclari kapatir."""
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
    """Feed'de GORUNMEYE DEVAM ETSE bile dakikasi ilerlemeyen maclari kapatir.

    _close_stale_missing feed'den tamamen dusen maclari yakalar; bu fonksiyon
    FARKLI bir sorunu yakalar - RapidAPI'nin bir fikstur icin surekli AYNI
    (bayat) veriyi dondurmesi. Boyle bir mac feed'de "goruluyor" oldugu icin
    last_seen_at hep tazelenir ve _close_stale_missing hicbir zaman devreye
    girmez; last_progress_at (SADECE dakika gercekten degistiginde tazelenir)
    burada devreye giriyor.

    Doner: kapatilan maclarin source_match_id listesi. Cagiran taraf bunlari
    bu ciklustaki 'matches' listesinden CIKARMALI - aksi halde ASAMA 4'teki
    upsert, API hala "ongoing: true" dedigi icin statusu hemen LIVE'a geri
    ceviriyordu (ayni ciklus icinde acilip kapanan bug buradan geliyordu).
    """
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


async def process_api_matches(session):
    """Doner: HTTP durum kodu (basarili=200), yoksa None (ag hatasi)."""
    url_live = f"https://{HOST}/football-current-live"
    try:
        async with session.get(url_live, headers=HEADERS, timeout=10) as resp:
            _kota_baslik_oku(resp.headers)
            if resp.status != 200:
                print(f"Failed to fetch live matches: {resp.status}")
                return resp.status
            data = await resp.json()
    except Exception as e:
        print(f"Error fetching live matches: {e}")
        return None

    _resp_raw = data.get("response", {})
    matches = _resp_raw
    if isinstance(matches, dict) and "live" in matches:
        matches = matches["live"]
    elif isinstance(matches, dict) and "matches" in matches:
        matches = matches["matches"]

    if not matches:
        matches = []

    # GECICI TESHIS (2026-09-10): kullanici "eskiden bu API gunde 150+ mac
    # donuyordu, simdi 7" diyor - kod Agustos'tan beri ayni. API'nin HAM
    # yanitini gormeden teshis edemiyoruz. Her turda ham yapiyi bas.
    try:
        _ust = (list(data.keys()) if isinstance(data, dict) else type(data).__name__)
        _resp_tip = (f"dict:{list(_resp_raw.keys())}" if isinstance(_resp_raw, dict)
                     else f"list:{len(_resp_raw)}" if isinstance(_resp_raw, list)
                     else type(_resp_raw).__name__)
        _ornek = ""
        if isinstance(matches, list) and matches and isinstance(matches[0], dict):
            _m0 = matches[0]
            _st = _m0.get("status", {}) or {}
            _ornek = (f" ornek: id={_m0.get('id')} lig={_m0.get('leagueId')} "
                      f"status_keys={list(_st.keys())} "
                      f"halfs={_st.get('halfs')} liveTime={_st.get('liveTime')}")
        print(f"🩺 API-HAM: data_keys={_ust} response={_resp_tip} "
              f"cikarilan_mac={len(matches) if hasattr(matches, '__len__') else '?'}"
              f"{_ornek}", flush=True)
    except Exception as _e:
        print(f"🩺 API-HAM teshis hatasi: {_e}", flush=True)

    # HAM feed'de gorunen TUM mac id'leri - onay/guven durumundan BAGIMSIZ.
    # Asagidaki plausibilite filtresi bir maci "henuz onaylanmadi" diye bir
    # kac tur disarida biraksa bile, mac feed'de GORUNMEYE devam ediyorsa
    # last_seen_at'ini tazelemek icin bunu kullaniyoruz - yoksa gercekten
    # canli ama imzasi birkac tur degismeyen (ornek: uzun sure ayni dakikada
    # gorunen bir mac) bir mac, sirf onay bekledigi icin grace-period'a
    # (MISSING_GRACE_MINUTES) yakalanip yanlislikla ABANDONED/FINISHED
    # olabiliyordu (bkz. Wuhan Three Towns vakasi).
    raw_active_ids = ["v4_" + str(m["id"]) for m in matches if "id" in m]

    # Halihazirda LIVE/HT olarak takip ettigimiz maclar icin "iki ardisik
    # sorguda imza degisti mi" onayi GEREKMEZ - DB'deki varliklari zaten
    # kalici bir kanit. Bu istisna olmadan, _feed_observations bellek-ici
    # oldugu icin her deploy/restart'ta TUM canli maclar aninda "yeni ve
    # onaysiz" sayiliyor, feed disina dusup last_seen_at'i donduruyor ve
    # grace-period saati gereksiz yere isliyordu.
    conn = connect()
    tracked_ids = {
        row[0] for row in conn.execute('''
            SELECT source_match_id FROM matches m
            WHERE status IN ('LIVE','HT')
              AND (
                  NOT EXISTS (
                      SELECT 1 FROM consensus_predictions p
                      WHERE p.match_id = m.id AND p.decision='signal'
                  )
                  OR EXISTS (
                      SELECT 1 FROM consensus_predictions p
                      WHERE p.match_id = m.id AND p.decision='signal' AND p.outcome IS NULL
                  )
              )
        ''').fetchall()
    }
    # NOT: status='LIVE/HT' olsa bile TUM sinyalleri zaten sonuclanmis (hic
    # PENDING kalmamis) bir mac tracked_ids'e DAHIL EDILMEZ. Bu, orchestrator
    # tarafindaki settlement.finalize_fully_settled_matches() ile ayni kurali
    # KAYNAGINDA da uygular - restart aninda iki surec (v4_api_bot, orchestrator)
    # BAGIMSIZ calistigi icin, finalize_fully_settled_matches() henuz calismadan
    # v4_api_bot bu tur bir maci "zaten canli, guven" diyip RapidAPI'nin
    # tasidigi stale/carried-over veriyle bir aninlığına yeniden canlandirabiliyordu
    # (dunku bir Konferans Ligi macinin her push'ta ekrana geri donmesi buradan
    # geliyordu). Kural artik HER IKI yerde de aninda gecerli.
    # last_seen_at'i SADECE feed'de gorunmeye devam eden maclar icin tazele -
    # onay durumundan bagimsiz. Bu, yukaridaki plausibilite filtresinin asil
    # amacini (stale/carried-over bir maci YENIDEN canli gibi ISLEMEYELIM)
    # bozmaz - o filtre hala matches listesini (asagida) daraltiyor, sadece
    # grace-period saatini "hala gorunuyor" bilgisiyle besliyoruz.
    if raw_active_ids:
        placeholders_raw = ','.join('?' for _ in raw_active_ids)
        conn.execute(f'''
            UPDATE matches SET last_seen_at=CURRENT_TIMESTAMP
            WHERE status IN ('LIVE','HT') AND source_match_id IN ({placeholders_raw})
        ''', raw_active_ids)
        conn.commit()
    conn.close()

    raw_match_count = len(matches)
    matches = [
        m for m in matches
        if ("v4_" + str(m.get("id", ""))) in tracked_ids or _plausibly_current_live_match(m)
    ]
    stale_or_unconfirmed = raw_match_count - len(matches)
    if stale_or_unconfirmed:
        print(
            f"🧹 Feed tazelik filtresi: {stale_or_unconfirmed} eski/ilerlemeyen "
            "kayit canli kabul edilmedi (last_seen_at yine de tazelendi).",
            flush=True,
        )

    if not raw_active_ids:
        # KRITIK: Canli feed TAMAMEN BOS (o an oynanan hicbir mac yok - orn. gece yarisi
        # Konferans Ligi maclari bittiginde). Eskiden fonksiyon burada "return" ile cikiyordu
        # ve asagidaki temizlik sorgusu HIC CALISMIYORDU; bu yuzden DB'deki tum maclar son
        # gorulen dakikalarinda (63', 64' gibi) SONSUZA KADAR 'LIVE' kaliyor, "Acik Bahisler"
        # sayfasindan hic dusmuyordu. Feed bossa acik kayitlari kapat; ancak erken
        # dakikada kaybolanlari gercek mac sonu gibi gostermeyip ABANDONED yap.
        conn = connect()
        cur0 = conn.cursor()
        closed = _close_stale_missing(cur0, [])
        closed_stale = _close_stale_progress(cur0)
        conn.commit()
        conn.close()
        print(f"ℹ️  Canli feed bos - grace suresi dolan {closed} mac, ilerlemeyen {len(closed_stale)} mac kapatildi.", flush=True)
        return

    # Onaylanmis/guvenilir maclarin id listesi - ASAMA 2+'deki islem hattinda
    # (yeni mac kaydi, istatistik cekme, sinyal uretimi) SADECE bunlar kullanilir.
    active_ids = ["v4_" + str(m["id"]) for m in matches if "id" in m]

    # Teshis sayaclari: her ciklusta feed'den kac mac geldi, kaci filtreye takildi,
    # kaci gercekten islendi -> Railway loglarindan net gorulsun diye.
    stat_feed_total = len(active_ids)
    stat_skipped_unknown = 0
    stat_skipped_cap = 0
    stat_processed = 0

    # --- ASAMA 1: KISA bir baglanti ile SADECE temizlik + okuma yap, hemen kapat. ---
    # NOT: bu baglanti asagidaki asama 2'nin ag cagrilarindan (fetch_stats) ONCE
    # commit edilip kapatiliyor - boylece yazma kilidi ag beklerken acik kalmiyor.
    conn = connect()
    cursor = conn.cursor()
    placeholders = ','.join('?' for _ in active_ids)
    # Bu sorgu SADECE canli feed'den tamamen DUSMUS (artik hic donmeyen) maclari kapsar.
    # Eskiden burada "dakika 40-50 arasindaysa HT yap" gibi bir kural vardi; bu, dakikasi
    # 40-50 araliginda donup kalmis (ornegin API'nin bir daha hic donmedigi) bir maci
    # SONSUZA KADAR 'HT' durumunda tutuyordu. 'HT' durumu "Mac Sonu" marketlerinde sonuc
    # olarak sayilmadigindan (sadece 'FINISHED' terminal sayilir), o mac PENDING'de takili
    # kaliyordu - hatta aylar/yillar sonra bile ("kiev macinin bir yil sonra hala acik
    # gozukmesi" bugu buradan geliyordu). Feed'den dusen bir mac artik takip edilmiyor
    # demektir, dogrudan FINISHED yapilmali.
    # Terminal olmayan HER durumu kapsa - sadece LIVE/HT degil.
    # Gecmiste ham metin durumlar ("2nd half", "Started") kaydedildigi icin
    # o satirlar hicbir zaman temizlenemiyordu.
    # raw_active_ids kullanilir (confirmed active_ids DEGIL): feed'de hala
    # gorunen ama henuz onaylanmamis bir mac burada YANLISLIKLA "dustu"
    # sayilip kapatilmasin - last_seen_at zaten yukarida tazelendigi icin
    # bu sorgu zaten onu es gececek, ama exception clause'u da tutarli olsun.
    _close_stale_missing(cursor, raw_active_ids)
    # Feed'de gorunmeye devam etse bile dakikasi ilerlemeyen (bayat veri
    # donen) maclari da kapat - _close_stale_missing bunlari YAKALAYAMAZ,
    # cunku feed'den "dusmus" sayilmazlar.
    closed_stale = _close_stale_progress(cursor)
    if closed_stale:
        print(f"🧊 {len(closed_stale)} mac dakikasi ilerlemedigi icin kapatildi (bayat feed verisi).", flush=True)
        # Az once kapattigimiz maclari BU CIKLUSTAKI matches listesinden de
        # cikar - yoksa asagidaki ASAMA 2-4, API hala "ongoing: true" dedigi
        # icin statusu upsert'te aninda LIVE'a geri cevirir (ayni ciklus
        # icinde acilip-kapanan bug tam olarak buradan geliyordu).
        closed_stale_set = set(closed_stale)
        matches = [m for m in matches if ("v4_" + str(m.get("id", ""))) not in closed_stale_set]
        # _feed_observations'taki "confirmed" hafizasini da sil - aksi halde
        # bir SONRAKI ciklusta ayni (hala donuk) veriyle geldiginde tracked_ids
        # bypass'i artik gecerli olmadigi icin (status artik LIVE/HT degil)
        # plausibilite kontrolune dusuyor ama ESKI "confirmed=True" hafizasi
        # sinyal degismeden onu tekrar iceri aliyordu - mac hicbir zaman
        # gercekten kapali KALMIYORDU. Hafiza silinince tekrar iceri girmek
        # icin GERCEK bir imza degisikligi (dakika ilerlemesi) sart olacak.
        for sid in closed_stale_set:
            raw_id = sid[3:] if sid.startswith("v4_") else sid
            _feed_observations.pop(raw_id, None)

    # Bu ciklustaki maclardan hangileri DB'de zaten var (halihazirda takip ediliyor)?
    # Zaten takip edilenler kota/filtre doldu diye BIRAKILMAZ - guncellenmeye devam eder.
    cursor.execute(f"SELECT source_match_id FROM matches WHERE source_match_id IN ({placeholders})", active_ids)
    existing_ids = set(row[0] for row in cursor.fetchall())
    conn.commit()
    conn.close()

    # --- ASAMA 2: HANGI maclarin islenecegini belirle. Sadece bellekte calisir,
    # DB baglantisi ACIK DEGIL - bu yuzden ag cagrilarindan once bitirmek guvenli. ---
    to_process = []
    for match in matches:
        match_id_api = str(match["id"])
        event_id = "v4_" + match_id_api
        home_name = match.get("home", {}).get("name", "Unknown")
        away_name = match.get("away", {}).get("name", "Unknown")
        home_id = match.get("home", {}).get("id")
        away_id = match.get("away", {}).get("id")

        home_logo = f"https://images.fotmob.com/image_resources/logo/teamlogo/{home_id}.png" if home_id else f"https://ui-avatars.com/api/?name={home_name.replace(' ', '+')}&background=1f2937&color=00e5ff"
        away_logo = f"https://images.fotmob.com/image_resources/logo/teamlogo/{away_id}.png" if away_id else f"https://ui-avatars.com/api/?name={away_name.replace(' ', '+')}&background=1f2937&color=00e5ff"

        score_h = match.get("home", {}).get("score", 0)
        score_a = match.get("away", {}).get("score", 0)
        total_goals = score_h + score_a

        # Lig bilgilerini al
        league_id_api = str(match.get("leagueId", ""))
        league_info = LEAGUES_CACHE.get(league_id_api, {"name": "Unknown League", "ccode": "INT", "logo": ""})

        # --- Bilindik mac / gunluk kota filtresi ---
        # Zaten takip edilen bir mac ise (existing_ids icinde) her zaman guncellenir.
        # YENI bir mac ise ve bilinen bir ligden/takimdan degilse, bu maci hic
        # isleme almadan atla (DB'ye yazma, stats sorgusu da atma - boylece
        # RapidAPI kotasi sadece anlamli maclarda harcanir).
        if event_id not in existing_ids:
            if not _is_known_match(league_info["name"], home_name, away_name):
                stat_skipped_unknown += 1
                continue

        stat_processed += 1

        status_data = match.get("status", {})
        import re
        live_time = status_data.get("liveTime", {}) or {}
        short_str = live_time.get("short", "0")
        short_key = live_time.get("shortKey", "")
        minute = 0
        minute_parsed_ok = False

        # HT TESPITI ongoing'den BAGIMSIZ: dakika kesif logu gosterdi ki API
        # devre arasinda bile "ongoing": true donduruyor - eskiden HT kontrolu
        # "if not is_ongoing" bloguyle korunuyordu, bu yuzden devre arasi maclar
        # hic HT'ye gecmiyor, LIVE + dakika 0'da takili kaliyordu.
        is_halftime = short_key == "halftime_short" or short_str in ["HT", "Halftime", "Devre"]

        try:
            num_str = re.sub(r'\D', '', short_str)
            if num_str:
                minute = int(num_str)
                minute_parsed_ok = True
        except Exception:
            pass

        if is_halftime:
            # Devre arasinda liveTime.short zaten "HT" (rakamsiz) oldugundan
            # yukaridaki regex hep basarisiz olur. Dakika olarak 0 yerine devrenin
            # sonunu (maxTime/basePeriod, ikisi de yoksa 45) kullan.
            ht_minute = live_time.get("maxTime") or live_time.get("basePeriod") or 45
            try:
                minute = int(ht_minute)
                minute_parsed_ok = True
            except (TypeError, ValueError):
                pass

        if not minute_parsed_ok:
            # YEDEK: ilk yari baslangic zaman damgasindan gecen sureyi hesapla.
            # halfs.firstHalfStarted'in gercek varligi/birimi henuz Railway
            # loglarinda dogrulanmadi - bu yuzden makul araliga (0-130dk) uymayan
            # sonuclar sessizce reddedilir, tahmine dayali yanlis dakika yazilmaz.
            try:
                started = (status_data.get("halfs") or {}).get("firstHalfStarted")
                if started:
                    gecen_dk = int((time.time() - float(started)) / 60)
                    if 0 <= gecen_dk <= 130:
                        minute = gecen_dk
                        minute_parsed_ok = True
            except Exception:
                pass

        if not minute_parsed_ok:
            # KESIF: dakika hicbir yontemle ayristirilamadi. Boyle bir maca 0 yazip
            # DB'deki GERCEK dakikayi EZMEK yerine (asagida ON CONFLICT'te minute
            # sadece basariyla ayristirildiyse guncelleniyor), API'nin bu durumda
            # gercekte ne gonderdigini TAHMIN etmeden OLCEREK ogreniyoruz -
            # istatistik anahtari kesfindeki yaklasimin aynisi.
            print(f"⏱️ DAKIKA AYRISTIRILAMADI: {home_name}-{away_name} "
                  f"skor={score_h}-{score_a} status={status_data}", flush=True)

        period_length = status_data.get("periodLength", 45)
        is_ongoing = status_data.get("ongoing", False)

        # 2. Yari Saati Bastan Baslayan Ligler Icin Duzeltme (Orn: MTK Budapeste)
        # API statusId: 3 (2. yari) dondurdugunde eger dakika 1-45 arasindaysa
        # uzerine 45 dakika ekle.
        status_id = match.get("statusId", 0)
        if status_id == 3 and 0 < minute <= 45 and period_length == 45:
            minute += 45
            minute_parsed_ok = True

        # Determine actual status
        match_status = "LIVE"
        if is_halftime:
            match_status = "HT"
        elif not is_ongoing:
            if short_str in ["FT", "Finished", "Ended", "Bitti"]:
                match_status = "FINISHED"
            elif not short_str or short_str == "0":
                match_status = "FINISHED" # If it's not ongoing and has no time, it's likely finished or hasn't started. For our purposes, it's inactive.
            else:
                # ONEMLI: Eskiden buraya API'nin ham metni ("2nd half", "Started",
                # "Halftime" gibi) oldugu gibi yaziliyordu. Temizlik sorgusu ise
                # sadece 'LIVE'/'HT' ariyordu; bu yuzden ham durumlu satirlar
                # SONSUZA KADAR temizlenemiyor ve "Tum Canlilar" sekmesinde
                # kaliciolarak takili kaliyordu. Artik her sey bilinen uc durumdan
                # birine indirgeniyor.
                match_status = "LIVE"

        target_market = f"İlk Yarı {total_goals + 0.5} Üst" if period_length == 45 and minute <= 45 else f"Maç Sonu {total_goals + 0.5} Üst"

        aggregate_score = status_data.get("aggregatedStr", "")

        to_process.append({
            "match_id_api": match_id_api,
            "event_id": event_id,
            "already_tracked": event_id in existing_ids,
            "home_name": home_name,
            "away_name": away_name,
            "home_logo": home_logo,
            "away_logo": away_logo,
            "score_h": score_h,
            "score_a": score_a,
            "league_info": league_info,
            "match_status": match_status,
            "minute": minute,
            "minute_parsed_ok": minute_parsed_ok,
            "aggregate_score": aggregate_score,
        })

    # --- ASAMA 3: TUM istatistikleri ESZAMANLI cek. Burada HICBIR DB baglantisi
    # acik degil - onceki halde tam da bu bekleme sirasinda (mac basina 10-20
    # saniye surebiliyordu, 22 mac * ag gecikmesi) yazma kilidi acik kaliyordu.
    #
    # NEDEN SINIRLANDIRILIYOR: es zamanli takip edilen mac sayisi sinirsiz
    # buyuyebiliyor; her biri icin ayri istatistik istegi atildigindan yogun
    # saatlerde RapidAPI hiz/kota sinirina carpip TUM istekleri 429'a
    # dusurebiliyor (gecmiste saatlerce sifir sinyal - bkz. git log).
    #
    # DINAMIK BUTCE (2026-09-10, kullanici karari + Astra K2/kota onerisi):
    # tur basina istatistik sayisi ARTIK SABIT DEGIL - RapidAPI'nin bildirdigi
    # gercek kota kullanimina gore kademeleniyor (bkz. _kota_kademesi):
    #   <300K (%60) -> 25/tur  |  300-420K -> 10  |  >=420K -> 4
    # Boylece rahat oldugumuzda tum takip edilen maclara istatistik cikiyor,
    # kota daralinca otomatik geri cekiliyor - "300K'ya ulasinca daha
    # temkinli, aynen devam" (kullanici, 2026-09-10).
    #
    # ROTASYON (Astra K2): eskiden "feed sirasindaki ilk 7" seciliyordu -
    # feed sirasi sabitse AYNI maclar surekli istatistik aliyor, gerisi HIC.
    # Artik: takip edilenler once, ONLARIN icinde EN UZUN SUREDIR istatistik
    # cekilmemis olan once (_son_stat_cekim).
    MAX_STATS_PER_CYCLE = _kota_kademesi()
    _simdi = time.monotonic()
    prioritized = sorted(
        to_process,
        key=lambda m: (not m["already_tracked"],
                       _son_stat_cekim.get(m["event_id"], 0.0)),
    )
    stats_targets = set(id(m) for m in prioritized[:MAX_STATS_PER_CYCLE])
    for m in prioritized[:MAX_STATS_PER_CYCLE]:
        _son_stat_cekim[m["event_id"]] = _simdi
    # _son_stat_cekim'i sinirla - artik canli olmayan maclarin kaydini at
    if len(_son_stat_cekim) > 500:
        _canli = {m["event_id"] for m in to_process}
        for k in [k for k in _son_stat_cekim if k not in _canli]:
            _son_stat_cekim.pop(k, None)

    stats_results = await asyncio.gather(
        *[fetch_stats(session, m["match_id_api"]) if id(m) in stats_targets
          else _no_stats() for m in to_process],
        return_exceptions=True,
    )
    for m, stats_groups in zip(to_process, stats_results):
        if isinstance(stats_groups, Exception):
            # bkz. fetch_stats/_parse_stats notu - beklenmedik bir istisna
            # da "bilmiyoruz" sayilir, sahte sifir degil.
            stats_groups = None
        m["stats"] = _parse_stats(stats_groups)

    # --- ASAMA 4: TEK KISA transaction'da hepsini yaz, hemen kapat. Ag cagrisi
    # bitmis, elde sadece bellekteki sonuclar var - bu blokta hic "await" yok. ---
    # DUZELTME (2026-09-09/10 gece, /goal oturumu): ciplak connect()/commit()
    # yerine measured_write() - BEGIN IMMEDIATE + olcum/log + otomatik
    # rollback-on-error, bugun diger yazma noktalarinda kanitlanmis desen.
    # Davranis DEGISMEDI (ayni sorgular, ayni sira) - sadece transaction
    # yonetimi ve gozlemlenebilirlik guclendirildi.
    with measured_write("v4_api_bot.write_matches", batch_size=len(to_process)) as conn:
        cursor = conn.cursor()

        for m in to_process:
            # NOT: Buraya bir ara "mac bir kere FINISHED olduysa bir daha asla guncellenmesin"
            # kilidi konulmustu. O kilit KALDIRILDI: yukaridaki "feed tamamen bos ise hepsini
            # FINISHED yap" temizligiyle birlesince, feed'de tek seferlik gecici bir bosluk
            # olusmasi durumunda o an gercekten oynanan tum maclar kalici olarak olu sayilacak
            # ve bir daha asla guncellenmeyeceklerdi. Gecici olarak yanlis FINISHED olmus bir mac
            # geri donebilmeli; nasil olsa bitince tekrar dogru sekilde sonuclanir.
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
                    aggregate_score=excluded.aggregate_score,
                    last_seen_at=CURRENT_TIMESTAMP,
                    -- last_progress_at SADECE dakika GERCEKTEN degistiyse tazelenir.
                    -- Feed'de gorunmeye devam edip dakikasi hic ilerlemeyen bir mac
                    -- (RapidAPI'nin bayat veri dondurdugu fikstur), last_seen_at surekli
                    -- tazelense bile burada eskiyip _close_stale_progress'e yakalanir.
                    last_progress_at=CASE
                        WHEN (CASE WHEN ? THEN excluded.minute ELSE matches.minute END) IS NOT matches.minute
                        THEN CURRENT_TIMESTAMP
                        ELSE matches.last_progress_at
                    END
            ''', (m["event_id"], m["home_name"], m["away_name"], m["match_status"],
                  m["league_info"]["name"], m["league_info"]["ccode"], m["league_info"]["logo"],
                  m["score_h"], m["score_a"], m["minute"], m["home_logo"], m["away_logo"],
                  m["aggregate_score"], m["minute_parsed_ok"], m["minute_parsed_ok"]))

            cursor.execute('SELECT id FROM matches WHERE source_match_id = ?', (m["event_id"],))
            match_id_db_res = cursor.fetchone()
            if not match_id_db_res:
                continue
            match_id_db = match_id_db_res[0]

            (h_pos, a_pos, h_xg, a_xg, h_shots, a_shots, h_sot, a_sot,
             h_sot_off, a_sot_off, h_danger, a_danger, h_atk, a_atk, h_cor, a_cor,
             h_red, a_red, h_big, a_big) = m["stats"]

            # Veritabanına canlı anlık görüntü (snapshot) kaydet
            cursor.execute('''
                INSERT INTO live_snapshots (
                    match_id, minute, period, home_score, away_score,
                    home_possession, away_possession, home_xg, away_xg,
                    home_shots, away_shots, home_shots_on_target, away_shots_on_target,
                    home_shots_off_target, away_shots_off_target,
                    home_dangerous_attacks, away_dangerous_attacks,
                    home_attacks, away_attacks,
                    home_corners, away_corners,
                    home_red_cards, away_red_cards,
                    home_big_chances, away_big_chances
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (match_id_db, m["minute"], 'first_half' if m["minute"] <= 45 else 'second_half',
                  m["score_h"], m["score_a"], h_pos, a_pos, h_xg, a_xg, h_shots, a_shots, h_sot, a_sot,
                  h_sot_off, a_sot_off, h_danger, a_danger, h_atk, a_atk, h_cor, a_cor,
                  h_red, a_red, h_big, a_big))

        cursor.execute("SELECT COUNT(*) FROM matches "
                       "WHERE status NOT IN ('FINISHED','ABANDONED','Ended','FT','Canceled')")
        still_open = cursor.fetchone()[0]


    _kesif_ozeti_yaz()

    _kota_str = ""
    if _quota_limit is not None and _quota_remaining is not None:
        _used = _quota_limit - _quota_remaining
        _kota_str = (f" | kota={_used}/{_quota_limit} "
                     f"(%{100 * _used // _quota_limit}) stat_butce={MAX_STATS_PER_CYCLE}/tur")
    print(
        f"📊 feed={stat_feed_total} islenen={stat_processed} "
        f"atlanan(taninmayan)={stat_skipped_unknown} atlanan(kota)={stat_skipped_cap} "
        f"| DB'de hala acik(LIVE/HT)={still_open}{_kota_str}",
        flush=True,
    )
    return 200

def _ensure_team_profiles():
    """team_profiles bossa hemen kurar - orchestrator'in acilisini beklemez.

    v4_api_bot ve orchestrator ayri surecler. Ikisi de takim eslestirmesi
    icin team_profiles'a bagli (bkz. prematch._get_index); orchestrator henuz
    ayaga kalkmadan v4_api_bot mac cektiginde tablo bos oluyor ve _is_known_match
    tum maclari 'taninmayan' sayip atliyordu.
    """
    try:
        import prematch
        conn = connect()
        count = conn.execute("SELECT COUNT(*) FROM team_profiles").fetchone()[0] if _table_exists(conn, "team_profiles") else 0
        conn.close()
        if count == 0:
            print("ℹ️  team_profiles bos, kuruluyor...", flush=True)
            prematch.build_profiles()
    except Exception as e:
        print(f"⚠️  team_profiles kurulamadi: {e}", flush=True)


def _table_exists(conn, name):
    row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    return row is not None


NORMAL_CYCLE_SECONDS = 60  # 2026-09-09: 500K/ay kotasi icin dusuruldu, bkz. KOTA HESABI notu yukarida
# RapidAPI 429 (kota/hiz siniri) dondugunde HER 15 saniyede tekrar denemek
# hem kotayi (eger basarisiz istekler de sayiliyorsa) bosa harciyor hem de
# kota gercekten tukenmisse iyilesmeyi geciktirebiliyor. Ardisik 429'larda
# bekleme suresi katlanarak artiyor (15sn -> 30 -> 60 -> ... -> 10dk tavan),
# ilk basarili cevapta hemen normale donuyor.
MAX_BACKOFF_SECONDS = 600


async def main():
    print("🚀 Starting V4 OFFICIAL API Radar Bot...", flush=True)
    _ensure_match_tracking_schema()
    _ensure_team_profiles()
    consecutive_429s = 0
    async with aiohttp.ClientSession() as session:
        while True:
            start_time = time.time()
            print("📡 Fetching RapidAPI Live matches...", flush=True)
            # DUZELTME (2026-09-09): process_api_matches() icinde BIRDEN FAZLA
            # ciplak connect()/execute() var (bu dosya bugunku DB-kilit
            # sertlestirmelerinden ONCE yazildi, kota tukenip devre disi
            # birakildigi icin dokunulmamisti). Bugun canli olcum: kronik
            # yazma-kilidi altinda bunlardan HERHANGI biri sqlite3.
            # OperationalError firlatinca (busy_timeout 30sn dolunca) tum
            # process CRASH ediyordu - hicbir try/except olmadigi icin
            # buraya kadar yukseliyordu (bkz. git log, "database is locked"
            # crash-loop). Turler arasi try/except: bir turun basarisiz DB
            # yazmasi, sonraki turu engellemesin - "bos donus" gibi ele
            # alinip bir sonraki dongude tekrar denenir.
            try:
                status = await process_api_matches(session)
            except Exception as e:
                print(f"⚠️  V4 cycle basarisiz (crash etmeden atlaniyor): {e}", flush=True)
                status = None
            elapsed = time.time() - start_time

            if status == 429:
                consecutive_429s += 1
                wait = min(NORMAL_CYCLE_SECONDS * (2 ** consecutive_429s), MAX_BACKOFF_SECONDS)
                print(f"⏳ RapidAPI 429 (kota/hiz siniri) - {consecutive_429s}. ardisik hata, "
                      f"{wait}sn bekleniyor (kotayi bosa harcamamak icin geri cekiliyor)", flush=True)
            else:
                if consecutive_429s > 0:
                    print(f"✅ RapidAPI tekrar cevap veriyor ({consecutive_429s} ardisik 429'dan sonra), "
                          "normal 15sn dongusune donuluyor", flush=True)
                consecutive_429s = 0
                wait = NORMAL_CYCLE_SECONDS

            print(f"✅ V4 Cycle completed in {elapsed:.2f} seconds. Waiting {wait}s...", flush=True)
            await asyncio.sleep(wait)

if __name__ == '__main__':
    asyncio.run(main())
