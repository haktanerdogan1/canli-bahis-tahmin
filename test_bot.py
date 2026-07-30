from app.bots.live_tempo_bot import LiveTempoBot
import sqlite3

bot = LiveTempoBot()

for m_id, name in [(57891, "Goteborg")]:
    conn = sqlite3.connect('database/fh_goal_predictor.db')
    cursor = conn.cursor()
    cursor.execute("SELECT minute FROM matches WHERE id=?", (m_id,))
    minute = cursor.fetchone()[0]
    conn.close()
    
    ctx = {
        "match_id_db": m_id,
        "current_minute": minute,
        "db_path": 'database/fh_goal_predictor.db'
    }
    pred = bot.predict(ctx)
    print(f"{name} ({minute}'): Decision={pred.decision}, Prob={pred.probability}, Reason={pred.reasons}, Error={pred.warnings}")
