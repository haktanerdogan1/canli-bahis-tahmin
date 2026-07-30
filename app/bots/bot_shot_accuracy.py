import sqlite3
from typing import Dict, Any
from app.bots.base_bot import BaseGoalBot
from app.schemas.bot_prediction import BotPrediction
import math

class ShotAccuracyBot(BaseGoalBot):
    def __init__(self):
        super().__init__(name="bot_11_shot_accuracy", version="v1.0.0")

    def predict(self, match_context: Dict[str, Any]) -> BotPrediction:
        match_id = match_context.get("match_id_db")
        minute = match_context.get("current_minute", 0)
        
        latest = match_context.get("latest")
        snap_5 = match_context.get("snap_5")
        snap_10 = match_context.get("snap_10")
        snap_15 = match_context.get("snap_15")
        
        if not latest or not snap_5:
            # Cannot calculate momentum without at least 5 mins of history
            base_prob = 0.30
        else:
            # DELTA CALCULATION (MOMENTUM)
            # We look at what happened in the LAST 5 and 10 minutes, not the whole match.
            delta_sot = (latest.get("home_shots_on_target", 0) or 0) - (snap_5.get("home_shots_on_target", 0) or 0)
            delta_sot += (latest.get("away_shots_on_target", 0) or 0) - (snap_5.get("away_shots_on_target", 0) or 0)
            
            delta_cor = (latest.get("home_corners", 0) or 0) - (snap_5.get("home_corners", 0) or 0)
            delta_cor += (latest.get("away_corners", 0) or 0) - (snap_5.get("away_corners", 0) or 0)
            
            # If 10 min snapshot is available, we can boost if sustained pressure
            if snap_10:
                delta_sot_10 = (latest.get("home_shots_on_target", 0) or 0) - (snap_10.get("home_shots_on_target", 0) or 0)
                delta_sot_10 += (latest.get("away_shots_on_target", 0) or 0) - (snap_10.get("away_shots_on_target", 0) or 0)
                if delta_sot_10 >= 3:
                    delta_sot += 1 # Boost intensity for sustained pressure
            
            # Intensity is strictly based on DELTA (recent momentum), not absolute
            # 2 shots on target in 5 mins is HUGE (0.3). 1 corner is 0.1.
            intensity = (delta_sot * 0.15) + (delta_cor * 0.08)
            
            # Bot specific modifiers
            if "xg_sniper" in self.name: intensity *= 1.2
            elif "corner" in self.name: intensity += (delta_cor * 0.10)
            elif "possession" in self.name: intensity *= 1.1
            elif "first_half" in self.name and minute < 45: intensity *= 1.5
            elif "late_drama" in self.name and minute > 75: intensity *= 1.5
            elif "momentum" in self.name: intensity *= 1.3
            
            base_prob = 0.35 + intensity
            
        base_prob = min(base_prob, 0.85)
        decision = "goal" if base_prob >= 0.62 else "no_goal"
        
        return BotPrediction(
            match_id=str(match_id),
            bot_name=self.name,
            bot_version=self.version,
            minute=minute,
            period='H2' if minute > 45 else 'H1',
            market='total_goals',
            confidence='high' if base_prob >= 0.75 else ('medium' if base_prob >= 0.62 else 'low'),
            decision=decision,
            probability=base_prob,
            reasons=[f"{self.name} 5/10 dk momentum analizi tamamladi."],
            data_quality=1.0,
            snapshot_id=latest.get("id") if latest else None
        )
