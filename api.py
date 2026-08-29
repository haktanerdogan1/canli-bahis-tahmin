from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import sqlite3
import os
import json
import re
import secrets
import time
import unicodedata
from collections import defaultdict, deque
from urllib.parse import urlencode

import requests

import auth
import settlement
import odds as odds_mod
import prematch
from match_filter import is_known_match

app = FastAPI(title="Canlı Gol Olasılığı API")

# users / app_config tablolarini hazirla (idempotent)
auth.init_auth_schema()


def _ensure_prediction_schema():
    """Sema garantisi tek yerden (settlement.ensure_schema) yonetilir.

    api.py bu sutunlari SORGULADIGI icin, orkestrator henuz hic calismamis olsa
    bile (ilk deploy / temiz veritabani) API'nin cokmemesi gerekiyor.
    """
    # API'nin SORGULADIGI her tablonun varligini API'nin KENDISI garanti etmeli.
    # Aksi halde orkestrator henuz hic calismamissa (ilk deploy / temiz veritabani)
    # uclar 500 veriyor. Daha once ayni hata signal_minute kolonunda yasandi.
    for ad, fn in (("settlement", settlement.ensure_schema),
                   ("prematch", prematch.ensure_schema),
                   ("odds", odds_mod.ensure_schema)):
        try:
            fn()
        except Exception as e:
            print(f"Sema kontrolu atlandi ({ad}): {e}")

    # Performans indeksleri (2026-08-28): /api/live-matches 9-19sn suruyordu -
    # bot_predictions/live_snapshots hic indekslenmemisti, sorgudaki iki
    # korelasyonlu alt-sorgu (ilk yari bitis skoru + lead_bot) her satir icin
    # tam tablo taramasi yapiyordu. CREATE INDEX IF NOT EXISTS idempotent,
    # veriye/davranisa dokunmaz - sadece SQLite'in ayni sorguyu indeks
    # uzerinden bulmasini saglar.
    try:
        conn = connect()
        cur = conn.cursor()
        cur.execute("CREATE INDEX IF NOT EXISTS ix_live_snapshots_match_minute ON live_snapshots(match_id, minute)")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_bot_predictions_match_snapshot_decision ON bot_predictions(match_id, snapshot_id, decision, probability)")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_consensus_predictions_created ON consensus_predictions(created_at DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_consensus_predictions_match ON consensus_predictions(match_id, snapshot_id)")
        # 2026-08-29: live_sync()'teki capraz-kaynak duplikasyon kontrolu
        # (bkz. _normalize_team_name) "matches WHERE status IN ('LIVE','HT')"
        # sorguyor - indekssiz olunca matches tablosunun (148k+ satir) TAMAMINI
        # her live-sync cagrisinda (15-20sn'de bir) taramaya basladi, genel
        # yavaslamaya (tekrar) yol acti. Ayni idempotent CREATE INDEX deseni.
        cur.execute("CREATE INDEX IF NOT EXISTS ix_matches_status ON matches(status)")
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Performans indeksleri atlandi: {e}")

SESSION_COOKIE = "jcode_session"


def current_user_id(request: Request):
    """Istekteki oturum cookie'sinden kullanici id'si cikarir; yoksa None.

    Yan etki: gecerli bir kullaniciysa "son gorulme" zamanini gunceller
    (bkz. auth.touch_last_seen - dakikada bir kereye kadar throttled) ki
    yonetici panelinde kimin aktif oldugu gorulebilsin."""
    uid = auth.verify_session_token(request.cookies.get(SESSION_COOKIE, ""))
    if uid is not None:
        auth.touch_last_seen(uid)
    return uid

# NOT: Eskiden allow_origins=["*"] + allow_credentials=True vardi. Bu ikisi birlikte
# hem tarayici tarafindan reddedilir hem de cookie tabanli oturumla birlesince baska
# sitelerin kullanicinin oturumuyla istek atmasina zemin hazirlar (CSRF). Frontend
# zaten ayni sunucudan (ayni origin) servis edildigi icin CORS'a ihtiyac yok.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Guvenlik basliklari (2026-08-28, lansman oncesi tarama sonrasi eklendi -
# bkz. Mozilla HTTP Observatory: X-Frame-Options/CSP/X-Content-Type-Options
# hicbiri yoktu). CSP'de 'unsafe-inline' bilerek var: sayfalar (index.html/
# mobile_app.html/admin.html) yaygin sekilde inline <script>, inline <style>
# ve onclick="..." kullaniyor - bunlari nonce/hash'e cevirmeden 'unsafe-inline'
# kaldirilirsa TUM ON YUZ CALISMAZ HALE GELIR. img-src bilerek genis (https+data):
# takim logolari/avatarlar cok sayida farkli CDN'den (flashscore, ui-avatars,
# google) geliyor, sabit bir liste kirilgan olur.
_SECURITY_HEADERS = {
    "X-Frame-Options": "SAMEORIGIN",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://cdnjs.cloudflare.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdnjs.cloudflare.com; "
        "font-src 'self' https://fonts.gstatic.com https://cdnjs.cloudflare.com; "
        "img-src 'self' https: data:; "
        "connect-src 'self'; "
        "frame-ancestors 'self'; "
        "object-src 'none'; "
        "base-uri 'self'"
    ),
}


@app.middleware("http")
async def _add_security_headers(request: Request, call_next):
    response = await call_next(request)
    for k, v in _SECURITY_HEADERS.items():
        response.headers[k] = v
    return response

