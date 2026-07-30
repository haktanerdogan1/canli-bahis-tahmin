import sqlite3
from typing import Dict, Any
from app.bots.base_bot import BaseGoalBot
from app.schemas.bot_prediction import BotPrediction

class EarlyBlitzBot(BaseGoalBot):
    def __init__(self):
        super().__init__(name="bot_17_early_blitz", version="v1.0.0")

    def predict(self, match_context: Dict[str, Any]) -> BotPrediction:
        match_id = match_context.get("match_id_db")
        minute = match_context.get("current_minute", 0)
        latest = match_context.get("latest")
        
        # Sadece ilk 10 dakikada (veya ikinci yarının ilk 10 dakikası 46-55) devrede olan bir bot
        is_early_h1 = (0 < minute <= 10)
        is_early_h2 = (45 < minute <= 55)
        
        if not latest or not (is_early_h1 or is_early_h2):
            return BotPrediction(
                match_id=str(match_id), bot_name=self.name, bot_version=self.version,
                minute=minute, period='H2' if minute > 45 else 'H1', market='total_goals',
                confidence='low', decision="no_goal", probability=0.30,
                reasons=[f"{self.name} sadece ilk dakikalardaki baskiyi inceler."],
                data_quality=1.0, snapshot_id=latest.get("id") if latest else None
            )

        # Hızlı başlangıç metrikleri (Absolute / Minute)
        h_sot = latest.get("home_shots_on_target", 0) or 0
        a_sot = latest.get("away_shots_on_target", 0) or 0
        h_cor = latest.get("home_corners", 0) or 0
        a_cor = latest.get("away_corners", 0) or 0
        h_da = latest.get("home_dangerous_attacks", 0) or 0
        a_da = latest.get("away_dangerous_attacks", 0) or 0

        # Dakika başına düşen aksiyon hızı
        active_minutes = minute if is_early_h1 else (minute - 45)
        if active_minutes <= 0:
            active_minutes = 1
            
        # Dakika başına tehlikeli atak, korner, isabetli şut oranları
        sot_per_min = (h_sot + a_sot) / active_minutes
        cor_per_min = (h_cor + a_cor) / active_minutes
        da_per_min = (h_da + a_da) / active_minutes
        
        intensity = 0.0
        
        # 4. dakikada 2 isabetli şut = 0.5 sot_per_min -> İnanılmaz bir başlangıç!
        if sot_per_min >= 0.4:
            intensity += 0.35
        elif sot_per_min >= 0.2:
            intensity += 0.20
            
        if cor_per_min >= 0.5: # 4. dakikada 2 korner
            intensity += 0.20
            
        if da_per_min >= 1.5: # Dakikada 1.5 tehlikeli atak
            intensity += 0.15
            
        base_prob = 0.35 + intensity
        base_prob = min(base_prob, 0.95)
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
            reasons=[f"{self.name} Erken Baski (Blitz) analizi: Dk basi SOT={sot_per_min:.2f}, DA={da_per_min:.2f}"],
            data_quality=1.0,
            snapshot_id=latest.get("id") if latest else None
        )
