import glob

bots = glob.glob("app/bots/bot_*.py")
for b in bots:
    if "bot_1_xg_sniper.py" in b or "bot_momentum" in b or "bot_h2h" in b or "bot_late_drama" in b or "bot_corner_pressure" in b or "bot_red_advantage" in b or "bot_danger_zone" in b or "bot_possession" in b or "bot_shot_accuracy" in b or "bot_favorite_trailing" in b or "bot_first_half" in b or "bot_draw_breaker" in b or "bot_underdog_bite" in b:
        with open(b, "r") as f:
            code = f.read()
            
        old_logic = 'snapshot = c.fetchone()'
        new_logic = '''row = c.fetchone()
                snapshot = dict(row) if row else None'''
                
        if old_logic in code:
            code = code.replace(old_logic, new_logic)
            with open(b, "w") as f:
                f.write(code)

print("Bots updated to use dict(row)!")