# "Devre yaziyor / eski veri gorunuyor" gibi sikayetlerin bir kismi tarayici/CDN'in bu
# endpoint'leri (ve index.html'i) agresifce cache'lemesinden kaynaklaniyordu - biz Cache-Control
# header'i hic set etmiyorduk, bu da bazi tarayicilarin/ara katmanlarin GET isteklerini
# sessizce eski haliyle sunmasina yol aciyordu. Her yanitta acikca "cache'leme" diyoruz.
@app.middleware("http")
async def no_cache_headers(request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response

from db_config import DB_PATH, connect  # Railway kalici disk destegi (bkz. db_config.py)

_ensure_prediction_schema()

@app.get("/")
def serve_index():
    from fastapi.responses import FileResponse
    return FileResponse(os.path.join(os.path.dirname(__file__), 'index.html'))

@app.get("/robots.txt")
def serve_robots_txt():
    # Kullanici talebi (2026-08-28): baskalari bizden veri kazimasin. Bu sadece
    # kurallara uyan botlari (Google vb.) durdurur - kararli bir kazici zaten
    # robots.txt'yi yok sayar, asil koruma _scrape_guarded() rate limiti.
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(
        "User-agent: *\nDisallow: /api/\nDisallow: /admin\n"
    )

@app.get("/app")
def serve_mobile_app():
    """Cyberpunk temali, ozgun tasarimli mobil arayuz - index.html'den TAMAMEN
    ayri bir dosya (ayni tasarimi kopyalamiyor). Ayni backend/API'yi kullanir,
    sadece farkli bir on yuz. Capacitor kabugu (mobile/) ileride bu URL'e
    yonlendirilebilir - bkz. mobile/capacitor.config.json."""
    from fastapi.responses import FileResponse
    return FileResponse(os.path.join(os.path.dirname(__file__), 'mobile_app.html'))


@app.get("/apple-touch-icon.png")
def serve_apple_touch_icon():
    from fastapi.responses import FileResponse
    return FileResponse(os.path.join(os.path.dirname(__file__), 'static', 'apple-touch-icon.png'))

@app.get("/api/health")
def health_check():
    return {"status": "ok"}


def _check_admin(request: Request):
    """Yonetici paneli icin kimlik dogrulama - uye hesaplarindan tamamen
    bagimsiz, ayri bir gizli anahtar (BACKUP_SECRET/thesports-test deseniyle
    ayni yaklasim). ADMIN_SECRET tanimli degilse panel tamamen kapali kalir."""
    expected = os.environ.get("ADMIN_SECRET")
    provided = request.headers.get("x-admin-secret")
    return bool(expected) and provided == expected


@app.get("/admin")
def serve_admin():
    """Yonetici paneli - ana siteden (index.html) tamamen bagimsiz sayfa,
    tek baglantisi ayni backend'e /api/admin/panel/* uclarindan istek atmasi.
    Sayfanin kendisi herkese acik yuklenir (statik kabuk, veri icermez);
    veri uclari x-admin-secret ile korunur."""
    from fastapi.responses import FileResponse
    return FileResponse(os.path.join(os.path.dirname(__file__), 'admin.html'))


@app.get("/api/admin/panel/ozet")
def admin_panel_ozet(request: Request):
    from fastapi.responses import JSONResponse
    if not _check_admin(request):
        return JSONResponse({"error": "yetkisiz"}, status_code=403)
    conn = connect()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM users")
    uye_sayisi = cur.fetchone()[0]
    cur.execute("""
        SELECT outcome, COUNT(*) FROM consensus_predictions
        WHERE decision='signal' GROUP BY outcome
    """)
    tally = dict(cur.fetchall())
    won = tally.get("WON", 0)
    lost = tally.get("LOST", 0)
    conn.close()
    return {
        "success": True,
        "uye_sayisi": uye_sayisi,
        "toplam_sinyal": sum(tally.values()),
        "kazanan": won,
        "kaybeden": lost,
        "gecersiz": tally.get("VOID", 0),
        "bekleyen": tally.get(None, 0),
        "isabet_orani": round(won / (won + lost), 3) if (won + lost) else None,
    }


@app.get("/api/admin/panel/uyeler")
def admin_panel_uyeler(request: Request):
    """Uye listesi. NOT: uyelik tipi (Free/Pro) henuz DB'de tutulmuyor -
    Pro rozeti su an sadece on-yuzde bir yer tutucu (window.isProMember),
    gercek odeme entegrasyonu yapilmadi. Bu yuzden herkes 'Free' donuyor -
    uydurma bir ayrim yapmiyoruz."""
    from fastapi.responses import JSONResponse
    if not _check_admin(request):
        return JSONResponse({"error": "yetkisiz"}, status_code=403)
    conn = connect()
    cur = conn.cursor()
    cur.execute('''
        SELECT id, email, created_at, last_seen_at,
               CASE WHEN last_seen_at >= datetime('now', '-5 minutes') THEN 1 ELSE 0 END AS aktif
        FROM users ORDER BY created_at DESC
    ''')
    uyeler = [
        {
            "id": uid, "email": email, "kayit_tarihi": created_at,
            "son_gorulme": last_seen_at, "aktif": bool(aktif),
            "uyelik_tipi": "Free",
        }
        for uid, email, created_at, last_seen_at, aktif in cur.fetchall()
    ]
    conn.close()
    return {"success": True, "uyeler": uyeler,
            "uyari": "Uyelik tipi (Free/Pro) henuz veritabaninda tutulmuyor - odeme entegrasyonu yapilana kadar herkes Free gorunur."}


@app.get("/api/admin/panel/botlar")
def admin_panel_botlar(request: Request):
    """Her botun kac sinyal urettigi + isabet orani (ozet, filtre dropdown'u icin)."""
    from fastapi.responses import JSONResponse
    if not _check_admin(request):
        return JSONResponse({"error": "yetkisiz"}, status_code=403)
    conn = connect()
    cur = conn.cursor()
    cur.execute("""
        SELECT b.bot_name, COUNT(*) n,
               SUM(CASE WHEN c.outcome='WON' THEN 1 ELSE 0 END) hits,
               SUM(CASE WHEN c.outcome IN ('WON','LOST') THEN 1 ELSE 0 END) sonuclanan
        FROM bot_predictions b
        JOIN consensus_predictions c ON c.match_id=b.match_id AND c.snapshot_id=b.snapshot_id
        WHERE b.decision='goal'
        GROUP BY b.bot_name
        ORDER BY n DESC
    """)
    botlar = []
    for name, n, hits, sonuclanan in cur.fetchall():
        botlar.append({
            "bot": name, "paylasim_sayisi": n,
            "isabet_orani": round(hits / sonuclanan, 3) if sonuclanan else None,
        })
    conn.close()
    return {"success": True, "botlar": botlar}


@app.get("/api/admin/panel/bot-sinyalleri")
def admin_panel_bot_sinyalleri(request: Request, bot: str = "", limit: int = 200):
    """Tek bir botun (ya da hepsinin) urettigi tum maç paylaşımlarının detayi:
    hangi mac, ne zaman, ne olasilikla, sonuc ne oldu."""
    from fastapi.responses import JSONResponse
    if not _check_admin(request):
        return JSONResponse({"error": "yetkisiz"}, status_code=403)
    limit = max(1, min(limit, 1000))
    conn = connect()
    cur = conn.cursor()
    params = []
    bot_clause = ""
    if bot:
        bot_clause = "AND b.bot_name = ?"
        params.append(bot)
    params.append(limit)
    cur.execute(f"""
        SELECT b.bot_name, b.decision, b.probability, b.confidence, b.reasons_json,
               m.home_team_id, m.away_team_id, m.league_name,
               c.market, c.signal_minute, c.outcome, c.weighted_probability, c.created_at
        FROM bot_predictions b
        JOIN consensus_predictions c ON c.match_id=b.match_id AND c.snapshot_id=b.snapshot_id
        JOIN matches m ON m.id = b.match_id
        WHERE b.decision='goal' {bot_clause}
        ORDER BY c.created_at DESC
        LIMIT ?
    """, params)
    rows = []
    for (bot_name, decision, prob, conf, reasons_json, home, away, league,
         market, sig_min, outcome, w_prob, created_at) in cur.fetchall():
        rows.append({
            "bot": bot_name, "olasilik": prob, "guven": conf,
            "gerekce": json.loads(reasons_json) if reasons_json else [],
            "ev_sahibi": home, "deplasman": away, "lig": league,
            "market": market, "sinyal_dakikasi": sig_min,
            "konsensus_olasilik": w_prob, "sonuc": outcome or "PENDING",
            "tarih": created_at,
        })
    conn.close()
    return {"success": True, "sinyaller": rows}


@app.post("/api/admin/void-pending-signals")
def admin_void_pending_signals(request: Request, league_name: str = "", match_ids: str = "",
                                allow_settled_for_named_matches: bool = False):
    """Sinyalleri hard-delete eder - kullanici talebi (2026-08-28, acil):
    supheli/sike iddiasi olan bir maci/ligi kaldirmak.

    CLAUDE.md kural 4 (outcome yazildiktan sonra ASLA silinmez) VARSAYILAN
    OLARAK korunuyor - league_name ile TOPLU silme her zaman SADECE
    outcome IS NULL satirlara dokunur (kotu sonuclari secip silme riskine
    karsi kasitli engel).

    ISTISNA: match_ids ile ACIKCA isimlendirilmis maclar icin, kullanici
    allow_settled_for_named_matches=true gonderirse sonuclanmis satirlar da
    silinebilir - bu, "sike iddiasi olan bir maca sinyal atadigimiz kaydin
    KENDISI olmasin, sonuc ne olursa olsun" gibi ozel/gerekceli tek seferlik
    durumlar icin (proje sahibinin acik talimatiyla, 2026-08-28). Toplu/lig
    bazli silmede bu istisna GECERLI DEGIL - sadece tek tek isimlendirilmis
    match_id'ler icin calisir, boylece kotu sonuclari sessizce temizleme
    riski yapisal olarak sinirli kaliyor."""
    from fastapi.responses import JSONResponse
    if not _check_admin(request):
        return JSONResponse({"error": "yetkisiz"}, status_code=403)
    if not league_name and not match_ids:
        return {"success": False, "error": "league_name veya match_ids gerekli."}

    ids = [int(x) for x in match_ids.split(",") if x.strip().isdigit()]

    conn = connect()
    cur = conn.cursor()
    deleted = 0

    if league_name:
        cur.execute(
            "DELETE FROM consensus_predictions WHERE outcome IS NULL "
            "AND match_id IN (SELECT id FROM matches WHERE league_name = ?)",
            [league_name],
        )
        deleted += cur.rowcount

    if ids:
        placeholders = ",".join("?" for _ in ids)
        if allow_settled_for_named_matches:
            cur.execute(f"DELETE FROM consensus_predictions WHERE match_id IN ({placeholders})", ids)
        else:
            cur.execute(
                f"DELETE FROM consensus_predictions WHERE outcome IS NULL AND match_id IN ({placeholders})",
                ids,
            )
        deleted += cur.rowcount

    conn.commit()
    conn.close()
    return {"success": True, "silinen_sinyal_sayisi": deleted}


@app.get("/api/admin/outbound-ip")
def outbound_ip(request: Request):
    """GECICI TANI UCU: Railway'in bu servis icin kullandigi cikis IP'sini
    ogrenmek icin. TheSports gibi IP whitelist isteyen ucuncu taraf API'lere
    hangi IP'yi eklememiz gerektigini bulmaya yariyor. Disariya sadece
    salt-okunur bir kontrol istegi atiyor, hicbir veriye dokunmuyor."""
    from fastapi.responses import JSONResponse
    expected = os.environ.get("BACKUP_SECRET") or os.environ.get("SECRET_KEY")
    provided = request.headers.get("x-backup-secret")
    if not expected or provided != expected:
        return JSONResponse({"error": "yetkisiz"}, status_code=403)
    try:
        r = requests.get("https://api.ipify.org?format=json", timeout=10)
        return r.json()
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/admin/thesports-test")
def thesports_test(request: Request, path: str = "/v1/football/match/detail_live", extra: str = ""):
    """GECICI TANI UCU: TheSports API'sine RAILWAY'IN kendi IP'sinden bir
    deneme istegi atar - IP whitelist onayini, endpoint kesfini ve gercek
    yanit yapisini dogrulamak icin. Anahtarlar ortam degiskeninden okunuyor.

    path: denenecek endpoint yolu (varsayilan detail_live)
    extra: "k1=v1,k2=v2" formatinda ekstra sorgu parametreleri (opsiyonel)
    """
    from fastapi.responses import JSONResponse
    expected = os.environ.get("BACKUP_SECRET") or os.environ.get("SECRET_KEY")
    provided = request.headers.get("x-backup-secret")
    if not expected or provided != expected:
        return JSONResponse({"error": "yetkisiz"}, status_code=403)
    user = os.environ.get("THESPORTS_USER")
    secret = os.environ.get("THESPORTS_SECRET")
    if not user or not secret:
        return JSONResponse({"error": "THESPORTS_USER/THESPORTS_SECRET tanimli degil"}, status_code=500)
    params = {"user": user, "secret": secret}
    if extra:
        for pair in extra.split(","):
            if "=" in pair:
                k, v = pair.split("=", 1)
                params[k.strip()] = v.strip()
    try:
        r = requests.get(
            f"https://api.thesports.com{path}",
            params=params,
            timeout=15,
        )
        return JSONResponse({"status_code": r.status_code, "body": r.text[:1000000]})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/admin/db-backup")
def db_backup(request: Request):
    """Canli SQLite dosyasinin salt-okunur yedegini indirir.

    NEDEN GEREKLI: Volume kalici olsa bile tek kopya - yanlislikla silinen bir
    Volume, hatali bir restore ya da baska bir felaket senaryosuna karsi
    veritabaninin Railway disinda da bir kopyasi bulunmali. SECRET_KEY zaten
    var olan bir gizli anahtar (auth.py'de token imzalamak icin kullaniliyor);
    yeni bir sir eklemek yerine onu yeniden kullaniyoruz. Query string yerine
    header'da tasiniyor - aksi halde Railway erisim loglarinda acik metin
    olarak gorunurdu.
    """
    from fastapi.responses import FileResponse, JSONResponse
    expected = os.environ.get("BACKUP_SECRET") or os.environ.get("SECRET_KEY")
    provided = request.headers.get("x-backup-secret")
    if not expected or provided != expected:
        return JSONResponse({"error": "yetkisiz"}, status_code=403)
    from db_config import DB_PATH
    return FileResponse(DB_PATH, filename="fh_goal_predictor_backup.db",
                         media_type="application/octet-stream")


FS_MISSING_GRACE_MINUTES = 5

# last_seen_at/last_progress_at bugune kadar SADECE thesports_bot.py
# calistiginda ekleniyordu (_ensure_match_tracking_schema). Bu uclar artik
# thesports_bot'a BAGIMLI OLMAMALI (TheSports devre disi kalsa/kaldirilsa
# bile calismali) - kendi idempotent semasini garantiliyor.
_fs_schema_ready = False


def _fs_ensure_schema():
    global _fs_schema_ready
    if _fs_schema_ready:
        return
    conn = connect()
    for stmt in (
        "ALTER TABLE matches ADD COLUMN last_seen_at TIMESTAMP",
        "ALTER TABLE matches ADD COLUMN last_progress_at TIMESTAMP",
    ):
        try:
            conn.execute(stmt)
        except Exception:
            pass
    conn.commit()
    conn.close()
    _fs_schema_ready = True

# Flashscore'un "stage" hucresinden gelen metni (minute, minute_ok, status)
# ucluye cevirir. Sadece YERELDE Playwright ile GOZLEMLENMIS degerler icin
# kesin bir esleme var (rakam -> dakika, "Half Time" -> HT, bkz.
# flashscore_xg_bot.py docstring). Tanimadigimiz bir metin gelirse TAHMIN
# ETMIYORUZ - LIVE/dakika bilinmiyor sayip stat_key_discovery'ye logluyoruz
# (thesports_bot.py'deki "YENI STATUS_ID" deseniyle ayni ilke).
def _fs_parse_stage(stage_text, source="fs"):
    st = (stage_text or "").strip()
    if st.isdigit():
        return int(st), True, "LIVE"
    stl = st.lower()
    if stl in ("half time", "ht"):
        return None, False, "HT"
    if "penalt" in stl or "extra time" in stl:
        return None, False, "LIVE"
    if "finished" in stl or stl in ("ft", "aet", "pen"):
        return None, False, "FINISHED"
    if "postponed" in stl or "cancel" in stl or "abandoned" in stl:
        return None, False, "ABANDONED"
    _fs_unknown_stage_kaydet(st, source)
    return None, False, "LIVE"


def _fs_unknown_stage_kaydet(stage_text, source="fs"):
    if not stage_text:
        return
    try:
        conn = connect()
        cur = conn.cursor()
        cur.execute("""CREATE TABLE IF NOT EXISTS stat_key_discovery (
            anahtar TEXT PRIMARY KEY, gorulme INTEGER, ilk_gorulme TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        cur.execute("""INSERT INTO stat_key_discovery (anahtar, gorulme) VALUES (?,1)
                       ON CONFLICT(anahtar) DO UPDATE SET gorulme = gorulme + 1""",
                    (f"{source}_stage_{stage_text}",))
        conn.commit()
        conn.close()
    except Exception:
        pass


def _iddaa_ensure_schema():
    conn = connect()
    for stmt in (
        # KALICI ARSIV: her gun gordugumuz mac + acilis orani, sonuc belli
        # olunca (kendi canli verimizle eslestirerek) sonuc da buraya yazilir -
        # zamanla kendi Iddaa-kaynakli veri setimizi olusturur (bkz. kullanici
        # talebi 2026-08-24). ISTATISTIKSEL KULLANIM icin (odds_profile.py'nin
        # arsivini YENILEMEK, yeni bantlar/marketler denemek) ileride buradan
        # beslenebilir - su an SADECE _iddaa_transfer_odds canliya gecis aninda
        # 1X2'yi live_odds'a tasimak icin okuyor.
        """CREATE TABLE IF NOT EXISTS iddaa_odds_archive (
            iddaa_event_id INTEGER PRIMARY KEY,
            home_raw TEXT, away_raw TEXT,
            home_norm TEXT, away_norm TEXT,
            league TEXT,
            odd_1 REAL, odd_x REAL, odd_2 REAL,
            fh_over_line REAL, fh_over_odd REAL, fh_under_odd REAL,
            ms_over_line REAL, ms_over_odd REAL, ms_under_odd REAL,
            first_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            fh_home_score INTEGER, fh_away_score INTEGER,
            ft_home_score INTEGER, ft_away_score INTEGER,
            result_captured_at TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS live_odds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id INTEGER, market TEXT, selection TEXT, odds REAL,
            captured_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
    ):
        try:
            conn.execute(stmt)
        except Exception:
            pass
    conn.commit()
    conn.close()


def _iddaa_transfer_odds(cur, match_db_id, home_raw, away_raw):
    """Mac ilk kez CANLIYA gectiginde (live_sync'teki is_new dalindan
    cagrilir) Iddaa arsivindeki (henuz baslamamisken cekilmis) en yakin
    esan takim adiyla eslesen kaydi arar, bulursa TEK SEFERLIK live_odds'a
    yazar - bu deger BIR DAHA GUNCELLENMEZ (acilis orani donduruldu, maç
    ilerledikce degisen canli oran DEGIL - eski tasarimin "her mac zamanla
    dengeli gorunuyor" sorunu buydu, bkz. orchestrator.py'deki eski NOT).
    Eslesme bulunamazsa sessizce gecilir (bu mac icin bot_odds_profile
    insufficient_data doner)."""
    from flashscore_xg_bot import _normalize as _tnorm, MIN_MATCH_SCORE
    import difflib
    h_norm, a_norm = _tnorm(home_raw), _tnorm(away_raw)
    cur.execute("SELECT home_norm, away_norm, odd_1, odd_x, odd_2 FROM iddaa_odds_archive")
    best, best_score = None, 0.0
    for hn, an, o1, ox, o2 in cur.fetchall():
        score = (difflib.SequenceMatcher(None, h_norm, hn or "").ratio()
                 + difflib.SequenceMatcher(None, a_norm, an or "").ratio()) / 2
        if score > best_score:
            best_score, best = score, (o1, ox, o2)
    if not best or best_score < MIN_MATCH_SCORE:
        return
    o1, ox, o2 = best
    cur.executemany(
        "INSERT INTO live_odds (match_id, market, selection, odds) VALUES (?,?,?,?)",
        [(match_db_id, "1x2", "1", o1), (match_db_id, "1x2", "x", ox), (match_db_id, "1x2", "2", o2)],
    )


def _iddaa_backfill_results(cur):
    """Arsivde SONUCU henuz bilinmeyen (ft_home_score NULL) kayitlar icin
    kendi matches tablomuzda (status='FINISHED') fuzzy takim adiyla eslesen
    bir mac var mi diye bakar, varsa skoru (MS + IY) kopyalar. Ayri bir
    "sonuc scraper"ina gerek yok - zaten canli takip ettigimiz maclarin
    sonucunu KENDI verimizden ariyoruz. Iddaa'nin listeledigi ama bizim hic
    canli gormedigimiz maclar (kucuk ligler, bizim kaynaklarimizin
    kapsamadigi) sonucsuz kalir - bu BEKLENEN bir durum, uydurma yok."""
    from flashscore_xg_bot import _normalize as _tnorm, MIN_MATCH_SCORE
    import difflib
    cur.execute('''
        SELECT iddaa_event_id, home_norm, away_norm FROM iddaa_odds_archive
        WHERE ft_home_score IS NULL
          AND last_seen_at <= datetime('now', '-2 hours')
    ''')
    bekleyen = cur.fetchall()
    if not bekleyen:
        return 0
    cur.execute('''
        SELECT home_team_id, away_team_id, home_score, away_score,
               first_half_home_score, first_half_away_score
        FROM matches WHERE status='FINISHED'
          AND last_seen_at >= datetime('now', '-3 days')
    ''')
    bitmis = cur.fetchall()
    bitmis_norm = [(_tnorm(h), _tnorm(a), hs, as_, fhs, fas) for h, a, hs, as_, fhs, fas in bitmis]

    guncellenen = 0
    for eid, hn, an in bekleyen:
        best, best_score = None, 0.0
        for bh, ba, hs, as_, fhs, fas in bitmis_norm:
            score = (difflib.SequenceMatcher(None, hn or "", bh).ratio()
                     + difflib.SequenceMatcher(None, an or "", ba).ratio()) / 2
            if score > best_score:
                best_score, best = score, (hs, as_, fhs, fas)
        if not best or best_score < MIN_MATCH_SCORE:
            continue
        hs, as_, fhs, fas = best
        if hs is None or as_ is None:
            continue
        cur.execute('''
            UPDATE iddaa_odds_archive
            SET ft_home_score=?, ft_away_score=?, fh_home_score=?, fh_away_score=?,
                result_captured_at=CURRENT_TIMESTAMP
            WHERE iddaa_event_id=?
        ''', (hs, as_, fhs, fas, eid))
        guncellenen += 1
    return guncellenen


@app.get("/api/odds-profile-fine-bins")
def odds_profile_fine_bins():
    """Kullanici talebi (2026-08-25): 'bugunku Iddaa maclarini arsivle
    karsilastiran yerel bir arac' icin - odds_profile.py'nin %5'lik ince
    dilim oranlarini (bin, ornek sayisi, IY gol orani, MS 1.5 ust orani)
    DISARIYA acar. Admin secret GEREKTIRMIYOR - burada hicbir kisisel/
    hassas veri yok, sadece 33k+ maclik arsivden onceden hesaplanmis
    ozet/aggregate istatistik (zaten /api/ozet-donem gibi diger public
    uclarla ayni hassasiyet seviyesinde). Yerel arac (iddaa_karsilastirma.py)
    bunu + Iddaa.com'un kendi canli API'sini dogrudan cekip client-side
    kiyaslama yapiyor."""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT bin, ornek, iy_gol_orani, ms15_orani FROM odds_profile_rates_fine ORDER BY bin"
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    conn.close()
    return {
        "success": True,
        "dilimler": [
            {"bin": b, "ornek": n, "iy_gol_orani": round(iy, 4), "ms15_orani": round(ms, 4)}
            for b, n, iy, ms in rows
        ],
    }


@app.get("/api/archive-market-bins")
def archive_market_bins():
    """Kullanici talebi (2026-08-25): yerel iddaa_karsilastirma.py aracinda
    MS 4.5 Ust, IY 1.5 Ust, IY/2Y KG Var, IY-MS 9'lu kombinasyon (0/1, 1/2 gibi)
    secilebilsin. odds_profile.build_market_fine()'in onceden hesapladigi TUM
    marketlerin %5'lik dilim oranlarini tek seferde disari acar (public,
    admin secret gerektirmiyor - sadece aggregate arsiv istatistigi, ayni
    /api/odds-profile-fine-bins ile ayni hassasiyet seviyesinde). Kesin/
    toleranslı esleme MANTIGI (bir dilimde yeterli ornek yoksa komsu dilime
    bak) bilerek CLIENT tarafinda (yerel arac) yapiliyor - boylece tek istekle
    tum mac x market kombinasyonlari aninda hesaplanabiliyor."""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT market, bin, ornek, oran FROM market_fine_bins ORDER BY market, bin"
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    conn.close()

    from odds_profile import MARKET_LABELS
    bins = {}
    for market, b, ornek, oran in rows:
        bins.setdefault(market, {})[b] = {"ornek": ornek, "oran": round(oran, 4)}
    return {"success": True, "market_labels": MARKET_LABELS, "bins": bins}


@app.get("/api/archive-bin-examples")
def archive_bin_examples(ornek: int = 8):
    """Kullanici talebi (2026-08-25): yerel iddaa_karsilastirma.py aracinda
    bir maca tiklayinca AYNI favori gucu dilimine (%5) dusen GERCEK gecmis
    maclari (takim adi + ilk yari/mac sonu skoru) gosterebilsin. Market'e
    OZEL degil - HAM skorlari doner, hangi marketi tuttugunu (KG var, Ust,
    IY/MS kombinasyonu) client tarafinda JS hesaplar (odds_profile.py'deki
    _market_outcomes ile ayni mantik) - boylece secili market degisince
    ayni ornek listesi yeniden kullanilir, tekrar istek atilmaz.

    Public (admin secret gerekmiyor) - sadece tarihsel mac skorlari/takim
    adlari, /api/archive-market-bins ile ayni hassasiyet seviyesinde."""
    ornek = max(1, min(ornek, 20))
    import odds_profile

    conn = connect()
    cur = conn.cursor()
    cur.execute('''
        SELECT o.home_win_odds, o.draw_odds, o.away_win_odds,
               m.home_team_id, m.away_team_id, m.league_name, m.kickoff_time,
               m.home_score, m.away_score, m.first_half_home_score, m.first_half_away_score
        FROM prematch_odds o JOIN matches m ON m.id = o.match_id
        WHERE o.home_win_odds IS NOT NULL AND o.draw_odds IS NOT NULL
          AND o.away_win_odds IS NOT NULL AND m.home_score IS NOT NULL AND m.away_score IS NOT NULL
        ORDER BY RANDOM()
    ''')
    dilimler = {}
    for ev, bx, dep, home, away, lig, kickoff, hs, aws, fhh, fha in cur.fetchall():
        p = odds_profile.devig_1x2(ev, bx, dep)
        if not p:
            continue
        favori = max(p[0], p[2])
        b = odds_profile._fine_bin(favori)
        lst = dilimler.setdefault(b, [])
        if len(lst) >= ornek:
            continue
        lst.append({
            "ev_sahibi": home, "deplasman": away, "lig": lig, "tarih": kickoff,
            "iy_h": fhh, "iy_a": fha, "ms_h": hs, "ms_a": aws,
        })
    conn.close()
    return {"success": True, "dilimler": dilimler}


@app.get("/api/admin/iddaa-odds-preview")
def iddaa_odds_preview(request: Request, ornekler: int = 0):
    """Kullanici talebi (2026-08-24): 3 kaba bant yerine 33.525 maclik
    arsivi favori gucune gore %5'lik ince dilimlerle tarayip, su an cache'te
    olan (henuz baslamamis) her Iddaa macinin gercek/olculmus IY gol oranini
    doner - buyukten kucuge siralanmis. Az orneli (FINE_MIN_ORNEK altinda)
    dilimler HARIC tutulur (bkz. odds_profile.build_fine).

    ornekler>0 ise HER mac icin ayni ince dilime dusen GERCEK gecmis
    maclardan (takim adi + skor) ornekler=N tanesini de ekler - kullanici
    talebi: "eslesen maclari ve gecmisteki ayni oran maclarin skorlarinin
    bulundugu liste hazirla"."""
    from fastapi.responses import JSONResponse
    if not _check_admin(request):
        return JSONResponse({"error": "yetkisiz"}, status_code=403)

    import odds_profile
    odds_profile.build_fine()
    ornekler = max(0, min(ornekler, 20))

    conn = connect()
    cur = conn.cursor()
    cur.execute("SELECT home_raw, away_raw, odd_1, odd_x, odd_2 FROM iddaa_odds_archive")
    rows = cur.fetchall()

    # ornekler istendiyse: tum arsivi (oran+skor+takim adi) TEK seferde
    # cekip bellekte dilime gore grupla - her mac icin ayri sorgu atmaktan
    # cok daha ucuz (250 mac x ayri sorgu yerine 1 sorgu).
    dilim_ornekleri = {}
    if ornekler > 0:
        cur.execute('''
            SELECT o.home_win_odds, o.draw_odds, o.away_win_odds,
                   m.home_team_id, m.away_team_id,
                   m.first_half_home_score, m.first_half_away_score,
                   m.home_score, m.away_score
            FROM prematch_odds o JOIN matches m ON m.id = o.match_id
            WHERE o.home_win_odds IS NOT NULL AND o.draw_odds IS NOT NULL
              AND o.away_win_odds IS NOT NULL
              AND m.first_half_home_score IS NOT NULL AND m.home_score IS NOT NULL
        ''')
        for ev, bx, dep, h, a, fhh, fha, hs, aws in cur.fetchall():
            p = odds_profile.devig_1x2(ev, bx, dep)
            if not p:
                continue
            favori = max(p[0], p[2])
            b = odds_profile._fine_bin(favori)
            lst = dilim_ornekleri.setdefault(b, [])
            if len(lst) < ornekler:
                lst.append({
                    "ev_sahibi": h, "deplasman": a,
                    "ilk_yari_skoru": f"{fhh}-{fha}", "mac_sonu_skoru": f"{hs}-{aws}",
                })
    conn.close()

    sonuc = []
    for home, away, o1, ox, o2 in rows:
        p = odds_profile.devig_1x2(o1, ox, o2)
        if not p:
            continue
        favori = max(p[0], p[2])
        fine = odds_profile.fine_rate_for_favori(favori)
        if not fine:
            continue
        ornek, iy_orani, ms15_orani = fine
        kayit = {
            "ev_sahibi": home, "deplasman": away,
            "oranlar": [o1, ox, o2], "favori_gucu": round(favori, 3),
            "ilk_yari_gol_orani": round(iy_orani, 3),
            "ms_15_ust_orani": round(ms15_orani, 3),
            "arsiv_ornek_sayisi": ornek,
        }
        if ornekler > 0:
            kayit["gecmis_ornek_maclar"] = dilim_ornekleri.get(odds_profile._fine_bin(favori), [])
        sonuc.append(kayit)
    sonuc.sort(key=lambda x: x["ilk_yari_gol_orani"], reverse=True)
    return {"success": True, "toplam": len(sonuc), "maclar": sonuc}


@app.post("/api/admin/iddaa-odds-sync")
def iddaa_odds_sync(request: Request, payload: dict):
    """iddaa_odds_client.py'den (Railway'de calisan, HER GUN oynanan
    futbol maclarinin Iddaa oranlarini ceken istemciden) gelen toplu
    guncellemeyi isler ve kalici arsive (iddaa_odds_archive) yazar.
    bot_odds_profile.py bu arsivi DOGRUDAN kullanmaz - mac canliya
    gectiginde live_sync icinde _iddaa_transfer_odds ile 1X2'si TEK
    SEFERLIK live_odds'a aktarilir. Ayrica her cagrida _iddaa_backfill_results
    ile SONUCU belli olan eski kayitlar kendi canli verimizden doldurulur -
    zamanla buyuyen bagimsiz bir oran+sonuc arsivi olusturuyor."""
    from fastapi.responses import JSONResponse
    expected = os.environ.get("BACKUP_SECRET") or os.environ.get("SECRET_KEY")
    provided = request.headers.get("x-backup-secret")
    if not expected or provided != expected:
        return JSONResponse({"error": "yetkisiz"}, status_code=403)
    _iddaa_ensure_schema()

    events = payload.get("events") or []
    conn = connect()
    cur = conn.cursor()
    yazilan = 0
    from flashscore_xg_bot import _normalize as _tnorm
    for e in events:
        eid = e.get("event_id")
        home = (e.get("home") or "").strip()
        away = (e.get("away") or "").strip()
        odd_1, odd_x, odd_2 = e.get("odd_1"), e.get("odd_x"), e.get("odd_2")
        if not eid or not home or not away:
            continue
        cur.execute('''
            INSERT INTO iddaa_odds_archive
                (iddaa_event_id, home_raw, away_raw, home_norm, away_norm, league,
                 odd_1, odd_x, odd_2, fh_over_line, fh_over_odd, fh_under_odd,
                 ms_over_line, ms_over_odd, ms_under_odd, first_seen_at, last_seen_at)
            VALUES (?,?,?,?,?,?, ?,?,?, ?,?,?, ?,?,?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT(iddaa_event_id) DO UPDATE SET
                odd_1=excluded.odd_1, odd_x=excluded.odd_x, odd_2=excluded.odd_2,
                fh_over_line=excluded.fh_over_line, fh_over_odd=excluded.fh_over_odd, fh_under_odd=excluded.fh_under_odd,
                ms_over_line=excluded.ms_over_line, ms_over_odd=excluded.ms_over_odd, ms_under_odd=excluded.ms_under_odd,
                last_seen_at=CURRENT_TIMESTAMP
        ''', (eid, home, away, _tnorm(home), _tnorm(away), e.get("league") or "",
              odd_1, odd_x, odd_2,
              e.get("fh_over_line"), e.get("fh_over_odd"), e.get("fh_under_odd"),
              e.get("ms_over_line"), e.get("ms_over_odd"), e.get("ms_under_odd")))
        yazilan += 1

    guncellenen = 0
    try:
        guncellenen = _iddaa_backfill_results(cur)
    except Exception as e:
        print(f"⚠️  Iddaa sonuc doldurma hatasi: {e}", flush=True)
    conn.commit()
    conn.close()
    return {"success": True, "yazilan": yazilan, "sonuc_dolduruldu": guncellenen}


@app.post("/api/admin/archive-csv-import")
def archive_csv_import(request: Request, payload: dict):
    """Kullanicinin elle indirip gonderdigi hazir mac arsivlerini (ornegin
    football-data.co.uk formatinda Div/Date/HomeTeam/AwayTeam/FTHG/FTAG/
    HTHG/HTAG/B365... CSV'leri) kalici arsive ekler - etl_archive.py'nin
    (Iddaa arsivi icin yazilmis, sadece YEREL calisan) ayni ilkesinin
    Railway'deki KALICI veritabanina yazan, tekrar tekrar guvenle
    calistirilabilen (idempotent) HTTP karsiligi.

    Beklenen payload: {"league_name": "...", "matches": [
        {"home","away","date","time","fthg","ftag","hthg","htag",
         "odds_h","odds_d","odds_a","odds_o25","odds_u25"}, ...
    ]}

    Takim kimligi flashscore_xg_bot._normalize ile ayni kurala tabi -
    boylece bu arsivdeki takimlar canli takip ettigimiz maclarla AYNI
    team_id altinda birlesip team_history.py'nin form hesabina da
    katkida bulunabiliyor (etl_archive.py'nin eski kaba normalize_name'i
    yerine bu kullanildi)."""
    from fastapi.responses import JSONResponse
    expected = os.environ.get("BACKUP_SECRET") or os.environ.get("SECRET_KEY")
    provided = request.headers.get("x-backup-secret")
    if not expected or provided != expected:
        return JSONResponse({"error": "yetkisiz"}, status_code=403)

    from flashscore_xg_bot import _normalize as _tnorm

    league_name = (payload.get("league_name") or "").strip()
    rows = payload.get("matches") or []
    conn = connect()
    cur = conn.cursor()
    yeni = 0
    zaten_vardi = 0
    for m in rows:
        home = (m.get("home") or "").strip()
        away = (m.get("away") or "").strip()
        date_str = (m.get("date") or "").strip()
        time_str = (m.get("time") or "").strip()
        if not home or not away or not date_str:
            continue
        try:
            fthg = int(m.get("fthg"))
            ftag = int(m.get("ftag"))
        except (TypeError, ValueError):
            continue
        try:
            hthg = int(m.get("hthg"))
            htag = int(m.get("htag"))
        except (TypeError, ValueError):
            hthg, htag = None, None

        home_id, away_id = _tnorm(home), _tnorm(away)
        source_id = f"csv_{home_id}_{away_id}_{date_str}_{time_str}"
        kickoff = f"{date_str} {time_str}".strip()

        cur.execute('''
            INSERT OR IGNORE INTO matches
                (source_match_id, home_team_id, away_team_id, kickoff_time, status,
                 home_score, away_score, first_half_home_score, first_half_away_score,
                 league_name, last_seen_at)
            VALUES (?,?,?,?, 'FINISHED', ?,?,?,?, ?, ?)
        ''', (source_id, home_id, away_id, kickoff, fthg, ftag, hthg, htag,
              league_name, kickoff))

        if cur.rowcount == 0:
            zaten_vardi += 1
            continue
        yeni += 1

        cur.execute("SELECT id FROM matches WHERE source_match_id=?", (source_id,))
        row = cur.fetchone()
        if not row:
            continue
        match_id = row[0]

        odds_h, odds_d, odds_a = m.get("odds_h"), m.get("odds_d"), m.get("odds_a")
        odds_o25, odds_u25 = m.get("odds_o25"), m.get("odds_u25")
        if odds_h and odds_d and odds_a:
            cur.execute('''
                INSERT INTO prematch_odds
                    (match_id, home_win_odds, draw_odds, away_win_odds, over_25_odds, under_25_odds)
                VALUES (?,?,?,?,?,?)
            ''', (match_id, odds_h, odds_d, odds_a, odds_o25, odds_u25))

    conn.commit()
    conn.close()
    return {"success": True, "yeni": yeni, "zaten_vardi": zaten_vardi, "toplam": len(rows)}


def _capture_fh_score(cur, match_db_id, status, score_h, score_a):
    """Mac devre arasina (HT) girdiginde o anki skoru matches.first_half_*
    kolonlarina TEK SEFERLIK yazar. Bu kolonlar eskiden SADECE toplu arsiv
    importunda (etl_archive.py) doluyordu - CANLI takip ettigimiz hicbir
    mac icin hic yazilmiyordu (kullanici raporu 2026-08-24: /api/match/{id}
    'Veri Bekleniyor' gosteriyor cunku team_match_history hic doluyor,
    o da BUNA bagli). HT hic gozlemlenmediyse (once bagli kalindiysa) mac
    FINISHED olduğunda son first_half doneminde kaydedilen skoru yedek
    olarak kullanir - kusursuz degil ama hicten iyi."""
    if status == "HT":
        cur.execute(
            "UPDATE matches SET first_half_home_score=?, first_half_away_score=? "
            "WHERE id=? AND first_half_home_score IS NULL",
            (score_h, score_a, match_db_id),
        )
    elif status == "FINISHED":
        cur.execute("SELECT first_half_home_score FROM matches WHERE id=?", (match_db_id,))
        row = cur.fetchone()
        if row and row[0] is None:
            cur.execute(
                "SELECT home_score, away_score FROM live_snapshots "
                "WHERE match_id=? AND period IN ('first_half','half_time') "
                "ORDER BY id DESC LIMIT 1",
                (match_db_id,),
            )
            fh = cur.fetchone()
            if fh:
                cur.execute(
                    "UPDATE matches SET first_half_home_score=?, first_half_away_score=? WHERE id=?",
                    (fh[0], fh[1], match_db_id),
                )


def _fs_valid_source(source):
    """Kaynak onegi bir SQL LIKE deseni ve event_id parcasi olarak kullanilacak -
    sadece kucuk harf+rakamla sinirla, gecersizse guvenli varsayilana (fs) don."""
    s = (source or "").strip().lower()
    if s and s.isalnum() and len(s) <= 12:
        return s
    return "fs"


def _fs_close_stale(cursor, active_ids, prefix="fs"):
    """thesports_bot._close_stale_missing ile AYNI mantik, ama SADECE verilen
    onekli (tek bir canli veri kaynagina ait) maclara uygulanir - diger
    kaynaklarin (ör. ts_, ss_) maclarina dokunmamak icin (bkz. thesports_bot
    ile fs_ kaynaklarinin birbirine karismamasi ilkesi)."""
    params = []
    active_clause = ""
    if active_ids:
        placeholders = ','.join('?' for _ in active_ids)
        active_clause = f"AND source_match_id NOT IN ({placeholders})"
        params.extend(active_ids)
    like_pattern = prefix.replace('_', r'\_').replace('%', r'\%') + r'\_%'
    cursor.execute(f'''
        UPDATE matches
        SET status = CASE WHEN COALESCE(minute, 0) >= 85 THEN 'FINISHED' ELSE 'ABANDONED' END
        WHERE status NOT IN ('FINISHED','ABANDONED','Ended','FT','Canceled')
          AND source_match_id LIKE ? ESCAPE '\\'
          AND last_seen_at IS NOT NULL
          AND last_seen_at <= datetime('now', '-{FS_MISSING_GRACE_MINUTES} minutes')
          {active_clause}
    ''', [like_pattern] + params)


_TEAM_SUFFIX_WORDS = re.compile(
    r'\b(fc|cf|sc|ac|cd|afc|sk|fk|club|sporting club|sports club)\b'
)


def _normalize_team_name(name):
    """Kaynaklar (fs_/ss_/7m_) ayni takimi farkli yaziyor - "Leganes B" vs
    "Leganés B", "Mohammedan" vs "Mohammedan Sporting Club" gibi. Bu farklar
    ayni gercek macin `matches` tablosunda IKI AYRI satir olarak kaydedilmesine
    yol aciyordu (2026-08-28'de kullanici tarafindan fark edildi - izleme
    panelinde ayni skorlu iki "farkli" mac gorunuyordu). Aksan/case/yaygin
    kulup eki farklarini eleyip karsilastirilabilir bir anahtar uretir -
    canli/tam eslesme icin kullanilir, bulanik/kismi eslesme YAPILMAZ (yanlis
    pozitif riski - iki farkli gercek mac yanlislikla birlestirilmesin diye)."""
    if not name:
        return ""
    s = unicodedata.normalize('NFKD', name)
    s = ''.join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = _TEAM_SUFFIX_WORDS.sub(' ', s)
    s = re.sub(r'[^a-z0-9]+', ' ', s).strip()
    return s


@app.post("/api/admin/live-sync")
def live_sync(request: Request, payload: dict):
    """Herhangi bir YEREL canli veri istemcisinden (flashscore_xg_client.py,
    veya ayni sozlesmeye uyan baska bir kaynak istemcisi - bkz. `source`
    alani) gelen TUM canli maclarin skor/dakika/lig ozetini isler.

    NEDEN VAR: 2026-08-12'de TheSports hesabi "yetkisiz" hatasi vermeye
    basladi (abonelik/plan sorunu, bkz. Railway loglari) ve TUM canli veri
    kaynagi durdu. Bu uc, thesports_bot.process_matches ile AYNI ROLU
    (matches tablosu upsert + is_known_match filtresi + stale-kapama) dolduruyor.

    COKLU KAYNAK: `payload["source"]` (varsayilan "fs") her kaynagi kendi
    onekiyle (fs_, ss_, ...) IZOLE eder - bir kaynak coker/yanlis veri
    gonderirse digerinin maclarina (stale-kapama dahil) DOKUNMAZ, ayni
    ts_/fs_ ayrimi ilkesi. Ayni mac birden fazla kaynaktan geliyorsa
    (ornegin hem Flashscore hem SofaScore) `matches` tablosunda kaynak
    basina AYRI bir satir olusur - bilincli bir tercih (bkz. proje notlari:
    "cift sinyal riski dusuk oncelikli").

    live_snapshots'a HER canli mac icin YENI bir satir eklenir (sparse UPDATE
    degil) - orchestrator.py'nin momentum botlari (bkz. app/core/orchestrator.py,
    "minute - 5/10/15" sorgulari) GERCEK zaman-serisi satirlarina ihtiyaç
    duyuyor. Detayli istatistik kolonlari bu ucta YOK - bir onceki satirdan
    OLDUGU GIBI KOPYALANIR (carry-forward), boylece /api/admin/live-stats-update
    tarafindan doldurulan degerler bir sonraki hizli senkron turunde
    KAYBOLMAZ (ayni "sparse yazma diger botlari aclikta birakir" ilkesi)."""
    from fastapi.responses import JSONResponse
    expected = os.environ.get("BACKUP_SECRET") or os.environ.get("SECRET_KEY")
    provided = request.headers.get("x-backup-secret")
    if not expected or provided != expected:
        return JSONResponse({"error": "yetkisiz"}, status_code=403)
    _fs_ensure_schema()
    _iddaa_ensure_schema()

    source = _fs_valid_source(payload.get("source"))
    matches_in = payload.get("matches") or []
    prepared = []
    active_ids = []
    for m in matches_in:
        ext_id = m.get("ext_id") or m.get("fs_id")
        home = (m.get("home") or "").strip()
        away = (m.get("away") or "").strip()
        if not ext_id or not home or not away:
            continue
        event_id = f"{source}_{ext_id}"
        minute, minute_ok, status = _fs_parse_stage(m.get("stage"), source)
        try:
            score_h = int(m.get("score_h") or 0)
            score_a = int(m.get("score_a") or 0)
        except (TypeError, ValueError):
            score_h, score_a = 0, 0
        active_ids.append(event_id)
        prepared.append({
            "event_id": event_id, "ext_id": ext_id, "home": home, "away": away,
            "league": (m.get("league") or "Unknown League").strip(),
            "home_logo": m.get("home_logo") or "", "away_logo": m.get("away_logo") or "",
            "minute": minute, "minute_ok": minute_ok, "status": status,
            "score_h": score_h, "score_a": score_a,
        })

    # ASAMA 1: KISA baglanti - temizlik + var olanlari oku (bkz.
    # thesports_bot.process_matches docstring - is_known_match kendi
    # baglantisini acabildigi icin bunu ACIK baglantiyla cakistirmamak lazim)
    conn = connect()
    cur = conn.cursor()
    if active_ids:
        placeholders = ','.join('?' for _ in active_ids)
        cur.execute(f'''
            UPDATE matches SET last_seen_at=CURRENT_TIMESTAMP
            WHERE status IN ('LIVE','HT') AND source_match_id IN ({placeholders})
        ''', active_ids)
    _fs_close_stale(cur, active_ids, source)
    existing_ids = set()
    if active_ids:
        cur.execute(f"SELECT source_match_id FROM matches WHERE source_match_id IN ({placeholders})", active_ids)
        existing_ids = set(r[0] for r in cur.fetchall())
    # Capraz-kaynak duplikasyon onleme (2026-08-28, kullanici tarafindan
    # farkedildi - izleme panelinde ayni mac "Leganes B-CD Guadalajara" ve
    # "Leganés B-Guadalajara" gibi IKI satir olarak gorunuyordu). Zaten canli
    # olan tum maclarin normalize edilmis isim ciftini topla - bir kaynak
    # AYNI maci farkli yaziliskla ilk kez bildirdiginde yeni satir ACILMASIN.
    cur.execute("SELECT home_team_id, away_team_id FROM matches WHERE status IN ('LIVE','HT')")
    live_norm_pairs = {
        (_normalize_team_name(h), _normalize_team_name(a)) for h, a in cur.fetchall()
    }
    conn.commit()
    conn.close()

    # ASAMA 2: HICBIR DB baglantisi ACIK DEGIL - is_known_match guvenle kendi
    # baglantisini acabilir.
    to_write = []
    for m in prepared:
        is_new = m["event_id"] not in existing_ids
        if is_new and not is_known_match(m["league"], m["home"], m["away"]):
            continue
        if is_new:
            norm_pair = (_normalize_team_name(m["home"]), _normalize_team_name(m["away"]))
            if norm_pair in live_norm_pairs:
                # Baska bir kaynaktan zaten canli takip edilen ayni mac -
                # ikinci bir satir acmadan atla (bkz. _normalize_team_name).
                continue
        to_write.append(m)

    # ASAMA 3: TEK KISA transaction'da hepsini yaz.
    conn = connect()
    cur = conn.cursor()
    yeni_sayisi = 0
    for m in to_write:
        is_new = m["event_id"] not in existing_ids
        cur.execute('''
            INSERT INTO matches
            (source_match_id, home_team_id, away_team_id, status, league_name, league_ccode, league_logo, home_score, away_score, minute, home_team_logo, away_team_logo, aggregate_score, last_seen_at, last_progress_at)
            VALUES (?, ?, ?, ?, ?, '', '', ?, ?, ?, ?, ?, '', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT(source_match_id) DO UPDATE SET
                minute=CASE WHEN ? THEN excluded.minute ELSE matches.minute END,
                status=excluded.status,
                home_score=excluded.home_score,
                away_score=excluded.away_score,
                league_name=excluded.league_name,
                home_team_logo=excluded.home_team_logo,
                away_team_logo=excluded.away_team_logo,
                last_seen_at=CURRENT_TIMESTAMP,
                last_progress_at=CASE
                    WHEN (CASE WHEN ? THEN excluded.minute ELSE matches.minute END) IS NOT matches.minute
                         OR excluded.home_score IS NOT matches.home_score
                         OR excluded.away_score IS NOT matches.away_score
                    THEN CURRENT_TIMESTAMP ELSE matches.last_progress_at
                END
        ''', (m["event_id"], m["home"], m["away"], m["status"], m["league"],
              m["score_h"], m["score_a"], m["minute"], m["home_logo"], m["away_logo"],
              m["minute_ok"], m["minute_ok"]))

        cur.execute("SELECT id FROM matches WHERE source_match_id=?", (m["event_id"],))
        row = cur.fetchone()
        if not row:
            continue
        match_db_id = row[0]

        if is_new:
            yeni_sayisi += 1
            minute_to_write = m["minute"] or 0
            period = 'half_time' if m["status"] == "HT" else ('first_half' if minute_to_write <= 45 else 'second_half')
            cur.execute('''
                INSERT INTO live_snapshots (match_id, minute, period, home_score, away_score)
                VALUES (?, ?, ?, ?, ?)
            ''', (match_db_id, minute_to_write, period, m["score_h"], m["score_a"]))
            _capture_fh_score(cur, match_db_id, m["status"], m["score_h"], m["score_a"])
            try:
                _iddaa_transfer_odds(cur, match_db_id, m["home"], m["away"])
            except Exception as e:
                print(f"⚠️  Iddaa oran aktarimi basarisiz: {e}", flush=True)
        else:
            cur.execute('''
                SELECT minute, home_possession, away_possession, home_attacks, away_attacks,
                       home_dangerous_attacks, away_dangerous_attacks, home_corners, away_corners,
                       home_red_cards, away_red_cards, home_shots_on_target, away_shots_on_target,
                       home_shots_off_target, away_shots_off_target, home_shots, away_shots,
                       home_xg, away_xg, home_big_chances, away_big_chances
                FROM live_snapshots WHERE match_id = ? ORDER BY id DESC LIMIT 1
            ''', (match_db_id,))
            prev = cur.fetchone() or (0,) + (None,) * 20
            minute_to_write = m["minute"] if m["minute"] is not None else (prev[0] or 0)
            period = 'half_time' if m["status"] == "HT" else ('first_half' if minute_to_write <= 45 else 'second_half')
            cur.execute('''
                INSERT INTO live_snapshots (
                    match_id, minute, period, home_score, away_score,
                    home_possession, away_possession, home_attacks, away_attacks,
                    home_dangerous_attacks, away_dangerous_attacks, home_corners, away_corners,
                    home_red_cards, away_red_cards, home_shots_on_target, away_shots_on_target,
                    home_shots_off_target, away_shots_off_target, home_shots, away_shots,
                    home_xg, away_xg, home_big_chances, away_big_chances
                ) VALUES (?,?,?,?,?, ?,?,?,?, ?,?,?,?, ?,?,?,?, ?,?,?,?, ?,?,?,?)
            ''', (match_db_id, minute_to_write, period, m["score_h"], m["score_a"], *prev[1:]))
            _capture_fh_score(cur, match_db_id, m["status"], m["score_h"], m["score_a"])

    conn.commit()
    conn.close()
    # istemci detayli istatistik taramasi icin batch slotlarini SADECE burada
    # kabul edilen (is_known_match'ten gecen) maclara ayirsin diye - aksi
    # halde filtrelenmis (obscure) maclar icin bosuna sayfa ziyareti yapip
    # 404 aliyordu.
    kabul_edilen = [m["ext_id"] for m in to_write]
    return {"success": True, "islenen": len(to_write), "yeni": yeni_sayisi, "kabul_edilen": kabul_edilen}


_FS_STAT_FIELDS = {
    "possession", "shots", "shots_on_target", "shots_off_target",
    "corners", "red_cards", "attacks", "dangerous_attacks", "big_chances", "xg",
}


@app.post("/api/admin/live-stats-update")
def live_stats_update(request: Request, payload: dict):
    """flashscore_xg_client.py'nin bir macin istatistik sayfasindan scrape
    ettigi degerleri (top hakimiyeti, sut, korner, xG vb. - hangileri
    mevcutsa) yazar.

    live_snapshots'a YENI SATIR eklemiyoruz - /api/admin/live-sync zaten her
    dongude taze bir satir aciyor (bkz. o uc'un docstring'i). Burasi SADECE
    EN SON satirin GELEN alanlarini UPDATE ediyor - Flashscore'un istatistik
    widget'i macdan maca DEGISKEN kategoriler gosterdigi icin (bazen korner/
    kart hic yok), gelmeyen alanlara DOKUNMUYORUZ (0 yazip UYDURMUYORUZ)."""
    from fastapi.responses import JSONResponse
    expected = os.environ.get("BACKUP_SECRET") or os.environ.get("SECRET_KEY")
    provided = request.headers.get("x-backup-secret")
    if not expected or provided != expected:
        return JSONResponse({"error": "yetkisiz"}, status_code=403)

    source = _fs_valid_source(payload.get("source"))
    ext_id = payload.get("ext_id") or payload.get("fs_id")
    stats = payload.get("stats") or {}
    if not ext_id or not stats:
        return JSONResponse({"error": "ext_id/stats gerekli"}, status_code=400)

    set_parts = []
    params = []
    for key, pair in stats.items():
        if key not in _FS_STAT_FIELDS or not isinstance(pair, (list, tuple)) or len(pair) != 2:
            continue
        set_parts.append(f"home_{key} = ?, away_{key} = ?")
        params.extend([pair[0], pair[1]])
    if not set_parts:
        return JSONResponse({"error": "gecerli stat alani yok"}, status_code=400)

    conn = connect()
    cur = conn.cursor()
    cur.execute("SELECT id FROM matches WHERE source_match_id = ?", (f"{source}_{ext_id}",))
    row = cur.fetchone()
    if not row:
        conn.close()
        return JSONResponse({"error": "mac bulunamadi"}, status_code=404)
    match_db_id = row[0]
    sql = (f"UPDATE live_snapshots SET {', '.join(set_parts)} "
           "WHERE id = (SELECT id FROM live_snapshots WHERE match_id = ? ORDER BY id DESC LIMIT 1)")
    params.append(match_db_id)
    cur.execute(sql, params)
    updated = cur.rowcount
    conn.commit()
    conn.close()
    return {"success": True, "updated_rows": updated}


# ---------------------------------------------------------------------------
# Kimlik dogrulama uclari
# ---------------------------------------------------------------------------

class Credentials(BaseModel):
    email: str
    password: str


# Lansman oncesi (2026-08-28) eklendi: giris/kayit uclarinda hicbir deneme
# siniri yoktu - sinirsiz sifre denemesi / sahte hesap acma riski. Basit,
# bellek-ici kayan pencere: ayni IP+e-posta kombinasyonu 15 dakikada en fazla
# 8 kez denenebilir. Surec yeniden baslarsa sifirlanir (kalici depolama
# gerektirmeyecek kadar hafif bir koruma - amac botlari yavaslatmak).
_RATE_LIMIT_WINDOW_SECONDS = 900
_RATE_LIMIT_MAX_ATTEMPTS = 8
_rate_limit_hits: dict[str, deque] = defaultdict(deque)


def _client_ip(request: Request) -> str:
    # Railway'in kendi ic proxy'si her istekte FARKLI bir dahili IP (100.64.x.x,
    # CGNAT araligi) ile gorunuyor - request.client.host guvenilmez (2026-08-28'de
    # canli test sirasinda ayni curl kaynagindan 3 istek 3 farkli IP verdi, rate
    # limit hicbir zaman tetiklenmedi). Gercek istemci IP'si X-Forwarded-For'un
    # ILK degeri (proxy zinciri: client -> Railway edge -> ... -> bu servis).
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "?"


def _rate_limited(request: Request, email: str) -> bool:
    key = f"{_client_ip(request)}:{auth.normalize_email(email)}"
    now = time.time()
    hits = _rate_limit_hits[key]
    while hits and now - hits[0] > _RATE_LIMIT_WINDOW_SECONDS:
        hits.popleft()
    if len(hits) >= _RATE_LIMIT_MAX_ATTEMPTS:
        return True
    hits.append(now)
    return False


# Toplu veri kazima (scraping) koruması (2026-08-28, kullanıcı talebi: "bizden
# veri çekmelerini istemiyorum"). Normal kullanım (frontend her 15sn'de bir
# fetchData cagiriyor, birden fazla sekme/uye ayni IP'yi paylasabilir) rahatca
# sigar; toplu/otomatik cekim (saniyede onlarca istek) engellenir. Login rate
# limitinden AYRI - burada e-posta yok, IP+endpoint bazli, pencere daha kisa.
_SCRAPE_GUARD_WINDOW_SECONDS = 60
_SCRAPE_GUARD_MAX_REQUESTS = 90
_scrape_guard_hits: dict[str, deque] = defaultdict(deque)


def _scrape_guarded(request: Request, bucket: str) -> bool:
    key = f"{bucket}:{_client_ip(request)}"
    now = time.time()
    hits = _scrape_guard_hits[key]
    while hits and now - hits[0] > _SCRAPE_GUARD_WINDOW_SECONDS:
        hits.popleft()
    if len(hits) >= _SCRAPE_GUARD_MAX_REQUESTS:
        return True
    hits.append(now)
    return False


def _set_session_cookie(response: Response, user_id: int, request: Request):
    token = auth.create_session_token(user_id)
    # https uzerinden servis ediliyorsa cookie'yi sadece guvenli baglantida gonder
    is_https = request.url.scheme == "https" or \
        request.headers.get("x-forwarded-proto", "") == "https"
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=auth.SESSION_TTL_SECONDS,
        httponly=True,       # JavaScript cookie'yi okuyamaz (XSS'e karsi)
        secure=is_https,
        samesite="lax",      # baska sitelerden gelen isteklerde gonderilmez (CSRF'e karsi)
        path="/",
    )


@app.post("/api/register")
def register(creds: Credentials, request: Request, response: Response):
    if _rate_limited(request, creds.email):
        return {"success": False, "error": "Çok fazla deneme yapıldı. Birkaç dakika sonra tekrar deneyin."}
    user_id, error = auth.create_user(creds.email, creds.password)
    if error:
        return {"success": False, "error": error}
    _set_session_cookie(response, user_id, request)
    return {"success": True, "email": auth.normalize_email(creds.email)}


@app.post("/api/login")
def login(creds: Credentials, request: Request, response: Response):
    if _rate_limited(request, creds.email):
        return {"success": False, "error": "Çok fazla deneme yapıldı. Birkaç dakika sonra tekrar deneyin."}
    user_id, error = auth.authenticate(creds.email, creds.password)
    if error:
        return {"success": False, "error": error}
    _set_session_cookie(response, user_id, request)
    return {"success": True, "email": auth.normalize_email(creds.email)}


class ForgotPasswordRequest(BaseModel):
    email: str


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str


def _send_email(to_email: str, subject: str, html: str) -> bool:
    """Resend uzerinden e-posta gonderir. Basarisizsa (API key eksik/hatali,
    domain dogrulanmamis vb.) False doner ve loglar - cagiran taraf bunu
    kullaniciya "sunucu hatasi" olarak degil, genel "bir sorun olustu" olarak
    yansitmali (bkz. /api/forgot-password - e-posta var/yok bilgisini de sizdirmaz)."""
    api_key = os.environ.get("RESEND_API_KEY")
    if not api_key:
        print("[email] RESEND_API_KEY tanimli degil, e-posta gonderilemedi.")
        return False
    from_addr = os.environ.get("RESEND_FROM_EMAIL", "Matchrix <onboarding@resend.dev>")
    try:
        r = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"from": from_addr, "to": [to_email], "subject": subject, "html": html},
            timeout=15,
        )
        if r.status_code >= 300:
            print(f"[email] Resend hatasi ({r.status_code}): {r.text[:300]}")
            return False
        return True
    except Exception as e:
        print(f"[email] Gonderim istisnasi: {e}")
        return False


