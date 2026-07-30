import asyncio
import time
import sqlite3
import os
import pandas as pd
from collections import deque
from sporkolik_scraper import SporkolikScraper, Match

DB_PATH = os.path.join(os.path.dirname(__file__), 'database', 'fh_goal_predictor.db')

SPORKOLIK_HISTORY = {}

def update_and_get_momentum(event_id, minute, shots, corners):
    current_ts = int(time.time())
    if event_id not in SPORKOLIK_HISTORY:
        SPORKOLIK_HISTORY[event_id] = deque(maxlen=20) 
        
    history = SPORKOLIK_HISTORY[event_id]
    history.append((minute, shots, corners, current_ts))
    
    target_minute = minute - 5
    best_snapshot = None
    min_diff = 999
    
    for snap in history:
        snap_min = snap[0]
        diff = abs(snap_min - target_minute)
        if (minute - snap_min) >= 3 and (minute - snap_min) <= 8:
            if diff < min_diff:
                min_diff = diff
                best_snapshot = snap
                
    if best_snapshot:
        past_min, past_shots, past_corners, _ = best_snapshot
        delta_shots = max(0, shots - past_shots)
        delta_corners = max(0, corners - past_corners)
        return delta_shots, delta_corners, (minute - past_min)
        
    return 0, 0, 0

def parse_minute(min_str, status_desc):
    if not min_str:
        return 0
    min_str = min_str.replace("'", "").strip()
    if min_str.isdigit():
        return int(min_str)
    if "+" in min_str:
        parts = min_str.split("+")
        if parts[0].isdigit():
            return int(parts[0])
    if status_desc == "Halftime" or status_desc == "İY":
        return 45
    return 0

async def process_sporkolik_matches(matches: list[Match]):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    for match in matches:
        event_id = "sporkolik_" + str(match.match_id)
        home = match.home
        away = match.away
        score_h = match.home_score or 0
        score_a = match.away_score or 0
        total_goals = score_h + score_a
        
        status_desc = match.status_description or ""
        minute = parse_minute(match.minute, status_desc)
        
        if status_desc not in ["1st half", "2nd half", "Halftime", "İY", "1. Yarı", "2. Yarı"] and minute == 0:
            continue
            
        if "1st" in status_desc or "1." in status_desc or "İY" in status_desc:
            target_market = f"İlk Yarı {total_goals + 0.5} Üst"
        else:
            target_market = f"Maç Sonu {total_goals + 0.5} Üst"
            
        cursor.execute('''
            INSERT OR IGNORE INTO matches 
            (source_match_id, home_team_id, away_team_id, status)
            VALUES (?, ?, ?, ?)
        ''', (event_id, home, away, 'LIVE'))
        
        cursor.execute('UPDATE matches SET home_score=?, away_score=?, minute=?, status=? WHERE source_match_id=?', 
                       (score_h, score_a, minute, status_desc, event_id))
                       
        cursor.execute('SELECT id FROM matches WHERE source_match_id = ?', (event_id,))
        match_id_res = cursor.fetchone()
        if not match_id_res:
            continue
        match_id = match_id_res[0]
        
        cursor.execute("SELECT id, market_name, prediction_status FROM model_predictions WHERE match_id = ? ORDER BY created_at DESC LIMIT 1", (match_id,))
        pred_row = cursor.fetchone()
        
        shots_on_target = 0
        corners = 0
        
        stats = match.all_stats
        if stats:
            s_item = stats.get("Shots on target") or stats.get("totalShotsOnGoal")
            c_item = stats.get("Corner kicks") or stats.get("cornerKicks")
            
            if s_item:
                try: shots_on_target = int(s_item.home_value or s_item.home) + int(s_item.away_value or s_item.away)
                except: pass
            if c_item:
                try: corners = int(c_item.home_value or c_item.home) + int(c_item.away_value or c_item.away)
                except: pass
                
        delta_shots, delta_corners, mins_passed = update_and_get_momentum(
            event_id, minute, shots_on_target, corners
        )
        
        live_xg = (shots_on_target * 0.11) + (corners * 0.03)
        
        if pred_row:
            pred_id, existing_market, p_status = pred_row
            if p_status == 'PENDING':
                try: target_g = float(existing_market.split()[-2])
                except: target_g = 99.0
                    
                if total_goals >= target_g:
                    cursor.execute("UPDATE model_predictions SET prediction_status='WON', confidence_level=? WHERE id=?", 
                                   (f'✅ KAZANDI! (Skor: {score_h}-{score_a})', pred_id))
                else:
                    if minute >= 85:
                        cursor.execute("UPDATE model_predictions SET prediction_status='LOST', confidence_level=? WHERE id=?", 
                                       (f'❌ SÜRE DOLDU (85+)', pred_id))
                    else:
                        conf_text = f'Son {mins_passed}dk: Şut+{delta_shots}, Korner+{delta_corners} (Sporkolik)'
                        cursor.execute("UPDATE model_predictions SET confidence_level=? WHERE id=?", (conf_text, pred_id))
        else:
            if minute >= 80 or minute < 5:
                continue
            if "1" in status_desc and minute >= 40:
                continue
                
            if mins_passed >= 3:
                if delta_shots < 2 and delta_corners < 2:
                    continue 
            else:
                continue
                
            final_prob = min(0.95, 0.5 + (live_xg * 0.10))
            
            if final_prob >= 0.70:
                conf_text = f'Son {mins_passed}dk: Şut+{delta_shots}, Korner+{delta_corners} 🔥 (Sporkolik Radar)'
                cursor.execute('''
                    INSERT INTO model_predictions (match_id, model_version, market_name, probability, confidence_level, prediction_status)
                    VALUES (?, ?, ?, ?, ?, 'PENDING')
                ''', (match_id, 'V3_Sporkolik', target_market, final_prob, conf_text))
                print(f"🔥 SPORKOLIK ALARM! {home} vs {away} - {target_market} - Son {mins_passed}Dk: Şut+{delta_shots}, Korner+{delta_corners}")

    conn.commit()
    conn.close()

async def main():
    print("🚀 Starting SPORKOLIK Radar Bot (Swarm Agent 2)...", flush=True)
    async with SporkolikScraper(concurrency=6) as scraper:
        while True:
            start_time = time.time()
            print("📡 Fetching Sporkolik live events...", flush=True)
            try:
                matches = await scraper.scrape_live(with_stats=True)
                await process_sporkolik_matches(matches)
            except Exception as e:
                print(f"❌ Error during Sporkolik loop: {e}", flush=True)
                
            elapsed = time.time() - start_time
            print(f"✅ Sporkolik cycle completed in {elapsed:.2f} seconds. Waiting 15s...", flush=True)
            await asyncio.sleep(15)

if __name__ == '__main__':
    asyncio.run(main())
