import asyncio
import time
import sqlite3
import os
import aiohttp
from collections import deque

from db_config import DB_PATH  # Railway kalici disk destegi (bkz. db_config.py)
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

# --- "Bilindik" lig/takım filtresi + günlük kota -----------------------------------------
# Amaç: RapidAPI sorgu kotasını, özellikle sezon dışı dönemde (çok sayıda alt lig / rezerv /
# altyapı maçı aynı anda oynanıyor) israf etmemek. Sadece tanınan büyük liglerin/takımların
# maçlarını takibe al, günde en fazla DAILY_MATCH_CAP kadar YENİ maç ekle. Zaten takip
# edilmekte olan bir maç, kota dolsa bile GÜNCELLENMEYE devam eder - yoksa yarım kalıp
# sonsuza kadar "PENDING" takılma bugu geri gelir.
DAILY_MATCH_CAP = 60

KNOWN_LEAGUE_NAMES = {
    "champions league", "europa league", "conference league",
    "uefa champions league", "uefa europa league", "uefa conference league",
}

KNOWN_TEAMS = {
    # Süper Lig
    "galatasaray", "fenerbahçe", "fenerbahce", "beşiktaş", "besiktas", "trabzonspor",
    "başakşehir", "basaksehir", "adana demirspor", "antalyaspor", "kasımpaşa", "kasimpasa",
    "kayserispor", "konyaspor", "sivasspor", "alanyaspor", "gaziantep", "çaykur rizespor", "rizespor",
    "göztepe", "goztepe", "samsunspor", "eyüpspor", "eyupspor", "kocaelispor",
    "gençlerbirliği", "genclerbirligi", "karagümrük", "karagumruk",
    # Premier League
    "manchester united", "manchester city", "liverpool", "chelsea", "arsenal", "tottenham",
    "newcastle", "aston villa", "west ham", "brighton", "everton", "wolverhampton", "wolves",
    "crystal palace", "fulham", "brentford", "nottingham forest", "bournemouth", "burnley",
    "leeds united", "sunderland",
    # La Liga
    "real madrid", "barcelona", "atletico madrid", "atlético madrid", "sevilla", "real sociedad",
    "real betis", "villarreal", "athletic bilbao", "athletic club", "valencia", "girona",
    "celta vigo", "osasuna", "getafe", "mallorca", "rayo vallecano", "alaves", "alavés",
    "las palmas", "espanyol", "levante", "elche", "real oviedo",
    # Serie A
    "juventus", "inter milan", "internazionale", "ac milan", "napoli", "as roma", " roma",
    "lazio", "atalanta", "fiorentina", "bologna", "torino", "udinese", "sassuolo", "genoa",
    "cagliari", "parma", "hellas verona", "lecce", "empoli", "como 1907", "cremonese", "pisa",
    # Bundesliga
    "bayern münchen", "bayern munich", "borussia dortmund", "rb leipzig", "bayer leverkusen",
    "eintracht frankfurt", "wolfsburg", "mönchengladbach", "monchengladbach", "union berlin",
    "freiburg", "hoffenheim", "mainz", "fc augsburg", "vfb stuttgart", "werder bremen",
    "köln", "koln", "heidenheim", "st. pauli", "hamburger sv",
    # Ligue 1
    "paris saint-germain", "psg", "marseille", "monaco", "olympique lyonnais", "lille",
    "nice", "rennes", "lens", "strasbourg", "toulouse", "nantes", "reims", "montpellier",
    "brest", "le havre", "angers", "auxerre", "metz", "paris fc",
}


def _arsivde_var_mi(takim_adi):
    """Takim, İddaa arsivimizden cikarilan profillerde var mi?

    Elle takim adi listesi tutmak yerine VERIYE dayali bir olcut: bir takim
    33 bin maclik bultene girmisse, zaten takip edilmeye deger tanidik bir
    takimdir. Bu, listeyi elle guncelleme zahmetini ortadan kaldirir ve
    Iddaa'nin listeledigi tum ligleri otomatik kapsar.

    Sonuc team_aliases tablosunda onbelleklenir, her ciklusta yeniden aranmaz.
    """
    try:
        import prematch
        return prematch.resolve_team(takim_adi) is not None
    except Exception:
        return False


def _is_known_match(league_name, home_name, away_name):
    """Mac takip edilmeye deger mi?

    Uc kademe:
      1. Lig adi tanidik bir turnuva mi (Sampiyonlar/Avrupa/Konferans Ligi)
      2. Takim adi elle tutulan buyuk kulup listesinde mi
      3. Takim Iddaa arsivinde var mi (veriye dayali, en genis kapsam)
    """
    ln = (league_name or "").strip().lower()
    if ln in KNOWN_LEAGUE_NAMES:
        return True

    hn = (home_name or "").strip().lower()
    an = (away_name or "").strip().lower()
    if any(kw in hn or kw in an for kw in KNOWN_TEAMS):
        return True

    # Arsiv kontrolu - en genis ag. Iki takimdan biri yetiyor.
    return _arsivde_var_mi(home_name) or _arsivde_var_mi(away_name)


