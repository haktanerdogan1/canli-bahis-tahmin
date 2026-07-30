from typing import List, Dict, Optional
from datetime import datetime
from pydantic import BaseModel, Field
from app.schemas.bot_prediction import BotPrediction

class ConsensusResult(BaseModel):
    match_id: str
    snapshot_id: Optional[int] = None
    consensus_version: str = "v1.0.0"
    positive_bot_count: int
    negative_bot_count: int
    insufficient_data_count: int
    weighted_probability: float
    signal_level: str  # "none", "izleme", "guclu_aday", "cok_guclu"
    decision: str      # "signal", "no_signal"
    created_at: datetime = Field(default_factory=datetime.utcnow)

class ConsensusEngine:
    def __init__(self):
        # Ağırlıklar toplamı %100'e (1.0) eşit olmalı
        self.bot_weights = {
            "bot_1_xg_sniper": 0.05,
            "bot_1_team_form": 0.05,
            "bot_3_live_tempo": 0.15, 
            "bot_4_momentum": 0.10, # 0.15'ten 0.10'a düşürüldü
            "bot_5_h2h": 0.05,
            "bot_6_late_drama": 0.05,
            "bot_7_corner_pressure": 0.05,
            "bot_8_red_advantage": 0.05,
            "bot_9_danger_zone": 0.05,
            "bot_10_possession": 0.05,
            "bot_11_shot_accuracy": 0.05,
            "bot_12_favorite_trailing": 0.05,
            "bot_13_first_half": 0.05,
            "bot_14_draw_breaker": 0.02,
            "bot_15_underdog_bite": 0.03,
            "bot_16_dangerous_attacks": 0.05,
            "bot_17_early_blitz": 0.05,
            "bot_18_dark_form": 0.05
        }

    def evaluate(self, predictions: List[BotPrediction]) -> ConsensusResult:
        if not predictions:
            return self._build_empty_result("unknown")
            
        match_id = predictions[0].match_id
        snapshot_id = predictions[0].snapshot_id
        
        pos_count = 0
        neg_count = 0
        insufficient_count = 0
        
        total_weight = 0.0
        weighted_prob_sum = 0.0
        
        for p in predictions:
            if p.decision == "insufficient_data":
                insufficient_count += 1
                continue
                
            if p.decision == "goal":
                pos_count += 1
            else:
                neg_count += 1
                
            weight = self.bot_weights.get(p.bot_name, 0.05)
            
            # Veri kalitesine göre ağırlığı cezalandır
            effective_weight = weight * p.data_quality
            
            if p.probability is not None:
                weighted_prob_sum += (p.probability * effective_weight)
                total_weight += effective_weight

        if total_weight > 0:
            final_prob = weighted_prob_sum / total_weight
        else:
            final_prob = 0.0
            
        # Sinyal Seviyesi Kuralları (15 Bota Göre)
        signal_level = "none"
        decision = "no_signal"
        
        if insufficient_count > 10:
            signal_level = "eksik_veri"
        elif pos_count >= 8 and final_prob >= 0.70:
            signal_level = "cok_guclu"
            decision = "signal"
        elif pos_count >= 5 and final_prob >= 0.62:
            signal_level = "guclu_aday"
            decision = "signal"
        elif pos_count >= 3 and final_prob >= 0.55:
            signal_level = "izleme"

        return ConsensusResult(
            match_id=match_id,
            snapshot_id=snapshot_id,
            positive_bot_count=pos_count,
            negative_bot_count=neg_count,
            insufficient_data_count=insufficient_count,
            weighted_probability=round(final_prob, 3),
            signal_level=signal_level,
            decision=decision
        )

    def _build_empty_result(self, match_id: str) -> ConsensusResult:
        return ConsensusResult(
            match_id=match_id,
            positive_bot_count=0,
            negative_bot_count=0,
            insufficient_data_count=0,
            weighted_probability=0.0,
            signal_level="none",
            decision="no_signal"
        )
