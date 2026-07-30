import sqlite3
from typing import Dict, Any, Tuple
from app.bots.base_bot import BaseGoalBot
from app.schemas.bot_prediction import BotPrediction
import math

class TeamFormBot(BaseGoalBot):
    def __init__(self):
        super().__init__(name="bot_1_team_form", version="v1.0.0")

    def _fetch_team_history(self, cursor, team_id: str, limit: int = 20) -> list:
        """
        Takımın geçmiş maçlarını tarihe göre azalan sırada (en yeni en üstte) getirir.
        """
        cursor.execute('''
            SELECT * FROM team_match_history 
            WHERE team_id = ? 
            ORDER BY match_date DESC 
            LIMIT ?
        ''', (team_id, limit))
        return cursor.fetchall()

    def _calculate_fh_stats(self, matches: list, limit: int) -> Tuple[float, int, int]:
        """
        Verilen maç listesinin ilk 'limit' kadarı için İlk Yarı Gol oranını hesaplar.
        Dönüş: (oran, toplam_gol_atilan, toplam_gol_yenen)
        """
        subset = matches[:limit]
        if not subset:
            return 0.0, 0, 0
            
        fh_goal_matches = 0
        total_scored = 0
        total_conceded = 0
        
        for m in subset:
            scored = m["fh_goals_scored"] if m["fh_goals_scored"] is not None else 0
            conceded = m["fh_goals_conceded"] if m["fh_goals_conceded"] is not None else 0
            
            total_scored += scored
            total_conceded += conceded
            
            if (scored + conceded) > 0:
                fh_goal_matches += 1
                
        rate = fh_goal_matches / len(subset)
        return rate, total_scored, total_conceded

    def predict(self, match_context: Dict[str, Any]) -> BotPrediction:
        """
        match_context expects:
        - match_id_db: int
        - home_team_id: str
        - away_team_id: str
        - current_minute: int
        - db_path: str
        """
        match_id_db = match_context.get("match_id_db")
        home_team_id = match_context.get("home_team_id")
        away_team_id = match_context.get("away_team_id")
        current_minute = match_context.get("current_minute", 0)
        db_path = match_context.get("db_path")

        if not all([match_id_db, home_team_id, away_team_id, db_path]):
            return self._build_error("insufficient_data", "Missing context parameters")

        total_goals = match_context.get("total_goals", 0)
        target_market = f"İlk Yarı {total_goals + 0.5} Üst" if current_minute <= 45 else f"Maç Sonu {total_goals + 0.5} Üst"

        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            # Ev sahibi ve Deplasman geçmişini çek
            home_matches = self._fetch_team_history(cursor, home_team_id, 20)
            away_matches = self._fetch_team_history(cursor, away_team_id, 20)
            conn.close()

            # Yeterli maç var mı? (En az 5 maç geçmişi olmalı)
            if len(home_matches) < 5 or len(away_matches) < 5:
                return BotPrediction(
                    bot_name=self.name,
                    bot_version=self.version,
                    match_id=str(match_id_db),
                    snapshot_id=None,
                    minute=current_minute,
                    period="first_half" if current_minute <= 45 else "second_half",
                    market=target_market,
                    decision="no_goal",
                    probability=0.50,
                    confidence="low",
                    data_quality=0.3,
                    reasons=["Tarihçe verisi yetersiz olduğu için nötr (%50) oran atandı."],
                    warnings=[f"Yetersiz maç geçmişi (Ev: {len(home_matches)}, Dep: {len(away_matches)})"],
                    sample_size=len(home_matches) + len(away_matches)
                )

            # Ev sahibi hesaplamaları
            home_5_rate, h5_s, h5_c = self._calculate_fh_stats(home_matches, 5)
            home_10_rate, h10_s, h10_c = self._calculate_fh_stats(home_matches, 10)
            home_20_rate, _, _ = self._calculate_fh_stats(home_matches, 20)

            # Deplasman hesaplamaları
            away_5_rate, a5_s, a5_c = self._calculate_fh_stats(away_matches, 5)
            away_10_rate, a10_s, a10_c = self._calculate_fh_stats(away_matches, 10)
            away_20_rate, _, _ = self._calculate_fh_stats(away_matches, 20)

            # Çapraz Hücum/Savunma özellikleri (Sadece İlk Yarı için)
            home_attack_rating = (h10_s / 10.0) if len(home_matches) >= 10 else 0
            away_defense_rating = (a10_c / 10.0) if len(away_matches) >= 10 else 0
            away_attack_rating = (a10_s / 10.0) if len(away_matches) >= 10 else 0
            home_defense_rating = (h10_c / 10.0) if len(home_matches) >= 10 else 0

            # Kombine gol atma potansiyeli
            # Ev sahibinin atma gücü * Deplasmanın yeme zayıflığı
            home_score_prob = (home_attack_rating * 0.6) + (away_defense_rating * 0.4)
            away_score_prob = (away_attack_rating * 0.6) + (home_defense_rating * 0.4)

            # Ağırlıklı genel İY gol oranı (Üstel zaman ağırlığı mantığıyla)
            # Son 5 maça %50, son 10 maça %30, son 20 maça %20 ağırlık.
            home_weighted_rate = (home_5_rate * 0.50) + (home_10_rate * 0.30) + (home_20_rate * 0.20)
            away_weighted_rate = (away_5_rate * 0.50) + (away_10_rate * 0.30) + (away_20_rate * 0.20)

            # Ortak İY Gol Olasılığı
            base_probability = (home_weighted_rate + away_weighted_rate) / 2.0
            
            # Form çarpanı (Eğer iki takımın hücum-savunma çarpışması yüksekse ihtimali artır)
            if home_score_prob > 0.8 or away_score_prob > 0.8:
                base_probability += 0.10
            elif home_score_prob < 0.4 and away_score_prob < 0.4:
                base_probability -= 0.10

            reasons = []
            warnings = []

            # Nedenler
            reasons.append(f"Ev sahibinin son 10 maçının %{int(home_10_rate*100)}'sinde ilk yarı gol oldu.")
            reasons.append(f"Deplasmanın son 10 maçının %{int(away_10_rate*100)}'sinde ilk yarı gol oldu.")
            if home_5_rate >= 0.8:
                reasons.append("Ev sahibi son 5 maçta ilk yarılarda çok yüksek skor üretiyor.")
            if away_defense_rating > 1.0:
                reasons.append("Deplasman takımı son maçlarında ilk yarılarda kolay gol yiyor.")

            # Uyarılar
            if len(home_matches) < 10 or len(away_matches) < 10:
                warnings.append("Takımlardan birinin son 10 maç geçmişi tam değil, istatistikler 5 maç üzerinden ağırlıklandırıldı.")
            
            if home_10_rate < 0.4 and away_10_rate < 0.4:
                warnings.append("İki takım da ilk yarılarda oldukça kısır geçiren ekipler.")

            # Kalan süre filtresi (Form botu canlı tempoya bakmaz ama süre azaldıysa olasılık matematiksel olarak düşer)
            time_decay = 0.0
            if current_minute > 30 and current_minute <= 45:
                # 30-45 arası her dakika için %2 düşüş
                time_decay = (current_minute - 30) * 0.02
                warnings.append(f"{current_minute}. dakikaya girildiği için form beklentisi zaman etkisiyle azaltıldı.")

            final_probability = max(0.05, min(0.95, base_probability - time_decay))
            
            decision = "goal" if final_probability >= 0.65 else "no_goal"
            confidence = "high" if final_probability >= 0.75 else "medium" if final_probability >= 0.60 else "low"

            return BotPrediction(
                bot_name=self.name,
                bot_version=self.version,
                match_id=str(match_id_db),
                snapshot_id=None,
                minute=current_minute,
                period="first_half" if current_minute <= 45 else "second_half",
                market=target_market,
                decision=decision,
                probability=round(final_probability, 2),
                confidence=confidence,
                data_quality=0.9 if len(home_matches) >= 10 else 0.7,
                reasons=reasons,
                warnings=warnings,
                sample_size=len(home_matches) + len(away_matches)
            )

        except Exception as e:
            return self._build_error("insufficient_data", f"Error calculating team form: {str(e)}")

    def _build_error(self, decision: str, reason: str, match_id: str = "unknown", market: str = "unknown", minute: int = 0) -> BotPrediction:
        return BotPrediction(
            bot_name=self.name,
            match_id=str(match_id),
            snapshot_id=None,
            minute=minute,
            period="first_half" if minute <= 45 else "second_half",
            market=market,
            decision=decision,
            probability=None,
            confidence="low",
            data_quality=0.0,
            warnings=[reason]
        )
