import asyncio
import aiohttp
import time
import sqlite3
import os
import pickle
import pandas as pd
from collections import deque

DB_PATH = os.path.join(os.path.dirname(__file__), 'database', 'fh_goal_predictor.db')
MODEL_PATH = os.path.join(os.path.dirname(__file__), 'models', 'lr_baseline_v1.pkl')

# Memory for tracking last N minutes of stats
MATCH_HISTORY = {}

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)',
    'Accept': '*/*',
    'Origin': 'https://www.sofascore.com',
    'Referer': 'https://www.sofascore.com/'
}

def load_model():
    if os.path.exists(MODEL_PATH):
        with open(MODEL_PATH, 'rb') as f:
            return pickle.load(f)
    return None

async def fetch_json(session, url):
    try:
        async with session.get(url, headers=HEADERS, timeout=5) as response:
            if response.status == 200:
                return await response.json()
    except Exception as e:
        pass
    return None

def update_and_get_momentum(event_id, minute, shots, corners, da):
    current_ts = int(time.time())
    if event_id not in MATCH_HISTORY:
        # Store tuples of (minute, shots, corners, dangerous_attacks, timestamp)
        MATCH_HISTORY[event_id] = deque(maxlen=20) 
        
    history = MATCH_HISTORY[event_id]
    history.append((minute, shots, corners, da, current_ts))
    
    # We want the snapshot from ~5 minutes ago (at least 3 mins, max 8 mins)
    target_minute = minute - 5
    best_snapshot = None
    min_diff = 999
    
    for snap in history:
        snap_min = snap[0]
        diff = abs(snap_min - target_minute)
        # Accept if it's within [minute-8, minute-3] range
        if (minute - snap_min) >= 3 and (minute - snap_min) <= 8:
            if diff < min_diff:
                min_diff = diff
                best_snapshot = snap
                
    if best_snapshot:
        past_min, past_shots, past_corners, past_da, _ = best_snapshot
        delta_shots = max(0, shots - past_shots)
        delta_corners = max(0, corners - past_corners)
        delta_da = max(0, da - past_da)
        return delta_shots, delta_corners, delta_da, (minute - past_min)
        
    return 0, 0, 0, 0


async def process_ghost_sweeper(session, conn, live_event_ids):
    cursor = conn.cursor()
    # 1. Sweeper for Predictions
    cursor.execute('''
        SELECT p.id, m.source_match_id, p.market_name 
        FROM model_predictions p 
        JOIN matches m ON p.match_id = m.id 
        WHERE p.prediction_status = 'PENDING'
    ''')
    pending_preds = cursor.fetchall()
    
    missing_pred_tasks = []
    for p_id, src_id, m_name in pending_preds:
        if src_id not in live_event_ids:
            missing_pred_tasks.append((p_id, src_id, m_name, fetch_json(session, f'https://api.sofascore.com/api/v1/event/{src_id}')))
            
    # Execute missing predictions detail fetch
    for p_id, src_id, m_name, task in missing_pred_tasks:
        res = await task
        if res and 'event' in res:
            final_event = res['event']
            sc_h = final_event.get('homeScore', {}).get('current', 0)
            sc_a = final_event.get('awayScore', {}).get('current', 0)
            tot = sc_h + sc_a
            try:
                tgt = float(m_name.split()[-2])
            except:
                tgt = 99.0
            
            if tot >= tgt:
                cursor.execute("UPDATE model_predictions SET prediction_status='WON', confidence_level=? WHERE id=?", 
                               (f'✅ KAZANDI! (Skor: {sc_h}-{sc_a})', p_id))
            else:
                cursor.execute("UPDATE model_predictions SET prediction_status='LOST', confidence_level=? WHERE id=?", 
                               (f'❌ MAÇ BİTTİ (Skor: {sc_h}-{sc_a})', p_id))
                               
    # 2. Sweeper for matches
    cursor.execute('''
        SELECT source_match_id FROM matches 
        WHERE status NOT IN ('Ended', 'FT', 'Canceled', 'FINISHED')
    ''')
    all_active = cursor.fetchall()
    
    missing_match_tasks = []
    for row in all_active:
        src_id = row[0]
        if src_id and src_id not in live_event_ids:
            missing_match_tasks.append((src_id, fetch_json(session, f'https://api.sofascore.com/api/v1/event/{src_id}')))
            
    for src_id, task in missing_match_tasks:
        res = await task
        if res and 'event' in res:
            final_event = res['event']
            sc_h = final_event.get('homeScore', {}).get('current', 0)
            sc_a = final_event.get('awayScore', {}).get('current', 0)
            cursor.execute("UPDATE matches SET status='Ended', home_score=?, away_score=? WHERE source_match_id=?", 
                           (sc_h, sc_a, src_id))
        else:
            cursor.execute("UPDATE matches SET status='Ended' WHERE source_match_id=?", (src_id,))

    conn.commit()


