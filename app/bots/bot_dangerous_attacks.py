import sqlite3
from typing import Dict, Any
from app.bots.base_bot import BaseGoalBot
from app.schemas.bot_prediction import BotPrediction

class DangerousAttackBot(BaseGoalBot):
    def __init__(self):
        super().__init__(name="bot_16_dangerous_attacks", version="v1.0.0")

    def predict(self, match_context: Dict[str, Any]) -> BotPrediction:
        match_id = match_context.get("match_id_db")
        minute = match_context.get("current_minute", 0)
        
        latest = match_context.get("latest")
        snap_5 = match_context.get("snap_5")
        snap_10 = match_context.get("snap_10")
        
        if not latest or not snap_5:
            base_prob = 0.30
        else:
            # DA = Dangerous Attacks
            delta_da = (latest.get("home_dangerous_attacks", 0) or 0) - (snap_5.get("home_dangerous_attacks", 0) or 0)
            delta_da += (latest.get("away_dangerous_attacks", 0) or 0) - (snap_5.get("away_dangerous_attacks", 0) or 0)
            
            intensity = 0.0
            
            # 5 dakikada 8+ tehlikeli atak çok ciddi bir baskıdır.
            if delta_da >= 12:
                intensity += 0.40
            elif delta_da >= 8:
                intensity += 0.25
            elif delta_da >= 5:
                intensity += 0.10
                
            if snap_10:
                delta_da_10 = (latest.get("home_dangerous_attacks", 0) or 0) - (snap_10.get("home_dangerous_attacks", 0) or 0)
                delta_da_10 += (latest.get("away_dangerous_attacks", 0) or 0) - (snap_10.get("away_dangerous_attacks", 0) or 0)
                if delta_da_10 >= 20: # 10 dakikada 20 tehlikeli atak
                    intensity += 0.15
                    
            base_prob = 0.35 + intensity
            
        base_prob = min(base_prob, 0.90)
        decision = "goal" if base_prob >= 0.65 else "no_goal"
        
        return BotPrediction(
            match_id=str(match_id),
            bot_name=self.name,
            bot_version=self.version,
            minute=minute,
            period='H2' if minute > 45 else 'H1',
            market='total_goals',
            confidence='high' if base_prob >= 0.75 else ('medium' if base_prob >= 0.65 else 'low'),
            decision=decision,
            probability=base_prob,
            reasons=[f"{self.name} tehlikeli atak momentumunu (DA) analiz etti."],
            data_quality=1.0,
            snapshot_id=latest.get("id") if latest else None
        )
