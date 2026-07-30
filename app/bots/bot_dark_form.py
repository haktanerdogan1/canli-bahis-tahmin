import re
from typing import Dict, Any
from app.bots.base_bot import BaseGoalBot
from app.schemas.bot_prediction import BotPrediction

class DarkFormBot(BaseGoalBot):
    """
    Karanlık Form Botu (GoalGPT Hacker):
    Canlı sahadaki şutlara veya kornerlere BAKMAZ.
    Sadece geçmiş maçtaki (Aggregate Score) güç dengesine ve formuna bakar.
    Eğer ilk maçta büyük bir fark atılmışsa veya çok gollü geçmişse (Örn: 6-2),
    bu maçın canlı momentumu "ölü" olsa bile erken gol uyarısı verir.
    """
    def __init__(self):
        super().__init__(name="bot_18_dark_form", version="v1.0.0")

    def predict(self, match_context: Dict[str, Any]) -> BotPrediction:
        minute = match_context.get("current_minute", 0)
        aggregate_str = match_context.get("aggregate_score", "")
        
        # Sadece maçın başlarında (ilk 35 dk) veya ikinci yarının başında (45-60) karanlık form devreye girer
        # Çünkü ilerleyen dakikalarda artık sahada ne oynandığı (canlı momentum) daha önemlidir.
        if not (0 < minute <= 35 or 45 < minute <= 60):
            return BotPrediction(
                match_id=str(match_context.get("match_id_db", 0)),
                minute=minute,
                period="1H" if minute <= 45 else "2H",
                market="İlk Yarı 0.5 Üst",
                bot_name=self.name,
                probability=0.5,
                confidence="none",
                decision="ignore",
                reasons=["Dark form only active early in halves."],
                data_quality=0.9
            )

        prob = 0.5
        reason = "No significant aggregate dominance."

        if aggregate_str:
            # Örnek aggregate_str: "6 - 2" veya "1 - 1"
            nums = re.findall(r'\d+', aggregate_str)
            if len(nums) >= 2:
                try:
                    s1 = int(nums[0])
                    s2 = int(nums[1])
                    diff = abs(s1 - s2)
                    total = s1 + s2
                    
                    # Eğer ilk maçta toplam 4+ gol olduysa (örn: 3-1, 2-2) veya bir takım 3+ fark attıysa (örn: 3-0)
                    if diff >= 3 or total >= 4:
                        prob = 0.85
                        reason = f"MASSIVE PRE-MATCH DOMINANCE DETECTED! Aggregate history is {aggregate_str}. High hidden goal expectancy."
                    elif diff >= 2 or total >= 3:
                        prob = 0.75
                        reason = f"Strong pre-match dominance. Aggregate {aggregate_str}."
                    elif total > 0:
                        prob = 0.60
                        reason = f"Slight pre-match advantage. Aggregate {aggregate_str}."
                except:
                    pass

        # Ayrıca bazı bilinen çok güçlü / dominant takımların isimlerinden (Odds simülasyonu) puan kırpabiliriz
        # Ancak şimdilik sadece Aggregate Score (GoalGPT'nin kullandığı gizli veri) üzerinden gidiyoruz.

        confidence = "none"
        if prob >= 0.8:
            confidence = "cok_guclu"
        elif prob >= 0.7:
            confidence = "guclu_aday"
        elif prob >= 0.6:
            confidence = "orta"

        # Calculate decision based on probability
        decision_val = "signal" if prob >= 0.70 else ("wait" if prob >= 0.50 else "ignore")

        return BotPrediction(
            match_id=str(match_context.get("match_id_db", 0)),
            minute=minute,
            period="1H" if minute <= 45 else "2H",
            market="İlk Yarı 0.5 Üst", # Placeholder, orchestrator handles exact market
            bot_name=self.name,
            probability=prob,
            confidence=confidence,
            decision=decision_val,
            reasons=[reason],
            data_quality=0.9
        )
