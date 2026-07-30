import glob

bots = glob.glob("app/bots/bot_*.py")
for b in bots:
    if "bot_1_xg_sniper.py" in b or "bot_momentum" in b or "bot_h2h" in b or "bot_late_drama" in b or "bot_corner_pressure" in b or "bot_red_advantage" in b or "bot_danger_zone" in b or "bot_possession" in b or "bot_shot_accuracy" in b or "bot_favorite_trailing" in b or "bot_first_half" in b or "bot_draw_breaker" in b or "bot_underdog_bite" in b:
        with open(b, "r") as f:
            code = f.read()
        
        # Replace the snapshot fetching logic
        old_logic = 'snapshot = match_context.get("latest_snapshot")'
        new_logic = '''db_path = match_context.get("db_path")
        snapshot = None
        if db_path:
            try:
                conn = sqlite3.connect(db_path)
                conn.row_factory = sqlite3.Row
                c = conn.cursor()
                c.execute("SELECT * FROM live_snapshots WHERE match_id = ? ORDER BY id DESC LIMIT 1", (match_id,))
                snapshot = c.fetchone()
                conn.close()
            except:
                pass'''
                
        if old_logic in code:
            code = code.replace(old_logic, new_logic)
            with open(b, "w") as f:
                f.write(code)

print("Bots updated to fetch snapshots!")
