import sqlite3
import os
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), 'database', 'fh_goal_predictor.db')
REPORT_PATH = os.path.join(os.path.dirname(__file__), 'gece_raporu.md')

def run_night_ops():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # 1. Update Team Form Features based on Finished Matches
    cursor.execute('''
        SELECT home_team_id, away_team_id, home_score, away_score 
        FROM matches 
        WHERE status IN ('Ended', 'FT')
    ''')
    finished_matches = cursor.fetchall()
    
    team_stats = {}
    for h_team, a_team, h_score, a_score in finished_matches:
        if h_team not in team_stats: team_stats[h_team] = {'matches': 0, 'goals': 0, 'wins': 0}
        if a_team not in team_stats: team_stats[a_team] = {'matches': 0, 'goals': 0, 'wins': 0}
        
        team_stats[h_team]['matches'] += 1
        team_stats[h_team]['goals'] += h_score
        if h_score > a_score: team_stats[h_team]['wins'] += 1
        
        team_stats[a_team]['matches'] += 1
        team_stats[a_team]['goals'] += a_score
        if a_score > h_score: team_stats[a_team]['wins'] += 1
        
    for team, stats in team_stats.items():
        if stats['matches'] > 0:
            avg_goals = stats['goals'] / stats['matches']
            win_rate = stats['wins'] / stats['matches']
            cursor.execute('''
                UPDATE team_form_features 
                SET last_10_fh_scored_rate = ?, 
                    last_10_fh_goal_rate = ? 
                WHERE team_id = ?
            ''', (avg_goals, win_rate, team.lower().strip()))
            
    conn.commit()

    # 2. Detailed Performance Analysis
    cursor.execute('''
        SELECT p.prediction_status, p.confidence_level
        FROM model_predictions p
        JOIN matches m ON p.match_id = m.id
        WHERE p.prediction_status IN ('WON', 'LOST')
    ''')
    preds = cursor.fetchall()
    conn.close()
    
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    if not preds:
        report_md = f"# 🌙 Gece Vardiyası Raporu\n\n*Son Güncelleme: {now_str}*\n\nHenüz sonuçlanan bir tahmin bulunmuyor. Sistem aktif olarak maçları taramaya devam ediyor."
    else:
        total_preds = len(preds)
        won_preds = sum(1 for p in preds if p[0] == 'WON')
        lost_preds = sum(1 for p in preds if p[0] == 'LOST')
        win_rate = (won_preds / total_preds) * 100 if total_preds > 0 else 0
        
        def parse_xg(conf):
            try:
                if 'xG:' in conf:
                    return float(conf.split('|')[0].replace('xG:', '').strip())
            except:
                pass
            return 0.0
            
        xg_wons = [parse_xg(p[1]) for p in preds if p[0] == 'WON']
        xg_losts = [parse_xg(p[1]) for p in preds if p[0] == 'LOST']
        
        avg_xg_won = sum(xg_wons)/len(xg_wons) if xg_wons else 0.0
        avg_xg_lost = sum(xg_losts)/len(xg_losts) if xg_losts else 0.0
        
        report_md = f"""# 🌙 Gece Vardiyası Performans Raporu

*Son Güncelleme: {now_str}*

**Özet İstatistikler:**
- **Toplam Alarm (Sinyal):** {total_preds}
- **✅ Kazanan Sinyaller:** {won_preds}
- **❌ Kaybeden Sinyaller:** {lost_preds}
- **🏆 Başarı Oranı:** %{win_rate:.1f}

**xG (Gol Beklentisi) Analizi:**
- Kazanan maçların ortalama yakalanan xG'si: **{avg_xg_won:.2f}**
- Kaybeden maçların ortalama yakalanan xG'si: **{avg_xg_lost:.2f}**

> [!TIP]
> xG (Gol Beklentisi) oranlarına bakarak sistemin hangi risk seviyesinde daha başarılı olduğunu görebiliriz. Eğer kaybeden maçların xG'si çok yüksekse şanssızlık faktörü devreye girmiş demektir.
"""
        
        print("--- GECE VARDİYASI PERFORMANS RAPORU ---")
        print(f"Toplam Alarm: {total_preds}")
        print(f"Kazanan: {won_preds}")
        print(f"Kaybeden: {lost_preds}")
        print(f"Başarı Oranı: %{win_rate:.1f}")
        print(f"Kazanan Ort xG: {avg_xg_won:.2f}")
        print(f"Kaybeden Ort xG: {avg_xg_lost:.2f}")
        print("------------------------------------------")

    with open(REPORT_PATH, 'w', encoding='utf-8') as f:
        f.write(report_md)

if __name__ == '__main__':
    run_night_ops()
