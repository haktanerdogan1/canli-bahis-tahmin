from app.bots.specialists import TempoScannerBot
import sqlite3

bot = TempoScannerBot()

for m_id, name in [(57891, "Goteborg")]:
    conn = sqlite3.connect('database/fh_goal_predictor.db')
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT minute, home_score, away_score FROM matches WHERE id=?", (m_id,))
    match = cursor.fetchone()
    cursor.execute("SELECT * FROM live_snapshots WHERE match_id = ? ORDER BY id DESC LIMIT 1", (m_id,))
    latest = cursor.fetchone()
    conn.close()

    ctx = {
        "match_id_db": m_id,
        "current_minute": match["minute"],
        "home_score": match["home_score"],
        "away_score": match["away_score"],
        "latest": latest,
    }
    pred = bot.predict(ctx)
    print(f"{name} ({match['minute']}'): Decision={pred.decision}, Prob={pred.probability}, Reason={pred.reasons}, Error={pred.warnings}")
