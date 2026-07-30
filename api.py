from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import sqlite3
import os

app = FastAPI(title="Canlı Gol Olasılığı API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_PATH = os.path.join(os.path.dirname(__file__), 'database', 'fh_goal_predictor.db')

@app.get("/")
def serve_index():
    from fastapi.responses import FileResponse
    return FileResponse(os.path.join(os.path.dirname(__file__), 'index.html'))

@app.get("/api/health")
def health_check():
    return {"status": "ok"}

@app.get("/api/live-matches")
def get_live_matches():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT m.home_team_id, m.away_team_id, m.home_score, m.away_score, m.minute,
               p.signal_level, p.weighted_probability, p.decision, m.league_name, m.league_logo, m.id,
               m.home_team_logo, m.away_team_logo, s.home_score, s.away_score, m.status, s.minute
        FROM matches m
        JOIN consensus_predictions p ON m.id = p.match_id
        LEFT JOIN live_snapshots s ON p.snapshot_id = s.id
        ORDER BY p.created_at DESC
        LIMIT 1000
    ''')
    rows = cursor.fetchall()
    conn.close()
    
    results = []
    for r in rows:
        minute = r[4]
        current_home = r[2]
        current_away = r[3]
        initial_home = r[13] if r[13] is not None else current_home
        initial_away = r[14] if r[14] is not None else current_away
        match_status = r[15]
        
        total_goals_now = current_home + current_away
        total_goals_initial = initial_home + initial_away
        
        signal_minute = r[16] if (len(r) > 16 and r[16] is not None) else minute
        
        outcome = "PENDING"
        if total_goals_now > total_goals_initial:
            outcome = "WON"
        elif signal_minute <= 45 and (match_status in ('Ended', 'FT', 'FINISHED', 'HT', 'Halftime', 'Canceled') or minute > 45):
            outcome = "LOST"
        elif signal_minute > 45 and (match_status in ('Ended', 'FT', 'FINISHED', 'Canceled') or minute >= 95):
            outcome = "LOST"
            
        market = f"İlk Yarı {total_goals_initial + 0.5} Üst" if signal_minute <= 45 else f"Maç Sonu {total_goals_initial + 0.5} Üst"
        
        results.append({
            "home_team": r[0],
            "away_team": r[1],
            "home_score": current_home,
            "away_score": current_away,
            "minute": minute,
            "market": market,
            "probability": round(r[6], 3),
            "confidence": f"Seviye: {r[5]}", 
            "league_name": r[8] if r[8] else "Canlı Alarmlar",
            "league_logo": r[9] if r[9] else "",
            "match_id": r[10],
            "home_logo": r[11] if r[11] else f"https://ui-avatars.com/api/?name={r[0].replace(' ', '+')}&background=1f2937&color=00e5ff",
            "away_logo": r[12] if r[12] else f"https://ui-avatars.com/api/?name={r[1].replace(' ', '+')}&background=1f2937&color=00e5ff",
            "outcome": outcome
        })
        
    return {"success": True, "data": results}

@app.get("/api/results")
def get_results():
    conn = sqlite3.connect(DB_PATH)
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
    conn = sqlite3.connect(DB_PATH)
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
def get_match_detail(match_id: int):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT home_team_id, away_team_id
        FROM matches WHERE id = ?
    ''', (match_id,))
    row = cursor.fetchone()
    if not row:
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
