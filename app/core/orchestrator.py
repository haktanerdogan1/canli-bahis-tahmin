import time
import sqlite3
import os
import json
from app.bots.live_tempo_bot import LiveTempoBot
from app.bots.team_form_bot import TeamFormBot
from app.bots.bot_xg_sniper import XGSniperBot
from app.bots.bot_momentum import MomentumBot
from app.bots.bot_h2h import H2HOracleBot
from app.bots.bot_late_drama import LateDramaBot
from app.bots.bot_corner_pressure import CornerPressureBot
from app.bots.bot_red_advantage import RedAdvantageBot
from app.bots.bot_danger_zone import DangerZoneBot
from app.bots.bot_possession import PossessionDominatorBot
from app.bots.bot_shot_accuracy import ShotAccuracyBot
from app.bots.bot_favorite_trailing import FavoriteTrailingBot
from app.bots.bot_first_half import FirstHalfSpecialistBot
from app.bots.bot_draw_breaker import DrawBreakerBot
from app.bots.bot_underdog_bite import UnderdogBiteBot
from app.bots.bot_dangerous_attacks import DangerousAttackBot
from app.bots.bot_early_blitz import EarlyBlitzBot
from app.bots.bot_dark_form import DarkFormBot
from app.core.consensus_engine import ConsensusEngine

DB_PATH = os.path.join(os.path.dirname(__file__), '..', '..', 'database', 'fh_goal_predictor.db')
COOLDOWN_SECONDS = 300  # 5 dakika içinde aynı maça sinyal atma

