import sqlite3
from typing import Dict, Any
from app.bots.base_bot import BaseGoalBot
from app.schemas.bot_prediction import BotPrediction
from datetime import datetime
import json

class LiveTempoBot(BaseGoalBot):
    def __init__(self):
        super().__init__(name="bot_3_live_tempo", version="v1.0.0")

    def predict(self, match_context: Dict[str, Any]) -> BotPrediction:
        """
        match_context expects:
        - match_id_db: int
        - current_minute: int
        - db_path: str
        """
        match_id = match_context.get("match_id_db")
        current_minute = match_context.get("current_minute")
        db_path = match_context.get("db_path")

        if not all([match_id, current_minute, db_path]):
            return self._build_error("insufficient_data", "Missing context parameters")

        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            # Maçın genel bilgilerini al
            cursor.execute("SELECT * FROM matches WHERE id = ?", (match_id,))
            match = cursor.fetchone()
            if not match:
                conn.close()
                return self._build_error("insufficient_data", "Match not found in DB")
                
            total_goals = match["home_score"] + match["away_score"]
            target_market = f"İlk Yarı {total_goals + 0.5} Üst" if current_minute <= 45 else f"Maç Sonu {total_goals + 0.5} Üst"

            # Snapshot'ları getir
            cursor.execute('''
                SELECT * FROM live_snapshots 
                WHERE match_id = ? 
                ORDER BY minute ASC
            ''', (match_id,))
            snapshots = cursor.fetchall()
            conn.close()

            if not snapshots:
                return self._build_error("insufficient_data", "No live snapshots available", match_id, target_market, current_minute)

            current_snap = snapshots[-1]
            snap_id = current_snap["id"]
            
            # Yeterli zaman farkı olan geçmiş bir snapshot bul (5-10 dk öncesi)
            past_snap = None
            target_past_minute = current_minute - 5
            
            for s in reversed(snapshots[:-1]):
                if s["minute"] <= target_past_minute:
                    past_snap = s
                    break
                    
            if not past_snap:
                return self._build_error("insufficient_data", "Not enough historical snapshots to calculate tempo (wait a few minutes)", match_id, target_market, current_minute, snap_id)

            mins_passed = current_snap["minute"] - past_snap["minute"]
            if mins_passed <= 0:
                return self._build_error("insufficient_data", "Invalid minute gap", match_id, target_market, current_minute, snap_id)

            # Yardımcı fonksiyon: None veya eksik veriyi 0'a çevirir
            def get_val(row, key, default=0):
                val = row[key]
                return default if val is None else val

            # Tempo hesaplama (Şut ve Korner İvmesi)
            current_sot = get_val(current_snap, "home_shots_on_target") + get_val(current_snap, "away_shots_on_target")
            past_sot = get_val(past_snap, "home_shots_on_target") + get_val(past_snap, "away_shots_on_target")
            delta_sot = max(0, current_sot - past_sot)

            current_shots = get_val(current_snap, "home_shots") + get_val(current_snap, "away_shots")
            past_shots = get_val(past_snap, "home_shots") + get_val(past_snap, "away_shots")
            delta_shots = max(0, current_shots - past_shots)

            current_cor = get_val(current_snap, "home_corners") + get_val(current_snap, "away_corners")
            past_cor = get_val(past_snap, "home_corners") + get_val(past_snap, "away_corners")
            delta_cor = max(0, current_cor - past_cor)
            
            current_attacks = get_val(current_snap, "home_dangerous_attacks") + get_val(current_snap, "away_dangerous_attacks")
            past_attacks = get_val(past_snap, "home_dangerous_attacks") + get_val(past_snap, "away_dangerous_attacks")
            delta_attacks = max(0, current_attacks - past_attacks)
            
            # xG Hesaplama
            total_xg = get_val(current_snap, "home_xg", 0.0) + get_val(current_snap, "away_xg", 0.0)
            if total_xg <= 0:
                total_xg = (current_sot * 0.11) + (current_cor * 0.03)

            # Kural 1: Çok yüksek tempo (5 dakikada en az 2 isabetli şut veya 3 korner)
            probability = 0.50
            reasons = []
            warnings = []
            
            sot_per_min = delta_sot / mins_passed
            cor_per_min = delta_cor / mins_passed
            attacks_per_min = delta_attacks / mins_passed if delta_attacks > 0 else 0

            reasons.append(f"Son {mins_passed} dakikada {delta_shots} şut ({delta_sot} isabetli) üretildi.")
            reasons.append(f"Son {mins_passed} dakikada toplam {delta_cor} korner kazanıldı.")
            
            if attacks_per_min > 2.0:
                reasons.append(f"Dakika başı tehlikeli atak çok yüksek ({attacks_per_min:.1f}).")
                probability += 0.15

            if delta_sot >= 2:
                probability += 0.20
            elif delta_sot == 1:
                probability += 0.10
                
            if delta_shots >= 4:
                probability += 0.15
            elif delta_shots >= 2:
                probability += 0.05
                
            if delta_sot == 0 and delta_shots == 0:
                warnings.append("Son bölümde şut yok.")
                probability -= 0.10

            if delta_cor >= 2:
                probability += 0.10
                
            if total_xg > 1.2:
                reasons.append(f"Maçtaki toplam xG beklentisi oldukça yüksek ({total_xg:.2f}).")
                probability += 0.10

            # Kalan süre (İlk yarı bitimine yaklaşırken ihtimal düşer)
            if current_minute > 40 and current_minute <= 45:
                warnings.append("İlk yarının bitimine çok az süre kaldı, gol ihtimali düşüyor.")
                probability -= 0.20

            # Karar mekanizması
            probability = min(0.95, max(0.05, probability))
            
            decision = "goal" if probability >= 0.65 else "no_goal"
            confidence = "high" if probability >= 0.75 else "medium" if probability >= 0.60 else "low"
            
            if mins_passed < 3:
                warnings.append("Veri aralığı kısa (3 dakikadan az).")
                confidence = "low"
                data_quality = 0.6
            else:
                data_quality = 0.95

            return BotPrediction(
                bot_name=self.name,
                bot_version=self.version,
                match_id=str(match_id),
                snapshot_id=snap_id,
                minute=current_minute,
                period="first_half" if current_minute <= 45 else "second_half",
                market=target_market,
                decision=decision,
                probability=round(probability, 2),
                confidence=confidence,
                data_quality=data_quality,
                reasons=reasons,
                warnings=warnings,
                sample_size=len(snapshots)
            )

        except Exception as e:
            return self._build_error("insufficient_data", f"Error calculating tempo: {str(e)}")

    def _build_error(self, decision: str, reason: str, match_id: str = "unknown", market: str = "unknown", minute: int = 0, snap_id: int = None) -> BotPrediction:
        return BotPrediction(
            bot_name=self.name,
            match_id=str(match_id),
            snapshot_id=snap_id,
            minute=minute,
            period="first_half" if minute <= 45 else "second_half",
            market=market,
            decision=decision,
            probability=None,
            confidence="low",
            data_quality=0.0,
            warnings=[reason]
        )