@app.post("/api/forgot-password")
def forgot_password(payload: ForgotPasswordRequest, request: Request):
    if _rate_limited(request, payload.email):
        return {"success": False, "error": "Çok fazla deneme yapıldı. Birkaç dakika sonra tekrar deneyin."}

    # E-posta kayitli olsun ya da olmasin AYNI basarili mesaj donuyor - aksi
    # halde disaridan "bu e-posta kayitli mi" sorgulanabilir (user enumeration).
    user_id = auth.find_user_id_by_email(payload.email)
    if user_id is not None:
        token = auth.create_reset_token(user_id)
        reset_link = f"{str(request.base_url).rstrip('/')}/?reset_token={token}"
        _send_email(
            auth.normalize_email(payload.email),
            "Matchrix - Şifre Sıfırlama",
            f"""
            <div style="font-family:sans-serif; max-width:480px; margin:0 auto;">
                <h2>Şifreni sıfırla</h2>
                <p>Şifreni sıfırlamak için aşağıdaki bağlantıya tıkla. Bu bağlantı 30 dakika geçerlidir.</p>
                <p><a href="{reset_link}" style="background:#00e5ff; color:#001014; padding:12px 20px; text-decoration:none; border-radius:8px; font-weight:bold; display:inline-block;">Şifremi Sıfırla</a></p>
                <p style="color:#888; font-size:13px;">Bu isteği sen yapmadıysan bu e-postayı yok sayabilirsin.</p>
            </div>
            """,
        )
    return {"success": True, "message": "Eğer bu e-posta kayıtlıysa, sıfırlama bağlantısı gönderildi."}