_daily_state = {"date": None, "count": 0}


def _daily_cap_available():
    """Gunluk YENI mac kotasini kontrol eder, gun degisince otomatik resetler (TR saatine gore)."""
    import datetime
    try:
        from zoneinfo import ZoneInfo
        today = datetime.datetime.now(ZoneInfo("Europe/Istanbul")).date()
    except Exception:
        today = datetime.date.today()
    if _daily_state["date"] != today:
        _daily_state["date"] = today
        _daily_state["count"] = 0
    return _daily_state["count"] < DAILY_MATCH_CAP


def _register_new_match():
    _daily_state["count"] += 1


# Hafıza havuzu
V4_HISTORY = {}

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
    return []

async def process_api_matches(session):
    url_live = f"https://{HOST}/football-current-live"
    try:
        async with session.get(url_live, headers=HEADERS, timeout=10) as resp:
            if resp.status != 200:
                print(f"Failed to fetch live matches: {resp.status}")
                return
            data = await resp.json()
    except Exception as e:
        print(f"Error fetching live matches: {e}")
        return
        
    matches = data.get("response", {})
    if isinstance(matches, dict) and "live" in matches:
        matches = matches["live"]
    elif isinstance(matches, dict) and "matches" in matches:
        matches = matches["matches"]

    if not matches:
        matches = []

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Extract all currently active match IDs
    active_ids = ["v4_" + str(m["id"]) for m in matches if "id" in m]

    if not active_ids:
        # KRITIK: Canli feed TAMAMEN BOS (o an oynanan hicbir mac yok - orn. gece yarisi
        # Konferans Ligi maclari bittiginde). Eskiden fonksiyon burada "return" ile cikiyordu
        # ve asagidaki temizlik sorgusu HIC CALISMIYORDU; bu yuzden DB'deki tum maclar son
        # gorulen dakikalarinda (63', 64' gibi) SONSUZA KADAR 'LIVE' kaliyor, "Acik Bahisler"
        # sayfasindan hic dusmuyordu. Feed bossa, canli gorunen her mac bitmis demektir.
        cursor.execute("UPDATE matches SET status = 'FINISHED' "
                       "WHERE status NOT IN ('FINISHED','Ended','FT','Canceled')")
        conn.commit()
        conn.close()
        print("ℹ️  Canli mac yok - acik kalan tum maclar FINISHED yapildi.", flush=True)
        return

    # Teshis sayaclari: her ciklusta feed'den kac mac geldi, kaci filtreye takildi,
    # kaci gercekten islendi -> Railway loglarindan net gorulsun diye.
    stat_feed_total = len(active_ids)
    stat_skipped_unknown = 0
    stat_skipped_cap = 0
    stat_processed = 0

    if active_ids:
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
        cursor.execute(f'''
            UPDATE matches
            SET status = 'FINISHED'
            WHERE status NOT IN ('FINISHED', 'Ended', 'FT', 'Canceled')
              AND source_match_id NOT IN ({placeholders})
        ''', active_ids)

        # Bu ciklustaki maclardan hangileri DB'de zaten var (halihazirda takip ediliyor)?
        # Zaten takip edilenler kota/filtre doldu diye BIRAKILMAZ - guncellenmeye devam eder.
        cursor.execute(f"SELECT source_match_id FROM matches WHERE source_match_id IN ({placeholders})", active_ids)
        existing_ids = set(row[0] for row in cursor.fetchall())
    else:
        existing_ids = set()
    
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
        # YENI bir mac ise: bilinen bir ligden/takimdan degilse VEYA gunluk 30 mac kotasi
        # dolmussa, bu maci hic isleme almadan atla (DB'ye yazma, stats sorgusu da atma -
        # boylece RapidAPI kotasi sadece anlamli maclarda harcanir).
        if event_id not in existing_ids:
            if not _is_known_match(league_info["name"], home_name, away_name):
                stat_skipped_unknown += 1
                continue
            if not _daily_cap_available():
                stat_skipped_cap += 1
                continue
            _register_new_match()

        stat_processed += 1

        status_data = match.get("status", {})
        import re
        short_str = "0"
        try:
            short_str = status_data.get("liveTime", {}).get("short", "0")
            num_str = re.sub(r'\D', '', short_str)
            minute = int(num_str) if num_str else 0
        except:
            minute = 0
            
        period_length = status_data.get("periodLength", 45)
        is_ongoing = status_data.get("ongoing", False)
        
        # Determine actual status
        match_status = "LIVE"
        if not is_ongoing:
            if short_str in ["HT", "Halftime", "Devre"]:
                match_status = "HT"
            elif short_str in ["FT", "Finished", "Ended", "Bitti"]:
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
        
        # NOT: Buraya bir ara "mac bir kere FINISHED olduysa bir daha asla guncellenmesin"
        # kilidi konulmustu. O kilit KALDIRILDI: yukaridaki "feed tamamen bos ise hepsini
        # FINISHED yap" temizligiyle birlesince, feed'de tek seferlik gecici bir bosluk
        # olusmasi durumunda o an gercekten oynanan tum maclar kalici olarak olu sayilacak
        # ve bir daha asla guncellenmeyeceklerdi. Gecici olarak yanlis FINISHED olmus bir mac
        # geri donebilmeli; nasil olsa bitince tekrar dogru sekilde sonuclanir.
        cursor.execute('''
            INSERT INTO matches 
            (source_match_id, home_team_id, away_team_id, status, league_name, league_ccode, league_logo, home_score, away_score, minute, home_team_logo, away_team_logo, aggregate_score)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_match_id) DO UPDATE SET
                minute=excluded.minute,
                status=excluded.status,
                home_score=excluded.home_score,
                away_score=excluded.away_score,
                league_name=excluded.league_name,
                league_ccode=excluded.league_ccode,
                league_logo=excluded.league_logo,
                home_team_logo=excluded.home_team_logo,
                away_team_logo=excluded.away_team_logo,
                aggregate_score=excluded.aggregate_score
        ''', (event_id, home_name, away_name, match_status, league_info["name"], league_info["ccode"], league_info["logo"], score_h, score_a, minute, home_logo, away_logo, aggregate_score))
                       
        cursor.execute('SELECT id FROM matches WHERE source_match_id = ?', (event_id,))
        match_id_db_res = cursor.fetchone()
        if not match_id_db_res:
            continue
        match_id_db = match_id_db_res[0]
        
        # İstisnai analiz (İstatistikleri Çek)
        stats_groups = await fetch_stats(session, match_id_api)
        
        # Detaylı istatistikler
        h_pos, a_pos = 0, 0
        h_xg, a_xg = 0.0, 0.0
        h_shots, a_shots = 0, 0
        h_sot, a_sot = 0, 0
        h_cor, a_cor = 0, 0
        
        for group in stats_groups:
            for item in group.get("stats", []):
                key = item.get("key")
                vals = item.get("stats", [0, 0])
                
                # Check if vals is valid
                if isinstance(vals, list) and len(vals) >= 2 and vals[0] is not None and vals[1] is not None:
                    if key == "BallPossesion":
                        try: h_pos, a_pos = int(vals[0]), int(vals[1])
                        except: pass
                    elif key == "expected_goals":
                        try: h_xg, a_xg = float(vals[0]), float(vals[1])
                        except: pass
                    elif key == "total_shots":
                        try: h_shots, a_shots = int(vals[0]), int(vals[1])
                        except: pass
                    elif key == "ShotsOnTarget":
                        try: h_sot, a_sot = int(vals[0]), int(vals[1])
                        except: pass
                    elif key == "corners":
                        try: h_cor, a_cor = int(vals[0]), int(vals[1])
                        except: pass

        # Veritabanına canlı anlık görüntü (snapshot) kaydet
        cursor.execute('''
            INSERT INTO live_snapshots (
                match_id, minute, period, home_score, away_score,
                home_possession, away_possession, home_xg, away_xg,
                home_shots, away_shots, home_shots_on_target, away_shots_on_target,
                home_corners, away_corners
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (match_id_db, minute, 'first_half' if minute <= 45 else 'second_half', 
              score_h, score_a, h_pos, a_pos, h_xg, a_xg, h_shots, a_shots, h_sot, a_sot, h_cor, a_cor))

    cursor.execute("SELECT COUNT(*) FROM matches "
                   "WHERE status NOT IN ('FINISHED','Ended','FT','Canceled')")
    still_open = cursor.fetchone()[0]

    conn.commit()
    conn.close()

    print(
        f"📊 feed={stat_feed_total} islenen={stat_processed} "
        f"atlanan(taninmayan)={stat_skipped_unknown} atlanan(kota)={stat_skipped_cap} "
        f"| DB'de hala acik(LIVE/HT)={still_open}",
        flush=True,
    )

async def main():
    print("🚀 Starting V4 OFFICIAL API Radar Bot...", flush=True)
    async with aiohttp.ClientSession() as session:
        while True:
            start_time = time.time()
            print("📡 Fetching RapidAPI Live matches...", flush=True)
            await process_api_matches(session)
            elapsed = time.time() - start_time
            print(f"✅ V4 Cycle completed in {elapsed:.2f} seconds. Waiting 15s...", flush=True)
            await asyncio.sleep(15)

if __name__ == '__main__':
    asyncio.run(main())
