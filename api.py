from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import sqlite3
import os

import auth
import settlement
import odds as odds_mod
import prematch

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

SESSION_COOKIE = "jcode_session"


def current_user_id(request: Request):
    """Istekteki oturum cookie'sinden kullanici id'si cikarir; yoksa None."""
    return auth.verify_session_token(request.cookies.get(SESSION_COOKIE, ""))

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

@app.get("/api/health")
def health_check():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Kimlik dogrulama uclari
# ---------------------------------------------------------------------------

class Credentials(BaseModel):
    email: str
    password: str


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
    user_id, error = auth.create_user(creds.email, creds.password)
    if error:
        return {"success": False, "error": error}
    _set_session_cookie(response, user_id, request)
    return {"success": True, "email": auth.normalize_email(creds.email)}


@app.post("/api/login")
def login(creds: Credentials, request: Request, response: Response):
    user_id, error = auth.authenticate(creds.email, creds.password)
    if error:
        return {"success": False, "error": error}
    _set_session_cookie(response, user_id, request)
    return {"success": True, "email": auth.normalize_email(creds.email)}


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

@app.get("/api/live-matches")
def get_live_matches(request: Request):
    is_member = current_user_id(request) is not None
    conn = connect()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT m.home_team_id, m.away_team_id, m.home_score, m.away_score, m.minute,
               p.signal_level, p.weighted_probability, p.decision, m.league_name, m.league_logo, m.id,
               m.home_team_logo, m.away_team_logo, s.home_score, s.away_score, m.status, s.minute,
               fh.fh_end_home, fh.fh_end_away, p.created_at, p.signal_minute, p.market, p.outcome
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
        ORDER BY p.created_at DESC
        LIMIT 5000
    ''')
    rows = cursor.fetchall()
    conn.close()
    
    # Bir maç icin orkestrator zaman icinde birden fazla sinyal (consensus_predictions satiri)
    # uretebilir (goller ilerledikce esik yukselerek yeni sinyal acilir). Ekranda her mac TEK
    # satir olarak gorunmeli: herhangi bir sinyali kazandiysa mac KAZANDI, hicbiri kazanmadan
    # hepsi sonuclandiysa KAYBETTI, hala acik/bekleyen bir sinyali varsa BEKLEMEDE sayilir.
    by_match = {}
    
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
            "created_at": r[19] if len(r) > 19 else None
        }
        
        # rows zaten p.created_at DESC sirali geldigi icin, bir match_id icin ilk gordugumuz
        # kayit o kategori (won/pending/lost) icin en guncel olandir.
        bucket = by_match.setdefault(match_id, {"won": None, "pending": None, "lost": None, "void": None})
        if entry["outcome"] == "WON" and bucket["won"] is None:
            bucket["won"] = entry
        elif entry["outcome"] == "PENDING" and bucket["pending"] is None:
            bucket["pending"] = entry
        elif entry["outcome"] == "LOST" and bucket["lost"] is None:
            bucket["lost"] = entry
        elif entry["outcome"] == "VOID" and bucket["void"] is None:
            bucket["void"] = entry
    
    results = []
    for match_id, bucket in by_match.items():
        if bucket["won"] is not None:
            chosen = bucket["won"]
        elif bucket["pending"] is not None:
            chosen = bucket["pending"]
        elif bucket["lost"] is not None:
            chosen = bucket["lost"]
        else:
            chosen = bucket["void"]
        chosen.pop("created_at", None)

        if not is_member:
            # UYE DEGILSE tahmin verisi hic gonderilmez. Sadece ekrani CSS ile
            # bulaniklastirmak koruma SAGLAMAZ (kullanici sayfa kaynagina veya
            # gelistirici konsoluna bakip veriyi okur). Bu yuzden hassas alanlar
            # sunucuda siliniyor; on yuzdeki bulanik gorunum yalnizca dekoratif.
            chosen["market"] = None
            chosen["probability"] = None
            chosen["confidence"] = None
            chosen["signal_minute"] = None
            chosen["locked"] = True

        results.append(chosen)

    return {"success": True, "data": results, "is_member": is_member}

@app.get("/api/ozet")
def get_ozet():
    """Herkese acik sonuc ozeti - seffaflik kozu, UYELIK GEREKTIRMEZ.

    /api/metrics'teki 'ozet' bloguyla ayni hesap; farki uyelik sarti olmamasi.
    Bot bazli detay ve kalibrasyon gibi rekabete hassas veriler burada YOK,
    onlar /api/metrics'te uyelere ozel kalmaya devam ediyor.
    """
    conn = connect()
    cur = conn.cursor()

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

    # Bugun (Turkiye saatiyle, UTC+3 - Turkiye yaz saati uygulamiyor) uretilmis
    # ve sonuclanmis sinyaller. created_at SQLite CURRENT_TIMESTAMP oldugu icin UTC'dir.
    cur.execute("""
        SELECT outcome, COUNT(*) FROM consensus_predictions
        WHERE decision='signal' AND outcome IN ('WON','LOST')
          AND date(created_at, '+3 hours') = date('now', '+3 hours')
        GROUP BY outcome
    """)
    bugun = dict(cur.fetchall())
    conn.close()

    return {
        "success": True,
        "sonuclanan": settled,
        "kazanan": won,
        "kaybeden": lost,
        "isabet_orani": round(won / settled, 3) if settled else None,
        "bekleyen": pending,
        "gozlem_disi": void,
        "bugun_kazanan": bugun.get("WON", 0),
        "bugun_kaybeden": bugun.get("LOST", 0),
    }


@app.get("/api/metrics")
def get_metrics(request: Request):
    """Bot bazli isabet orani + konsensus kalibrasyonu.

    "Piyasanin en iyisi" iddiasi ancak olculebilirse anlamlidir. Bu uc, sonuclanmis
    (outcome dolu) sinyaller uzerinden calisir - anlik tahmin degil, gecmis gercek.

    Uyelere ozel: rakiplere veya rastgele ziyaretcilere sistemin ic performansini acmayiz.
    """
    if current_user_id(request) is None:
        return {"success": False, "locked": True, "error": "Bu sayfa üyelere özel."}

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
        "uyari": note,
    }

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

@app.get("/api/results")
def get_results():
    conn = connect()
    cursor = conn.cursor()
    # fix the error: use created_at instead of updated_at since it wasn't defined
    cursor.execute('''
        SELECT m.home_team_id, m.away_team_id, m.home_score, m.away_score, m.minute,
               p.market_name, p.probability, p.confidence_level, p.prediction_status, m.league_name, m.league_logo
        FROM matches m
        JOIN model_predictions p ON m.id = p.match_id
        WHERE p.prediction_status IN ('WON', 'LOST')
        ORDER BY m.league_name ASC, p.created_at DESC
        LIMIT 50
    ''')
    rows = cursor.fetchall()
    conn.close()
    
    results = []
    for r in rows:
        results.append({
            "home_team": r[0],
            "away_team": r[1],
            "home_score": r[2],
            "away_score": r[3],
            "minute": r[4],
            "market": r[5],
            "probability": round(r[6], 3),
            "confidence": r[7],
            "status": r[8],
            "league_name": r[9] if r[9] else "Geçmiş Alarmlar",
            "league_logo": r[10] if r[10] else ""
        })
        
    return {"success": True, "data": results}

@app.get("/api/all-live")
def get_all_live():
    conn = connect()
    cursor = conn.cursor()
    # status in matches table indicates '1st half', '2nd half', 'Halftime' etc.
    cursor.execute('''
        SELECT id, home_team_id, away_team_id, home_score, away_score, minute, status, league_name, league_logo, home_team_logo, away_team_logo
        FROM matches
        WHERE status NOT IN ('Ended', 'FT', 'Canceled', 'FINISHED')
        ORDER BY league_name ASC, minute DESC
    ''')
    rows = cursor.fetchall()
    conn.close()
    
    results = []
    for r in rows:
        results.append({
            "match_id": r[0],
            "home_team": r[1],
            "away_team": r[2],
            "home_score": r[3],
            "away_score": r[4],
            "minute": r[5],
            "status": r[6],
            "league_name": r[7] if r[7] else "Diğer Ligler",
            "league_logo": r[8] if r[8] else "",
            "home_logo": r[9] if r[9] else f"https://ui-avatars.com/api/?name={r[1].replace(' ', '+')}&background=1f2937&color=00e5ff",
            "away_logo": r[10] if r[10] else f"https://ui-avatars.com/api/?name={r[2].replace(' ', '+')}&background=1f2937&color=00e5ff",
            "market": "Beklemede",
            "probability": 0.0,
            "confidence": "Analiz Ediliyor"
        })
        
    return {"success": True, "data": results}

@app.get("/api/match/{match_id}")
def get_match_detail(match_id: int, request: Request):
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