@app.post("/api/reset-password")
def reset_password(payload: ResetPasswordRequest):
    user_id = auth.verify_reset_token(payload.token)
    if user_id is None:
        return {"success": False, "error": "Bağlantının süresi dolmuş veya geçersiz. Yeniden talep et."}
    ok, error = auth.set_password(user_id, payload.new_password)
    if not ok:
        return {"success": False, "error": error}
    return {"success": True}


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"success": True}


@app.get("/api/me")
def me(request: Request):
    user_id = current_user_id(request)
    if not user_id:
        return {"authenticated": False}
    email = auth.get_user_email(user_id)
    if not email:
        # Kullanici silinmis ama cookie duruyorsa oturumu gecersiz say
        return {"authenticated": False}
    return {"authenticated": True, "email": email}


# --- Google ile giris (OAuth 2.0 authorization code akisi) -----------------
# NEDEN: e-posta/sifreye ek, tek tikla giris. GOOGLE_CLIENT_ID/SECRET Railway
# Variables'ta tanimli olmali (bkz. CLAUDE_DEPLOYMENT_HANDOVER.md) - Google
# Cloud Console'da bir OAuth Client (Web application) olusturulup, "Authorized
# redirect URI" olarak <SITE_URL>/api/auth/google/callback eklenmeli. Bu iki
# adim sadece hesap sahibi tarafindan yapilabilir, kod tarafinda otomatiklestirilemez.
GOOGLE_STATE_COOKIE = "jcode_oauth_state"
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"


