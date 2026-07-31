import time
import sqlite3
import os
import json
from app.bots.bot_prematch_prophet import PrematchProphetBot
from app.bots.specialists import tum_uzmanlar
from app.core.consensus_engine import ConsensusEngine
import settlement
import prematch

from db_config import DB_PATH  # Railway kalici disk destegi (bkz. db_config.py)
COOLDOWN_SECONDS = 300  # 5 dakika içinde aynı maça sinyal atma

def _ensure_schema():
    """Sema garantisi tek yerden (settlement.ensure_schema) yonetilir."""
    settlement.ensure_schema()

def run_orchestrator():
    print("🧠 Başlatılıyor: Sinyal Avcısı (Konsensüs Orkestratörü)", flush=True)
    _ensure_schema()

    # Arsivden takim profillerini kur (bir kez, acilista).
    # Bunlar botlarin macin ilk dakikalarinda kullanacagi mac oncesi bilgidir.
    try:
        prematch.build_profiles()
    except Exception as e:
        print(f"⚠️  Takim profilleri kurulamadi: {e}")
    
    # BOT KADROSU
    # Eski kadroda 18 bottan 12'si birebir ayni formulu kullaniyordu; bu yuzden
    # "18 bot oy verdi" demek aslinda tek botun oyunu 18 kez saymak anlamina
    # geliyordu. Yeni kadroda her bot AYRI bir bilgi ailesine bakar ve baktigi
    # veri yoksa durustce cekilir. Bir toplulugun deger uretmesi, uyelerinin
    # BAGIMSIZ hatalar yapmasina baglidir.
    bots = [PrematchProphetBot()] + tum_uzmanlar()

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
                
                # MAC ONCESI PROFIL: canli veri henuz yokken (ilk dakikalar) botlarin
                # dayanabilecegi tek bilgi kaynagi. Takim eslesmezse None doner ve
                # botlar bu bilgiyi kullanmaz - uydurma yapmaz.
                try:
                    pre = prematch.match_expectation(match['home_team_id'], match['away_team_id'])
                except Exception:
                    pre = None

                context = {
                    "prematch": pre,
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
                         signal_minute, market, initial_goals)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        signal_market,
                        total_goals_initial
                    ))

                    # HER BOTUN KARARINI AYRI AYRI KAYDET.
                    # Bu tablo onceden hic doldurulmuyordu (0 kayit), bu yuzden hangi botun
                    # ne kadar isabetli oldugu OLCULEMIYORDU. Artik her sinyalde 18 botun
                    # karari saklaniyor; sonuc kesinlestiginde bot bazli basari orani
                    # hesaplanabiliyor.
                    for bp in predictions:
                        try:
                            cursor.execute('''
                                INSERT INTO bot_predictions
                                (match_id, snapshot_id, bot_name, bot_version, decision,
                                 probability, confidence, data_quality, reasons_json, warnings_json)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ''', (
                                match_id,
                                latest['id'] if latest else None,
                                bp.bot_name,
                                bp.bot_version,
                                bp.decision,
                                bp.probability,
                                bp.confidence,
                                bp.data_quality,
                                json.dumps(bp.reasons, ensure_ascii=False),
                                json.dumps(bp.warnings, ensure_ascii=False),
                            ))
                        except Exception as be:
                            print(f"⚠️  Bot kaydi yazilamadi ({bp.bot_name}): {be}")

                    conn.commit()
                    
                    # Cooldown ekle
                    signal_cooldowns[match_id] = current_time
                    
                    print(f"🚨 SİNYAL BULUNDU! Maç: {match['home_team_id']} vs {match['away_team_id']} | Dakika: {minute} | Seviye: {consensus_result.signal_level} | Olasılık: %{int(consensus_result.weighted_probability * 100)}")
            
            conn.close()

            # Kesinlesen sinyalleri KALICI olarak sonuclandir.
            # Boylece gecmis, mac verisi degisse bile bozulmaz ve bot basari
            # oranlarini hesaplayabilecegimiz egitim verisi birikir.
            try:
                settlement.settle_pending()
            except Exception as se:
                print(f"⚠️  Sonuclandirma hatasi: {se}")

        except Exception as e:
            print(f"❌ Orkestratör Hatası: {e}")
            
        # 15 saniyede bir yeni sinyal tara
        time.sleep(15)

if __name__ == "__main__":
    run_orchestrator()