async def process_live_events(session, model, events):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    live_events = [e for e in events if e.get('status', {}).get('type') in ['inprogress', 'finished', 'canceled']]
    live_event_ids = [str(e['id']) for e in live_events]
    current_ts = int(time.time())
    
    # Run ghost sweeper first
    await process_ghost_sweeper(session, conn, live_event_ids)
    
    # Prepare fetching stats for up to 40 events concurrently
    target_events = live_events[:40]
    
    # We will fetch all stats simultaneously
    stat_tasks = [fetch_json(session, f'https://api.sofascore.com/api/v1/event/{e["id"]}/statistics') for e in target_events]
    all_stats_results = await asyncio.gather(*stat_tasks)
    
    for idx, event in enumerate(target_events):
        event_id = str(event['id'])
        home = event['homeTeam']['name']
        away = event['awayTeam']['name']
        
        score_h = event.get('homeScore', {}).get('current', 0)
        score_a = event.get('awayScore', {}).get('current', 0)
        total_goals = score_h + score_a
        
        time_info = event.get('time', {})
        start_ts = time_info.get('currentPeriodStartTimestamp')
        status_desc = event.get('status', {}).get('description', '')
        
        if status_desc == '1st half':
            target_market = f"İlk Yarı {total_goals + 0.5} Üst"
        else:
            target_market = f"Maç Sonu {total_goals + 0.5} Üst"
        
        minute = 0
        if start_ts:
            played_min = (current_ts - start_ts) // 60
            if status_desc == '2nd half':
                minute = 45 + played_min
            elif status_desc == '1st half':
                minute = played_min
            elif status_desc == 'Extra time':
                minute = 90 + played_min
            else:
                minute = played_min
                
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
        
        stats_data = all_stats_results[idx]
        shots_on_target = 0
        corners = 0
        dangerous_attacks = 0
        
        if stats_data:
            statistics = stats_data.get('statistics', [])
            period_all = next((p for p in statistics if p.get('period') == 'ALL'), None)
            if period_all:
                for group in period_all.get('groups', []):
                    for item in group.get('statisticsItems', []):
                        name = item.get('name')
                        try:
                            h_val = int(item.get('home', 0))
                            a_val = int(item.get('away', 0))
                            if name == "Shots on target": shots_on_target = h_val + a_val
                            elif name == "Corner kicks": corners = h_val + a_val
                            elif name == "Dangerous attacks": dangerous_attacks = h_val + a_val
                        except: pass
                        
        delta_shots, delta_corners, delta_da, mins_passed = update_and_get_momentum(
            event_id, minute, shots_on_target, corners, dangerous_attacks
        )
        
        live_xg = (shots_on_target * 0.11) + (corners * 0.03) + (dangerous_attacks * 0.01)
        
        if pred_row:
            pred_id, existing_market, p_status = pred_row
            if p_status == 'PENDING':
                try:
                    target_g = float(existing_market.split()[-2])
                except:
                    target_g = 99.0
                    
                if total_goals >= target_g:
                    cursor.execute("UPDATE model_predictions SET prediction_status='WON', confidence_level=? WHERE id=?", 
                                   (f'✅ KAZANDI! (Skor: {score_h}-{score_a})', pred_id))
                else:
                    is_ilk_yari = "İlk Yarı" in existing_market
                    if is_ilk_yari and status_desc in ['Halftime', '2nd half', 'Ended', 'FT']:
                        cursor.execute("UPDATE model_predictions SET prediction_status='LOST', confidence_level=? WHERE id=?", 
                                       (f'❌ İLK YARI GOL OLMADI (Skor: {score_h}-{score_a})', pred_id))
                    elif status_desc in ['Ended', 'FT']:
                        cursor.execute("UPDATE model_predictions SET prediction_status='LOST', confidence_level=? WHERE id=?", 
                                       (f'❌ MAÇ BİTTİ (Skor: {score_h}-{score_a})', pred_id))
                    elif minute >= 85:
                        cursor.execute("UPDATE model_predictions SET prediction_status='LOST', confidence_level=? WHERE id=?", 
                                       (f'❌ SÜRE DOLDU (85+)', pred_id))
                    else:
                        risk_str = " ⚠️ [RİSKLİ]" if (40 <= minute <= 45) else ""
                        conf_text = f'xG: {live_xg:.2f} | İ.Şut: {shots_on_target} | Korner: {corners}{risk_str}'
                        cursor.execute("UPDATE model_predictions SET confidence_level=? WHERE id=?", (conf_text, pred_id))
        
        else:
            # NO PREDICTION YET - Check if we should predict (V2 Momentum Rules)
            if minute >= 80: # V2 RULE: No predictions after 80 mins
                continue
            if status_desc == '1st half' and minute >= 40: # V2 RULE: No FH predictions after 40 mins
                continue
            if event.get('status', {}).get('type') != 'inprogress':
                continue
                
            # V3 RULE: MUST HAVE 5-MIN MOMENTUM
            if mins_passed >= 3:
                # We have a valid history window
                if delta_shots < 2 and delta_corners < 2 and delta_da < 10:
                    continue # Not enough momentum in the last ~5 mins
            else:
                # Not enough history collected yet (wait a few cycles)
                continue
                
            cursor.execute('SELECT last_10_fh_goal_rate FROM team_form_features WHERE team_id = ? OR team_id = ?', 
                           (home.lower().strip(), away.lower().strip()))
            form_row = cursor.fetchone()
            historical_form = form_row[0] if form_row else 0.50
            
            home_win_odds = 2.10
            over_25_odds = 1.85
            
            X_live = pd.DataFrame([{
                'prob_home': 1 / home_win_odds,
                'prob_over25': 1 / over_25_odds,
                'home_win_odds': home_win_odds,
                'over_25_odds': over_25_odds
            }])
            base_prob = model.predict_proba(X_live)[0][1] if model else 0.5
            
            live_adjustment = (live_xg * 0.10) + ((historical_form - 0.5) * 0.15)
            final_prob = min(0.95, base_prob + live_adjustment)
            
            # V2 RULE: PROBABILITY THRESHOLD RAISED TO 80%
            if final_prob >= 0.80:
                conf_text = f'Son {mins_passed}dk: Şut+{delta_shots}, Korner+{delta_corners}, Atak+{delta_da} 🔥 (V3 Radar)'
                cursor.execute('''
                    INSERT INTO model_predictions (match_id, model_version, market_name, probability, confidence_level, prediction_status)
                    VALUES (?, ?, ?, ?, ?, 'PENDING')
                ''', (match_id, 'V3_Momentum', target_market, final_prob, conf_text))
                print(f"🔥 V3 ALARM! {home} vs {away} - {target_market} - Son {mins_passed}Dk: Şut+{delta_shots}, Korner+{delta_corners}, Atak+{delta_da}")

    conn.commit()
    conn.close()

async def main():
    model = load_model()
    print("🚀 Starting ASYNC Live Statistics Bot (V2 Momentum Radar)...", flush=True)
    
    # We use a single ClientSession for connection pooling and speed
    async with aiohttp.ClientSession() as session:
        while True:
            start_time = time.time()
            print("📡 Fetching live events concurrently...", flush=True)
            try:
                res = await fetch_json(session, 'https://api.sofascore.com/api/v1/sport/football/events/live')
                if res and 'events' in res:
                    events = res['events']
                    await process_live_events(session, model, events)
                else:
                    print("⚠️ No events found or failed to parse.", flush=True)
            except Exception as e:
                print(f"❌ Error during main loop: {e}", flush=True)
                
            elapsed = time.time() - start_time
            print(f"✅ Cycle completed in {elapsed:.2f} seconds. Waiting 10s...", flush=True)
            await asyncio.sleep(10)

if __name__ == '__main__':
    asyncio.run(main())