def _google_redirect_uri(request: Request) -> str:
    # Railway, TLS'i proxy'de sonlandirip uygulamaya duz HTTP ile iletiyor -
    # request.base_url bu yuzden "http://" doner. Google Cloud Console'a
    # kayitli URI "https://" oldugu icin, sema burada X-Forwarded-Proto'ya
    # gore duzeltilmezse Google "redirect_uri_mismatch" hatasi verir.
    is_https = request.url.scheme == "https" or \
        request.headers.get("x-forwarded-proto", "") == "https"
    scheme = "https" if is_https else request.url.scheme
    host = request.url.hostname
    port = request.url.port
    if port and port not in (80, 443):
        host = f"{host}:{port}"
    return f"{scheme}://{host}/api/auth/google/callback"


@app.get("/api/auth/google/login")
def google_login(request: Request):
    from fastapi.responses import RedirectResponse, JSONResponse

    client_id = os.environ.get("GOOGLE_CLIENT_ID")
    if not client_id:
        return JSONResponse(
            {"error": "Google ile giriş henüz yapılandırılmadı."}, status_code=503)

    state = secrets.token_urlsafe(24)
    params = {
        "client_id": client_id,
        "redirect_uri": _google_redirect_uri(request),
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "prompt": "select_account",
    }
    redirect_response = RedirectResponse(f"{GOOGLE_AUTH_URL}?{urlencode(params)}")
    is_https = request.url.scheme == "https" or \
        request.headers.get("x-forwarded-proto", "") == "https"
    redirect_response.set_cookie(
        key=GOOGLE_STATE_COOKIE, value=state, max_age=600,
        httponly=True, secure=is_https, samesite="lax", path="/",
    )
    return redirect_response


