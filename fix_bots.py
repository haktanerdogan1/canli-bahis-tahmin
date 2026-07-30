import os
import glob

bot_files = glob.glob("app/bots/bot_*.py")
for bf in bot_files:
    if bf in ["app/bots/bot_xg_sniper.py", "app/bots/bot_momentum.py", "app/bots/bot_h2h.py", "app/bots/bot_late_drama.py", "app/bots/bot_corner_pressure.py", "app/bots/bot_red_advantage.py", "app/bots/bot_danger_zone.py", "app/bots/bot_possession.py", "app/bots/bot_shot_accuracy.py", "app/bots/bot_favorite_trailing.py", "app/bots/bot_first_half.py", "app/bots/bot_draw_breaker.py", "app/bots/bot_underdog_bite.py"]:
        with open(bf, "r") as f:
            content = f.read()
        
        content = content.replace("version=self.version,", "bot_version=self.version,\n            minute=minute,\n            period='H2' if minute > 45 else 'H1',\n            market='total_goals',\n            confidence='high' if base_prob >= 0.70 else ('medium' if base_prob >= 0.60 else 'low'),")
        
        with open(bf, "w") as f:
            f.write(content)

print("Bots fixed!")