def _ensure_schema():
    """consensus_predictions tablosuna signal_minute/market kolonlarini ekler (yoksa).
    Boylece sinyalin uretildigi dakika ve market adi DB'de SABIT olarak saklanir;
    api.py bunu daha sonra maçın o anki (degismis) dakikasindan yeniden hesaplamaya calismaz.
    Bu, ilk yarida uretilen bir sinyalin zamanla 'Mac Sonu' olarak yanlis etiketlenmesini onler."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    for ddl in (
        "ALTER TABLE consensus_predictions ADD COLUMN signal_minute INTEGER",
        "ALTER TABLE consensus_predictions ADD COLUMN market TEXT",
    ):
        try:
            cur.execute(ddl)
        except sqlite3.OperationalError:
            pass
    conn.commit()
    conn.close()

def run_orchestrator():
    print("🧠 Başlatılıyor: Sinyal Avcısı (Konsensüs Orkestratörü) - 18 AI BOTS ACTIVE...", flush=True)
    _ensure_schema()
    
    bots = [
        XGSniperBot(), TeamFormBot(), LiveTempoBot(), MomentumBot(),
        H2HOracleBot(), LateDramaBot(), CornerPressureBot(), RedAdvantageBot(),
        DangerZoneBot(), PossessionDominatorBot(), ShotAccuracyBot(), FavoriteTrailingBot(),
        FirstHalfSpecialistBot(), DrawBreakerBot(), UnderdogBiteBot(), DangerousAttackBot(), EarlyBlitzBot(), DarkFormBot()
    ]
    consensus_engine = ConsensusEngine()
    
    # Hangi maç için en son ne zaman sinyal ürettik? (Spam önleyici)
    # {match_id: last_signal_timestamp}
    signal_cooldowns = {}
    COOLDOWN_SECONDS = 300 # Aynı maça 5 dakikada bir sinyal at

    while True:
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            # 1. Sadece 'LIVE' statüsündeki maçları çek
            cursor.execute("SELECT id, source_match_id, home_team_id, away_team_id, minute, status, home_score, away_score, aggregate_score FROM matches WHERE status='LIVE'")
            live_matches = cursor.fetchall()
            
            current_time = time.time()
            
            for match in live_matches:
                match_id = match['id']
                minute = match['minute']
                aggregate_score = match['aggregate_score'] or ''
                
                # Sinyal kuralı: İlk yarı 35'e kadar (35-45 arası riskli), maç sonu 80'e kadar (80-90 arası riskli) sinyal ara
                if (35 < minute < 46) or (minute >= 80):
                    continue
                    
                # Eğer maç cooldown içerisindeyse atla
                if match_id in signal_cooldowns:
                    if (current_time - signal_cooldowns[match_id]) < COOLDOWN_SECONDS:
                        continue
                        
                # Check if we should allow a new signal based on score progression
                cursor.execute('''
                    SELECT s.home_score, s.away_score 
                    FROM consensus_predictions p 
                    JOIN live_snapshots s ON p.snapshot_id = s.id 
                    WHERE p.match_id = ? AND p.decision = 'signal' 
                    ORDER BY p.id DESC LIMIT 1
                ''', (match_id,))
                last_signal = cursor.fetchone()
                
                if last_signal:
                    last_total = (last_signal[0] or 0) + (last_signal[1] or 0)
                    current_total = (match['home_score'] or 0) + (match['away_score'] or 0)
                    if current_total <= last_total:
                        continue 
                
                # Get snapshots for momentum (delta) analysis
                cursor.execute("SELECT * FROM live_snapshots WHERE match_id = ? ORDER BY id DESC LIMIT 1", (match_id,))
                latest = cursor.fetchone()
                
                snap_5 = None
                snap_10 = None
                snap_15 = None
                
                if latest:
                    cursor.execute("SELECT * FROM live_snapshots WHERE match_id = ? AND minute <= ? ORDER BY id DESC LIMIT 1", (match_id, latest['minute'] - 5))
                    snap_5 = cursor.fetchone()
                    
                    cursor.execute("SELECT * FROM live_snapshots WHERE match_id = ? AND minute <= ? ORDER BY id DESC LIMIT 1", (match_id, latest['minute'] - 10))
                    snap_10 = cursor.fetchone()
                    
                    cursor.execute("SELECT * FROM live_snapshots WHERE match_id = ? AND minute <= ? ORDER BY id DESC LIMIT 1", (match_id, latest['minute'] - 15))
                    snap_15 = cursor.fetchone()
                
                context = {
                    "match_id_db": match_id,
                    "source_match_id": match["source_match_id"],
                    "home_team_id": match["home_team_id"],
                    "away_team_id": match["away_team_id"],
                    "current_minute": minute,
                    "home_score": match["home_score"],
                    "away_score": match["away_score"],
                    "aggregate_score": aggregate_score,
                    "db_path": DB_PATH,
                    "latest": dict(latest) if latest else None,
                    "snap_5": dict(snap_5) if snap_5 else None,
                    "snap_10": dict(snap_10) if snap_10 else None,
                    "snap_15": dict(snap_15) if snap_15 else None
                }
                
                # Tüm Botları çalıştır
                predictions = [bot.predict(context) for bot in bots]
                
                # Konsensüs
                consensus_result = consensus_engine.evaluate(predictions)
                
                if consensus_result.decision == "signal":
                    # Sinyalin uretildigi dakika ve hedeflenen market SABIT olarak hesaplanip kaydedilir.
                    # Boylece maç ilerledikce (ör. 2. yariya gecince) bu sinyal SONRADAN yanlislikla
                    # "Mac Sonu" marketine donusmez; hep uretildigi andaki (ilk yari / mac sonu) haliyle kalir.
                    total_goals_initial = (match["home_score"] or 0) + (match["away_score"] or 0)
                    is_first_half_market = minute <= 45
                    signal_market = (
                        f"İlk Yarı {total_goals_initial + 0.5} Üst" if is_first_half_market
                        else f"Maç Sonu {total_goals_initial + 0.5} Üst"
                    )

                    # DB'ye kaydet
                    cursor.execute('''
                        INSERT INTO consensus_predictions 
                        (match_id, snapshot_id, consensus_version, positive_bot_count, negative_bot_count, 
                         insufficient_data_count, weighted_probability, signal_level, decision,
                         signal_minute, market)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        match_id,
                        latest['id'] if latest else None,
                        consensus_result.consensus_version,
                        consensus_result.positive_bot_count,
                        consensus_result.negative_bot_count,
                        consensus_result.insufficient_data_count,
                        consensus_result.weighted_probability,
                        consensus_result.signal_level,
                        consensus_result.decision,
                        minute,
                        signal_market
                    ))
                    conn.commit()
                    
                    # Cooldown ekle
                    signal_cooldowns[match_id] = current_time
                    
                    print(f"🚨 SİNYAL BULUNDU! Maç: {match['home_team_id']} vs {match['away_team_id']} | Dakika: {minute} | Seviye: {consensus_result.signal_level} | Olasılık: %{int(consensus_result.weighted_probability * 100)}")
            
            conn.close()
            
        except Exception as e:
            print(f"❌ Orkestratör Hatası: {e}")
            
        # 15 saniyede bir yeni sinyal tara
        time.sleep(15)

if __name__ == "__main__":
    run_orchestrator()