@app.get("/api/auth/google/callback")
def google_callback(request: Request, code: str = None, state: str = None, error: str = None):
    from fastapi.responses import RedirectResponse

    expected_state = request.cookies.get(GOOGLE_STATE_COOKIE)
    if error or not code or not expected_state or state != expected_state:
        return RedirectResponse("/?auth_error=1")

    client_id = os.environ.get("GOOGLE_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET")
    if not client_id or not client_secret:
        return RedirectResponse("/?auth_error=1")

    try:
        token_res = requests.post(GOOGLE_TOKEN_URL, data={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": _google_redirect_uri(request),
            "grant_type": "authorization_code",
        }, timeout=10)
        access_token = token_res.json().get("access_token") if token_res.ok else None
        if not access_token:
            return RedirectResponse("/?auth_error=1")

        userinfo_res = requests.get(
            GOOGLE_USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"}, timeout=10)
        email = userinfo_res.json().get("email") if userinfo_res.ok else None
        if not email:
            return RedirectResponse("/?auth_error=1")
    except requests.RequestException:
        return RedirectResponse("/?auth_error=1")

    user_id = auth.get_or_create_oauth_user(email)
    redirect_response = RedirectResponse("/")
    _set_session_cookie(redirect_response, user_id, request)
    redirect_response.delete_cookie(GOOGLE_STATE_COOKIE, path="/")
    return redirect_response

@app.get("/api/live-matches")
def get_live_matches(request: Request):
    if _scrape_guarded(request, "live-matches"):
        return {"success": False, "error": "Çok fazla istek. Lütfen biraz yavaşlayın."}
    is_member = current_user_id(request) is not None
    conn = connect()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT m.home_team_id, m.away_team_id, m.home_score, m.away_score, m.minute,
               p.signal_level, p.weighted_probability, p.decision, m.league_name, m.league_logo, m.id,
               m.home_team_logo, m.away_team_logo, s.home_score, s.away_score, m.status, s.minute,
               fh.fh_end_home, fh.fh_end_away, p.created_at, p.signal_minute, p.market, p.outcome,
               date(p.created_at, '+3 hours') AS signal_date,
               date(COALESCE(p.settled_at, p.created_at), '+3 hours') AS result_date,
               lb.lead_bot
        FROM matches m
        JOIN consensus_predictions p ON m.id = p.match_id
        LEFT JOIN live_snapshots s ON p.snapshot_id = s.id
        LEFT JOIN (
            SELECT ls1.match_id, ls1.home_score AS fh_end_home, ls1.away_score AS fh_end_away
            FROM live_snapshots ls1
            INNER JOIN (
                SELECT match_id, MAX(minute) AS max_min
                FROM live_snapshots
                WHERE minute <= 45
                GROUP BY match_id
            ) ls2 ON ls1.match_id = ls2.match_id AND ls1.minute = ls2.max_min
        ) fh ON fh.match_id = m.id
        LEFT JOIN (
            -- Konsensuse "goal" diyen botlar arasinda en yuksek olasilik veren
            -- (yani sinyali en cok sahiplenen) bot - karta "hangi bot" etiketi
            -- icin. Esitlik durumunda GROUP BY rastgele birini secer, sorun degil.
            SELECT match_id, snapshot_id, bot_name AS lead_bot
            FROM bot_predictions b1
            WHERE decision = 'goal'
              AND probability = (
                  SELECT MAX(probability) FROM bot_predictions b2
                  WHERE b2.match_id = b1.match_id AND b2.snapshot_id = b1.snapshot_id
                    AND b2.decision = 'goal'
              )
            GROUP BY match_id, snapshot_id
        ) lb ON lb.match_id = p.match_id AND lb.snapshot_id = p.snapshot_id
        ORDER BY p.created_at DESC
        LIMIT 5000
    ''')
    rows = cursor.fetchall()
    conn.close()

    # Bir mac artik her yarida en fazla bir sinyal uretiyor. Ilk yari sinyali
    # sonuclandiktan sonra ikinci yarida yeni bir sinyal acilabilir; bunlari yalnizca
    # match_id ile gruplarsak eski WON kaydi yeni PENDING kaydini gizler. Bu nedenle
    # gorunurluk birimi mac + yari: H1 sonucu gecmiste kalir, H2 acik sinyali kaybolmaz.
    by_match_half = {}

    for r in rows:
        minute = r[4]
        current_home = r[2]
        current_away = r[3]
        initial_home = r[13] if r[13] is not None else current_home
        initial_away = r[14] if r[14] is not None else current_away
        match_status = r[15]
        fh_end_home = r[17] if len(r) > 17 else None
        fh_end_away = r[18] if len(r) > 18 else None
        match_id = r[10]
        # p.signal_minute / p.market: sinyal uretildigi anda SABIT olarak kaydedilmis degerler
        # (bkz. orchestrator.py). Eski (bu kolonlar eklenmeden once atilmis) sinyaller icin
        # None gelir; o durumda eski (kirilgan) fallback'e geri donulur.
        stored_signal_minute = r[20] if len(r) > 20 else None
        stored_market = r[21] if len(r) > 21 else None
        signal_minute = stored_signal_minute if stored_signal_minute is not None else (
            r[16] if (len(r) > 16 and r[16] is not None) else minute
        )

        total_goals_now = current_home + current_away
        total_goals_initial = initial_home + initial_away

        is_first_half_market = signal_minute <= 45

        # ONCE KALICI SONUCA BAK. Sinyal bir kez sonuclandiysa (settlement.py) o sonuc
        # degismez - mac verisi sonradan degisse/eskise bile gecmis bozulmaz.
        # Sadece henuz sonuclanmamis (mac devam eden) sinyaller anlik hesaplanir.
        stored_outcome = r[22] if len(r) > 22 else None

        if stored_outcome in ("WON", "LOST", "VOID"):
            outcome = stored_outcome
        else:
            outcome = settlement.compute_outcome(
                signal_minute, total_goals_initial, current_home, current_away,
                match_status, minute, fh_end_home, fh_end_away,
            ) or "PENDING"

        market = stored_market if stored_market else (f"İlk Yarı {total_goals_initial + 0.5} Üst" if is_first_half_market else f"Maç Sonu {total_goals_initial + 0.5} Üst")

        entry = {
            "home_team": r[0],
            "away_team": r[1],
            "home_score": current_home,
            "away_score": current_away,
            "minute": minute,
            "match_status": match_status,
            "market": market,
            "probability": round(r[6], 3),
            "confidence": f"Seviye: {r[5]}",
            "league_name": r[8] if r[8] else "Canlı Alarmlar",
            "league_logo": r[9] if r[9] else "",
            "match_id": match_id,
            "home_logo": r[11] if r[11] else f"https://ui-avatars.com/api/?name={r[0].replace(' ', '+')}&background=1f2937&color=00e5ff",
            "away_logo": r[12] if r[12] else f"https://ui-avatars.com/api/?name={r[1].replace(' ', '+')}&background=1f2937&color=00e5ff",
            "outcome": outcome,
            "signal_minute": signal_minute,
            "signal_period": "H1" if signal_minute <= 45 else "H2",
            "signal_date": r[23] if len(r) > 23 else None,
            "result_date": r[24] if len(r) > 24 else None,
            "lead_bot": r[25] if len(r) > 25 else None,
            "created_at": r[19] if len(r) > 19 else None
        }

        # rows p.created_at DESC sirali; mac+yari icin ilk gordugumuz kayit
        # ilgili sonuc kategorisinin en guncel kaydidir.
        half_key = "H1" if signal_minute <= 45 else "H2"
        group_key = (match_id, half_key)
        bucket = by_match_half.setdefault(
            group_key, {"won": None, "pending": None, "lost": None, "void": None}
        )
        if entry["outcome"] == "WON" and bucket["won"] is None:
            bucket["won"] = entry
        elif entry["outcome"] == "PENDING" and bucket["pending"] is None:
            bucket["pending"] = entry
        elif entry["outcome"] == "LOST" and bucket["lost"] is None:
            bucket["lost"] = entry
        elif entry["outcome"] == "VOID" and bucket["void"] is None:
            bucket["void"] = entry

    results = []
    for (_match_id, _half), bucket in by_match_half.items():
        if bucket["won"] is not None:
            chosen = bucket["won"]
        elif bucket["pending"] is not None:
            chosen = bucket["pending"]
        elif bucket["lost"] is not None:
            chosen = bucket["lost"]
        else:
            chosen = bucket["void"]
        results.append(chosen)

    conn = connect()
    today_tr = conn.execute("SELECT date('now', '+3 hours')").fetchone()[0]
    conn.close()

    # Kullanici talebi (2026-08-28, lansman oncesi): ucretsiz onizleme kaldirildi -
    # uye olmayan HICBIR tahmini goremez, gunun ilk 3'u istisnasi da dahil.
    for chosen in results:
        chosen.pop("created_at", None)

        if not is_member:
            # UYE DEGILSE VE gunun ucretsiz 3'unden biri DEGILSE HICBIR SEY
            # gonderilmez - sadece tahmin degil, hangi macin takip edildigi de
            # (takim adi, skor, dakika, lig, logo, match_id). Kullanici talebi:
            # aktif maclari gormek icin uye olmak sart olsun, kart tamamen
            # bulaniklassin. Sadece ekrani CSS ile bulaniklastirmak koruma
            # SAGLAMAZ (kullanici sayfa kaynagina/gelistirici konsoluna bakip
            # veriyi okur) - bu yuzden hassas alanlar sunucuda siliniyor;
            # on yuzdeki bulanik gorunum yalnizca dekoratif.
            chosen["market"] = None
            chosen["probability"] = None
            chosen["confidence"] = None
            chosen["signal_minute"] = None
            chosen["lead_bot"] = None
            chosen["home_team"] = None
            chosen["away_team"] = None
            chosen["home_score"] = None
            chosen["away_score"] = None
            chosen["minute"] = None
            chosen["match_status"] = None
            chosen["league_name"] = None
            chosen["league_logo"] = None
            chosen["home_logo"] = None
            chosen["away_logo"] = None
            chosen["match_id"] = None
            # NOT: outcome BILEREK null'lanmiyor - frontend'in 'today'/'open'/
            # 'results' sekme filtreleri outcome'a gore calisiyor (bkz. index.html
            # fetchData). outcome (PENDING/WON/LOST) tek basina hangi macin
            # oynandigini ifsa etmez, sadece kilitli karti dogru sekmede
            # bulaniklastirilmis olarak gostermeye yarar.
            chosen["locked"] = True

    return {"success": True, "data": results, "is_member": is_member,
            "today_tr": today_tr}


@app.get("/api/bet-assistant/latest")
def get_bet_assistant_latest(request: Request):
    """Kisisel tarayici yardimcisi icin en yeni acik sinyali dondurur.

    Uyelik cookie'si ucuncu taraf bir siteden guvenle kullanilamayacagi icin bu
    uc ayri bir ortam degiskeniyle korunur. Anahtar yoksa uc tamamen kapalidir.
    """
    from fastapi.responses import JSONResponse
    import hmac

    expected = os.environ.get("BET_ASSISTANT_TOKEN", "")
    authorization = request.headers.get("authorization", "")
    bearer = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
    provided = bearer or request.headers.get("x-bet-assistant-token", "").strip()
    if not expected:
        return JSONResponse(
            {"success": False, "error": "BET_ASSISTANT_TOKEN Railway servisinde tanimli degil"},
            status_code=503,
        )
    if not provided:
        return JSONResponse(
            {"success": False, "error": "Safari yetkilendirme basligini gondermedi"},
            status_code=401,
        )
    if not hmac.compare_digest(provided, expected.strip()):
        return JSONResponse(
            {"success": False, "error": "Safari ve Railway anahtarlari eslesmiyor"},
            status_code=403,
        )

    conn = connect()
    conn.row_factory = sqlite3.Row
    row = conn.execute('''
        SELECT m.id AS match_id, m.home_team_id AS home_team,
               m.away_team_id AS away_team, m.home_score, m.away_score,
               m.minute, m.status AS match_status, p.market,
               p.weighted_probability AS probability,
               p.signal_level, p.signal_minute, p.created_at
        FROM consensus_predictions p
        JOIN matches m ON m.id = p.match_id
        WHERE p.decision = 'signal'
          AND p.outcome IS NULL
          AND m.status = 'LIVE'
          AND p.market IS NOT NULL
        ORDER BY p.created_at DESC, p.id DESC
        LIMIT 1
    ''').fetchone()
    conn.close()

    if row is None:
        return {"success": True, "data": None}

    return {
        "success": True,
        "data": {
            "match_id": row["match_id"],
            "home_team": row["home_team"],
            "away_team": row["away_team"],
            "home_score": row["home_score"],
            "away_score": row["away_score"],
            "minute": row["minute"],
            "match_status": row["match_status"],
            "market": row["market"],
            "probability": round(row["probability"], 3),
            "confidence": f"Seviye: {row['signal_level']}",
            "signal_minute": row["signal_minute"],
            "created_at": row["created_at"],
        },
    }


@app.get("/api/daily-results")
def get_daily_results(request: Request):
    """Sonuclari sinyalin uretildigi Turkiye gunune gore gruplar."""
    payload = get_live_matches(request)
    days = {}
    for match in payload["data"]:
        outcome = match.get("outcome")
        # Gunluk test yalnizca o gun GERCEKTEN paylasilan takip edilebilir
        # sinyallerdir. VOID/gozlem disi kayitlar test tablosuna girmez.
        if outcome not in ("WON", "LOST", "PENDING"):
            continue
        day = match.get("signal_date") or "Tarihsiz"
        bucket = days.setdefault(day, {
            "date": day, "won": 0, "lost": 0, "pending": 0, "matches": []
        })
        if outcome == "WON":
            bucket["won"] += 1
        elif outcome == "LOST":
            bucket["lost"] += 1
        else:
            bucket["pending"] += 1
        bucket["matches"].append(match)

    result = []
    for day in sorted(days, reverse=True):
        bucket = days[day]
        measured = bucket["won"] + bucket["lost"]
        bucket["total"] = measured + bucket["pending"]
        bucket["isabet_orani"] = round(bucket["won"] / measured, 3) if measured else None
        result.append(bucket)
    return {"success": True, "days": result, "today_tr": payload["today_tr"],
            "is_member": payload["is_member"]}

@app.get("/api/ozet")
def get_ozet(request: Request):
    """Herkese acik sonuc ozeti - seffaflik kozu, UYELIK GEREKTIRMEZ.

    _hesapla_metrics()'teki 'ozet' bloguyla ayni hesap; farki uyelik/admin
    sarti olmamasi. Bot bazli detay ve kalibrasyon gibi rekabete hassas
    veriler burada YOK, onlar /api/admin/panel/istatistikler'de sadece
    yoneticiye ozel kalmaya devam ediyor.
    """
    if _scrape_guarded(request, "ozet"):
        return {"success": False, "error": "Çok fazla istek. Lütfen biraz yavaşlayın."}
    conn = connect()
    cur = conn.cursor()

    # TUM ZAMANLAR toplami - seffaflik seridi kucuk gunluk orneklemle (bazi gunler
    # 0/0) yanitlayip yanlis izlenim vermesin diye kalici, olculmus toplam gosterilir.
    # Gunun kendi hareketi ayrica bugun_kazanan/bugun_kaybeden alanlarinda donuyor.
    cur.execute("""
        SELECT outcome, COUNT(*) FROM consensus_predictions
        WHERE decision='signal' AND outcome IN ('WON','LOST') GROUP BY outcome
    """)
    tally = dict(cur.fetchall())
    won = tally.get("WON", 0)
    lost = tally.get("LOST", 0)
    settled = won + lost

    cur.execute("SELECT COUNT(*) FROM consensus_predictions WHERE decision='signal' AND outcome='VOID'")
    void = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM consensus_predictions WHERE decision='signal' AND outcome IS NULL")
    pending = cur.fetchone()[0]

    # Anasayfadaki "+500 Veri Kaynagi" rozeti sabit yaziliydi (kullanici
    # talebi, 2026-08-28: "gercek yap") - benzersiz lig/turnuva sayisi,
    # verinin gercekte kac farkli kaynaktan geldiginin olculebilir bir vekili.
    cur.execute("SELECT COUNT(DISTINCT league_name) FROM matches WHERE league_name IS NOT NULL AND league_name != ''")
    lig_sayisi = cur.fetchone()[0]

    # Bugun (Turkiye saatiyle, UTC+3 - Turkiye yaz saati uygulamiyor) PAYLASILAN
    # (uretilme tarihine gore) tum sinyaller, sonuc durumuna gore kirilmis -
    # genel (tum-zamanlar) seridin hemen altinda ikinci, gune ozel bir serit
    # icin. created_at SQLite CURRENT_TIMESTAMP oldugu icin UTC'dir.
    cur.execute("""
        SELECT outcome, COUNT(*) FROM consensus_predictions
        WHERE decision='signal'
          AND date(created_at, '+3 hours') = date('now', '+3 hours')
        GROUP BY outcome
    """)
    bugun = {(k if k is not None else "PENDING"): v for k, v in cur.fetchall()}

    # Guncel kazanma serisi (2026-08-29, kullanici talebi): en son sonuclanan
    # sinyalden geriye dogru, ilk LOST'a kadar art arda kac WON var. PENDING
    # sinyaller diziden tamamen haric (ne seriyi bozar ne uzatir) - sadece
    # sonuclanmis (WON/LOST) sinyaller sirayla taranir.
    cur.execute("""
        SELECT outcome FROM consensus_predictions
        WHERE decision='signal' AND outcome IN ('WON','LOST')
        ORDER BY created_at DESC LIMIT 50
    """)
    guncel_seri = 0
    for (o,) in cur.fetchall():
        if o == 'WON':
            guncel_seri += 1
        else:
            break
    conn.close()

    bugun_kazanan = bugun.get("WON", 0)
    bugun_kaybeden = bugun.get("LOST", 0)
    bugun_sonuclanan = bugun_kazanan + bugun_kaybeden

    return {
        "success": True,
        "sonuclanan": settled,
        "kazanan": won,
        "kaybeden": lost,
        "isabet_orani": round(won / settled, 3) if settled else None,
        "guncel_seri": guncel_seri,
        "bekleyen": pending,
        "gozlem_disi": void,
        "lig_sayisi": lig_sayisi,
        "bugun_kazanan": bugun_kazanan,
        "bugun_kaybeden": bugun_kaybeden,
        # VOID (gozlem disi) artik kalici bir kategori degil - reconcile_void_signals()
        # kesin sonuca ulasanlari WON/LOST yapiyor, delete_unresolvable_void() asla
        # cozulemeyecekleri siliyor (bkz. settlement.py). Bu yuzden "paylasilan" sayisina
        # VOID dahil edilmiyor - kullanici talebi.
        "bugun_paylasilan": sum(bugun.values()) - bugun.get("VOID", 0),
        "bugun_sonuclanan": bugun_sonuclanan,
        "bugun_isabet_orani": round(bugun_kazanan / bugun_sonuclanan, 3) if bugun_sonuclanan else None,
        "bugun_bekleyen": bugun.get("PENDING", 0),
        "bugun_gecersiz": bugun.get("VOID", 0),
    }


@app.get("/api/ozet-donem")
def get_ozet_donem(request: Request, donem: str = "tum"):
    """Ana sayfadaki tiklanabilir Bu Hafta/Bu Ay/Tum Zamanlar ozeti - herkese
    acik, uyelik gerektirmez. Kullanici talebiyle (2026-08-21) bot bazli
    detaylar/kalibrasyon admin paneline tasindi (bkz. /api/admin/panel/
    istatistikler); ana sitede artik SADECE bu sadelestirilmis donem ozeti
    gosteriliyor."""
    if _scrape_guarded(request, "ozet-donem"):
        return {"success": False, "error": "Çok fazla istek. Lütfen biraz yavaşlayın."}
    if donem == "bugun":
        where = "date(created_at, '+3 hours') = date('now', '+3 hours')"
    elif donem == "hafta":
        # Bu haftanin Pazartesi'si (Turkiye saatiyle +3): 6 gun geri gidip
        # ileri dogru en yakin Pazartesi'ye (weekday 1) yuvarla.
        where = "date(created_at, '+3 hours') >= date('now', '+3 hours', '-6 days', 'weekday 1')"
    elif donem == "ay":
        where = "strftime('%Y-%m', created_at, '+3 hours') = strftime('%Y-%m', 'now', '+3 hours')"
    else:
        donem = "tum"
        where = "1=1"

    conn = connect()
    cur = conn.cursor()
    cur.execute(f"""
        SELECT outcome, COUNT(*) FROM consensus_predictions
        WHERE decision='signal' AND outcome IN ('WON','LOST') AND {where}
        GROUP BY outcome
    """)
    tally = dict(cur.fetchall())
    conn.close()

    won = tally.get("WON", 0)
    lost = tally.get("LOST", 0)
    settled = won + lost
    return {
        "success": True, "donem": donem,
        "kazanan": won, "kaybeden": lost, "sonuclanan": settled,
        "isabet_orani": round(won / settled, 3) if settled else None,
    }


@app.get("/api/kasa-ozet")
def get_kasa_ozet(request: Request):
    """Kasa yonetimi kartinin dayandigi olculmus istatistikler - herkese acik,
    uyelik gerektirmez. Kullanici talebi (2026-08-28): 'insanlara haftasini
    karli kapatmasi icin bir sistem' - ama CLAUDE.md'nin 'olcmeden iddia yok'
    ilkesine uyarak SABIT sayi yazmak yerine son 30 gunun gercek verisinden
    canli hesaplaniyor, zamanla otomatik guncel kalir."""
    if _scrape_guarded(request, "kasa-ozet"):
        return {"success": False, "error": "Çok fazla istek. Lütfen biraz yavaşlayın."}

    conn = connect()
    cur = conn.cursor()
    cur.execute("""
        SELECT outcome, date(created_at, '+3 hours') AS gun
        FROM consensus_predictions
        WHERE decision='signal' AND outcome IN ('WON','LOST')
          AND date(created_at, '+3 hours') >= date('now', '+3 hours', '-30 days')
    """)
    rows = cur.fetchall()
    conn.close()

    if not rows:
        return {"success": True, "haftalik_ortalama_sinyal": None, "isabet_orani": None,
                "veri_gun_sayisi": 0}

    gunler = {r[1] for r in rows}
    won = sum(1 for r in rows if r[0] == "WON")
    lost = sum(1 for r in rows if r[0] == "LOST")
    settled = won + lost
    # Takvim gunu bazinda haftalik ortalama (sinyal olmayan gunler de payda'ya
    # girer - boylece "haftada ortalama X" gercekten bir hafta boyunca
    # beklenebilecek hacmi yansitir, sadece aktif gunlerin ortalamasi degil).
    ilk_gun = min(gunler)
    son_gun = max(gunler)
    from datetime import datetime as _dt
    takvim_gun = (_dt.strptime(son_gun, "%Y-%m-%d") - _dt.strptime(ilk_gun, "%Y-%m-%d")).days + 1
    haftalik_ortalama = round(settled / takvim_gun * 7, 1) if takvim_gun else None

    return {
        "success": True,
        "haftalik_ortalama_sinyal": haftalik_ortalama,
        "isabet_orani": round(won / settled, 3) if settled else None,
        "veri_gun_sayisi": takvim_gun,
        "sonuclanan_30gun": settled,
    }


def _hesapla_metrics():
    """Bot bazli isabet orani + konsensus kalibrasyonu.

    "Piyasanin en iyisi" iddiasi ancak olculebilirse anlamlidir. Bu fonksiyon,
    sonuclanmis (outcome dolu) sinyaller uzerinden calisir - anlik tahmin
    degil, gecmis gercek. Eskiden /api/metrics olarak uye girisiyle
    korunuyordu; kullanici talebiyle (2026-08-21) ana siteden tamamen
    kaldirilip admin paneline tasindi (bkz. /api/admin/panel/istatistikler)."""
    conn = connect()
    cur = conn.cursor()

    # --- Genel tablo ---
    cur.execute("""
        SELECT outcome, COUNT(*) FROM consensus_predictions
        WHERE decision='signal' AND outcome IN ('WON','LOST') GROUP BY outcome
    """)
    tally = dict(cur.fetchall())
    won = tally.get("WON", 0)
    lost = tally.get("LOST", 0)
    settled = won + lost

    cur.execute("SELECT COUNT(*) FROM consensus_predictions WHERE decision='signal' AND outcome='VOID'")
    void = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM consensus_predictions WHERE decision='signal' AND outcome IS NULL")
    pending = cur.fetchone()[0]

    # --- Bot bazli isabet ---
    # Her botun "goal" dedigi sinyallerde gercekten gol gelmis mi?
    cur.execute("""
        SELECT b.bot_name,
               COUNT(*) AS n,
               SUM(CASE WHEN c.outcome='WON' THEN 1 ELSE 0 END) AS hits,
               ROUND(AVG(b.probability), 3) AS avg_prob
        FROM bot_predictions b
        JOIN consensus_predictions c
          ON c.match_id = b.match_id AND c.snapshot_id = b.snapshot_id
        WHERE c.outcome IN ('WON','LOST') AND b.decision = 'goal'
        GROUP BY b.bot_name
        HAVING n > 0
        ORDER BY (CAST(hits AS FLOAT)/n) DESC
    """)
    bots = []
    for name, n, hits, avg_prob in cur.fetchall():
        rate = round(hits / n, 3) if n else None
        bots.append({
            "bot": name,
            "sinyal_sayisi": n,
            "isabet": hits,
            "isabet_orani": rate,
            "ortalama_iddia": avg_prob,
            # Kalibrasyon farki: bot %80 diyorsa ve %55 tutuyorsa, +0.25 fazla iyimser
            "asiri_iyimserlik": round((avg_prob or 0) - (rate or 0), 3),
        })

    # --- Konsensus kalibrasyonu: iddia edilen olasilik vs gerceklesen ---
    cur.execute("""
        SELECT CAST(weighted_probability*10 AS INT) AS band,
               COUNT(*) AS n,
               SUM(CASE WHEN outcome='WON' THEN 1 ELSE 0 END) AS hits,
               ROUND(AVG(weighted_probability),3)
        FROM consensus_predictions
        WHERE decision='signal' AND outcome IN ('WON','LOST')
        GROUP BY band ORDER BY band
    """)
    calibration = []
    for band, n, hits, avg_p in cur.fetchall():
        calibration.append({
            "olasilik_araligi": f"%{band*10}-{band*10+10}",
            "sinyal_sayisi": n,
            "gerceklesen": round(hits / n, 3) if n else None,
            "iddia_edilen": avg_p,
        })

    # --- Sinyal seviyesine gore ---
    cur.execute("""
        SELECT signal_level, COUNT(*), SUM(CASE WHEN outcome='WON' THEN 1 ELSE 0 END)
        FROM consensus_predictions
        WHERE decision='signal' AND outcome IN ('WON','LOST')
        GROUP BY signal_level
    """)
    by_level = [
        {"seviye": lvl, "sinyal_sayisi": n, "isabet_orani": round(h/n, 3) if n else None}
        for lvl, n, h in cur.fetchall()
    ]

    # --- Gunluk ozet: her gun kac sinyal paylasildi, kaci kazandi/kaybetti ---
    # Yeni bir tabloya gerek yok - consensus_predictions.created_at zaten
    # kalici (outcome degismezligi kurali geregi) ve gun bazinda gruplanabilir.
    # Turkiye saatiyle (+3) gunluk sinir, /api/ozet'teki "bugun" mantigiyla ayni.
    cur.execute("""
        SELECT date(created_at, '+3 hours') AS gun,
               COUNT(*) AS paylasilan,
               SUM(CASE WHEN outcome='WON' THEN 1 ELSE 0 END) AS kazanan,
               SUM(CASE WHEN outcome='LOST' THEN 1 ELSE 0 END) AS kaybeden,
               SUM(CASE WHEN outcome='VOID' THEN 1 ELSE 0 END) AS gecersiz,
               SUM(CASE WHEN outcome IS NULL THEN 1 ELSE 0 END) AS bekleyen
        FROM consensus_predictions
        WHERE decision='signal'
        GROUP BY gun
        ORDER BY gun DESC
        LIMIT 60
    """)
    gunluk = []
    for gun, paylasilan, kazanan, kaybeden, gecersiz, bekleyen in cur.fetchall():
        sonuclanan = kazanan + kaybeden
        gunluk.append({
            "tarih": gun,
            # VOID (gecersiz) artik kalici bir kategori degil (bkz. settlement.py
            # reconcile_void_signals / delete_unresolvable_void) - paylasilan
            # sayisina dahil edilmiyor.
            "paylasilan": paylasilan - gecersiz,
            "kazanan": kazanan,
            "kaybeden": kaybeden,
            "bekleyen": bekleyen,
            "isabet_orani": round(kazanan / sonuclanan, 3) if sonuclanan else None,
        })

    conn.close()

    # --- PIYASA KARSILASTIRMASI ---
    # Asil soru "kac tuttu" degil, "piyasanin fiyatini yendik mi".
    conn2 = connect()
    c2 = conn2.cursor()
    c2.execute("""
        SELECT p.id, p.match_id, p.weighted_probability, p.outcome
        FROM consensus_predictions p
        WHERE p.decision='signal' AND p.outcome IN ('WON','LOST')
    """)
    kiyas = []
    for pid, mid, bizim, sonuc in c2.fetchall():
        try:
            piyasa = odds_mod.piyasa_gol_olasiligi(mid)
        except Exception:
            piyasa = None
        if piyasa is None:
            continue
        kiyas.append({"bizim": bizim, "piyasa": piyasa,
                      "fark": round(bizim - piyasa, 3), "sonuc": sonuc})
    conn2.close()

    piyasa_ozet = None
    if kiyas:
        n = len(kiyas)
        kazanan = sum(1 for k in kiyas if k["sonuc"] == "WON")
        ort_biz = sum(k["bizim"] for k in kiyas) / n
        ort_piy = sum(k["piyasa"] for k in kiyas) / n
        piyasa_ozet = {
            "oran_bulunan_sinyal": n,
            "bizim_ortalama_iddia": round(ort_biz, 3),
            "piyasa_ortalama": round(ort_piy, 3),
            "gerceklesen": round(kazanan / n, 3),
            "aciklama": ("Piyasa bu sinyallere ortalama %{:.0f} diyordu, biz %{:.0f} dedik, "
                         "gercekte %{:.0f} tuttu.").format(100*ort_piy, 100*ort_biz, 100*kazanan/n),
        }

    # Istatistiksel uyari: kucuk orneklemde oranlar yaniltici olur.
    note = None
    if settled < 100:
        note = (f"UYARI: Sadece {settled} sonuclanmis sinyal var. Bu sayida veriyle "
                f"bot siralamasi buyuk olcude sanstan ibarettir; guvenilir bir "
                f"karsilastirma icin en az birkac yuz sinyal gerekir.")

    return {
        "success": True,
        "ozet": {
            "sonuclanan": settled,
            "kazanan": won,
            "kaybeden": lost,
            "isabet_orani": round(won / settled, 3) if settled else None,
            "bekleyen": pending,
            "gozlem_disi": void,
        },
        "bot_performansi": bots,
        "kalibrasyon": calibration,
        "piyasa_karsilastirmasi": piyasa_ozet,
        "seviyeye_gore": by_level,
        "gunluk_ozet": gunluk,
        "uyari": note,
    }


@app.get("/api/admin/panel/istatistikler")
def admin_panel_istatistikler(request: Request):
    """Bot bazli isabet/kalibrasyon/piyasa karsilastirmasi - eskiden ana
    sitede uye girisiyle goruluyordu, kullanici talebiyle (2026-08-21)
    admin paneline tasindi (bkz. _hesapla_metrics)."""
    from fastapi.responses import JSONResponse
    if not _check_admin(request):
        return JSONResponse({"error": "yetkisiz"}, status_code=403)
    return _hesapla_metrics()


@app.get("/api/monitor")
def get_monitor(request: Request):
    """Canli sistem durumu - izleme paneli icin.

    Botlarin o an ne dusundugunu, hangi maclari taradigini ve son sinyallerde
    hangi botun ne dedigini dondurur. Uyelere ozel.
    """
    if current_user_id(request) is None:
        return {"success": False, "locked": True, "error": "Üyelere özel."}

    conn = connect()
    cur = conn.cursor()

    # --- Sistem nabzi ---
    cur.execute("SELECT COUNT(*) FROM matches WHERE status IN ('LIVE','HT')")
    canli_mac = cur.fetchone()[0]
    cur.execute("SELECT MAX(captured_at) FROM live_snapshots")
    son_veri = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM consensus_predictions WHERE decision='signal'")
    toplam_sinyal = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM consensus_predictions WHERE decision='signal' AND outcome IS NULL")
    acik = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM team_profiles")
    profil = cur.fetchone()[0]

    # --- Taranan canli maclar ---
    cur.execute("""
        SELECT m.id, m.home_team_id, m.away_team_id, m.home_score, m.away_score,
               m.minute, m.status, m.league_name,
               (SELECT COUNT(*) FROM live_snapshots s WHERE s.match_id=m.id)
        FROM matches m WHERE m.status IN ('LIVE','HT')
        ORDER BY m.minute DESC LIMIT 25
    """)
    maclar = [{
        "match_id": r[0], "ev": r[1], "dep": r[2], "skor": f"{r[3]}-{r[4]}",
        "dakika": r[5], "durum": r[6], "lig": r[7] or "-", "snapshot": r[8],
    } for r in cur.fetchall()]

    # --- Son sinyaller + o sinyalde botlarin oylari ---
    cur.execute("""
        SELECT p.id, p.match_id, p.snapshot_id, m.home_team_id, m.away_team_id,
               p.signal_minute, p.market, p.weighted_probability, p.signal_level,
               p.positive_bot_count, p.negative_bot_count, p.insufficient_data_count,
               p.outcome, p.created_at
        FROM consensus_predictions p JOIN matches m ON m.id=p.match_id
        WHERE p.decision='signal' ORDER BY p.id DESC LIMIT 8
    """)
    sinyaller = []
    for r in cur.fetchall():
        cur.execute("""
            SELECT bot_name, decision, probability FROM bot_predictions
            WHERE match_id=? AND (snapshot_id=? OR ? IS NULL)
            ORDER BY probability DESC
        """, (r[1], r[2], r[2]))
        oylar = [{"bot": b, "karar": d, "olasilik": pr} for b, d, pr in cur.fetchall()]
        sinyaller.append({
            "id": r[0], "mac": f"{r[3]} - {r[4]}", "dakika": r[5],
            "market": r[6], "olasilik": r[7], "seviye": r[8],
            "pozitif": r[9], "negatif": r[10], "cekilen": r[11],
            "sonuc": r[12] or "BEKLIYOR", "zaman": r[13], "oylar": oylar,
        })

    # --- Bot bazli ozet (sonuclanmis sinyaller uzerinden) ---
    cur.execute("""
        SELECT b.bot_name, COUNT(*),
               SUM(CASE WHEN c.outcome='WON' THEN 1 ELSE 0 END),
               SUM(CASE WHEN b.decision='insufficient_data' THEN 1 ELSE 0 END)
        FROM bot_predictions b
        LEFT JOIN consensus_predictions c
               ON c.match_id=b.match_id AND c.snapshot_id=b.snapshot_id
              AND c.outcome IN ('WON','LOST')
        GROUP BY b.bot_name ORDER BY b.bot_name
    """)
    botlar = [{"bot": r[0], "kayit": r[1], "isabet": r[2] or 0, "cekildi": r[3] or 0}
              for r in cur.fetchall()]

    conn.close()
    return {
        "success": True,
        "nabiz": {"canli_mac": canli_mac, "son_veri": son_veri,
                  "toplam_sinyal": toplam_sinyal, "acik_sinyal": acik,
                  "takim_profili": profil},
        "maclar": maclar, "sinyaller": sinyaller, "botlar": botlar,
    }


@app.get("/api/match/{match_id}")
def get_match_detail(match_id: int, request: Request):
    if _scrape_guarded(request, "match-detail"):
        return {"success": False, "error": "Çok fazla istek. Lütfen biraz yavaşlayın."}
    # Detayli analiz (xG, momentum, form) uyelere ozel. Uye olmayana veri
    # hic uretilmez - on yuzde gizlemek yeterli degil.
    if current_user_id(request) is None:
        return {"success": False, "locked": True, "error": "Bu analizi görmek için üye olun."}

    conn = connect()
    cursor = conn.cursor()

    cursor.execute('''
        SELECT home_team_id, away_team_id
        FROM matches WHERE id = ?
    ''', (match_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return {"success": False, "error": "Match not found"}

    home_team = row[0]
    away_team = row[1]

    # Canlı istatistikleri (live_snapshots) tablosundan en güncel veriyi çek
    cursor.execute('''
        SELECT home_possession, away_possession,
               home_xg, away_xg,
               home_shots, away_shots,
               home_shots_on_target, away_shots_on_target,
               home_corners, away_corners
        FROM live_snapshots
        WHERE match_id = ?
        ORDER BY minute DESC LIMIT 1
    ''', (match_id,))
    snap_row = cursor.fetchone()
    d = {}
    for team_type, team_name in [("home_form", home_team), ("away_form", away_team)]:
        cursor.execute('''
            SELECT fh_goals_scored, fh_goals_conceded
            FROM team_match_history
            WHERE team_id = ? COLLATE NOCASE
            ORDER BY match_date DESC LIMIT 10
        ''', (team_name,))
        history_rows = cursor.fetchall()

        if len(history_rows) > 0:
            total_matches = len(history_rows)
            fh_goals = 0
            fh_goal_matches = 0
            for hr in history_rows:
                scored = hr[0] if hr[0] else 0
                conceded = hr[1] if hr[1] else 0
                if (scored + conceded) > 0:
                    fh_goal_matches += 1
                fh_goals += scored

            fh_rate = fh_goal_matches / total_matches
            avg_scored = fh_goals / total_matches

            d[team_type] = {
                "avg_goals_scored": f"{avg_scored:.2f}",
                "fh_goal_rate": f"%{int(fh_rate * 100)}",
                "win_rate": "Taranıyor..."
            }
        else:
            d[team_type] = {
                "avg_goals_scored": "Veri Bekleniyor",
                "fh_goal_rate": "Veri Bekleniyor",
                "win_rate": "Veri Bekleniyor"
            }

    conn.close()

    return {
        "success": True,
        "data": {
            "home_team": home_team,
            "away_team": away_team,
            "home_form": d["home_form"],
            "away_form": d["away_form"],
            "live_stats": {
                "possession": {"home": snap_row[0] or 0, "away": snap_row[1] or 0} if snap_row else {"home":0, "away":0},
                "xg": {"home": snap_row[2] or 0.0, "away": snap_row[3] or 0.0} if snap_row else {"home":0.0, "away":0.0},
                "shots": {"home": snap_row[4] or 0, "away": snap_row[5] or 0} if snap_row else {"home":0, "away":0},
                "shots_target": {"home": snap_row[6] or 0, "away": snap_row[7] or 0} if snap_row else {"home":0, "away":0},
                "corners": {"home": snap_row[8] or 0, "away": snap_row[9] or 0} if snap_row else {"home":0, "away":0}
            }
        }
    }
