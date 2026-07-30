import sqlite3
import os
import csv
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), 'database', 'fh_goal_predictor.db')
ARCHIVE_CSV = os.path.join(os.path.dirname(__file__), 'data', 'iddaa_arsiv_YEDEK.csv')
STATS_DIR = os.path.join(os.path.dirname(__file__), 'data', 'istatistik_sonuclari')

def normalize_name(name):
    if not name: return ""
    return name.lower().strip()

def import_iddaa_archive():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    with open(ARCHIVE_CSV, 'r', encoding='utf-8', errors='ignore') as f:
        reader = csv.DictReader(f)
        count = 0
        for row in reader:
            try:
                mac = row.get('Mac', '')
                if ' vs ' not in mac: continue
                
                home_team = normalize_name(mac.split(' vs ')[0])
                away_team = normalize_name(mac.split(' vs ')[1])
                
                date_str = row.get('Date', '')
                saat_str = row.get('Saat', '')
                # Just use strings for now if parsing fails
                kickoff = f"{date_str} {saat_str}"
                
                fthg = float(row.get('FTHG', 0) or 0)
                ftag = float(row.get('FTAG', 0) or 0)
                hthg = float(row.get('HTHG', 0) or 0)
                htag = float(row.get('HTAG', 0) or 0)
                
                source_id = f"archive_{home_team}_{away_team}_{date_str}_{saat_str}"
                
                # Insert match
                cursor.execute('''
                    INSERT OR IGNORE INTO matches 
                    (source_match_id, home_team_id, away_team_id, kickoff_time, status, home_score, away_score, first_half_home_score, first_half_away_score)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (source_id, home_team, away_team, kickoff, 'FINISHED', int(fthg), int(ftag), int(hthg), int(htag)))
                
                cursor.execute('SELECT id FROM matches WHERE source_match_id = ?', (source_id,))
                res = cursor.fetchone()
                if not res: continue
                match_id = res[0]
                
                # Insert prematch odds
                open_h = float(row.get('Open_H', 0) or 0)
                open_d = float(row.get('Open_D', 0) or 0)
                open_a = float(row.get('Open_A', 0) or 0)
                open_o25 = float(row.get('Open_O25', 0) or 0)
                open_u25 = float(row.get('Open_U25', 0) or 0)
                
                cursor.execute('''
                    INSERT INTO prematch_odds 
                    (match_id, home_win_odds, draw_odds, away_win_odds, over_25_odds, under_25_odds)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (match_id, open_h, open_d, open_a, open_o25, open_u25))
                
                count += 1
                if count % 1000 == 0:
                    print(f"Imported {count} archive rows...")
                    
            except Exception as e:
                # ignore bad rows
                pass
                
    conn.commit()
    conn.close()
    print(f"Finished! Imported total {count} matches from archive.")

def import_team_stats():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    count = 0
    
    if os.path.exists(STATS_DIR):
        for fname in os.listdir(STATS_DIR):
            if fname.endswith('.csv'):
                fpath = os.path.join(STATS_DIR, fname)
                with open(fpath, 'r', encoding='utf-8', errors='ignore') as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        try:
                            team = normalize_name(row.get('Takım', ''))
                            if not team: continue
                            pct_str = row.get('Yüzde', '0').replace('%', '')
                            rate = float(pct_str) / 100.0 if pct_str.isdigit() else 0.0
                            
                            # We can just store this in team_form_features 
                            # Or update it if it exists. We'll use this for the historical form logic
                            cursor.execute('''
                                INSERT INTO team_form_features (team_id, last_10_fh_goal_rate)
                                VALUES (?, ?)
                            ''', (team, rate))
                            count += 1
                        except:
                            pass
    conn.commit()
    conn.close()
    print(f"Finished! Imported {count} team stat records.")

if __name__ == '__main__':
    print("Starting ETL process...")
    import_iddaa_archive()
    import_team_stats()
