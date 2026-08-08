import time
import sqlite3
import os
import json
from app.bots.bot_prematch_prophet import PrematchProphetBot
from app.bots.bot_base_rate import BaseRateBot
from app.bots.bot_odds_profile import OddsProfileBot
from app.bots.specialists import tum_uzmanlar
from app.core.consensus_engine import ConsensusEngine
import settlement
import prematch
import odds as odds_mod
import baserates
import odds_profile

from db_config import DB_PATH, connect  # Railway kalici disk destegi (bkz. db_config.py)
COOLDOWN_SECONDS = 300  # 5 dakika içinde aynı maça sinyal atma


def _has_signal_for_half(cursor, match_id, minute):
    """Bir macin ilgili yarisinda daha once sinyal uretilmis mi?"""
    first_half = minute <= 45
    cursor.execute('''
        SELECT 1
        FROM consensus_predictions p
        LEFT JOIN live_snapshots s ON s.id = p.snapshot_id
        WHERE p.match_id = ? AND p.decision = 'signal'
          AND CASE
                WHEN COALESCE(p.signal_minute, s.minute, 0) <= 45 THEN 1
                ELSE 0
              END = ?
        LIMIT 1
    ''', (match_id, 1 if first_half else 0))
    return cursor.fetchone() is not None

AYNI_ANDA_ACIK_KAPASITE = 10


def _kapasite_kontrolu(cursor, yeni_olasilik):
    """En fazla AYNI_ANDA_ACIK_KAPASITE kadar PENDING sinyal ayni anda acik
    kalsin - "cok fazla acik bahis, secici olmaliyiz" karari (kullanici
    talebi). Kapasite doluysa:
      - yeni aday, acik olanlarin EN ZAYIFINDAN daha guvenilirse (daha yuksek
        agirlikli olasilik) o zayif sinyal VOID yapilip yerine yenisine yer
        acilir (outcome bir kez daha KALICI - VOID yazildiktan sonra bu kayda
        bir daha dokunulmaz, tipki normal sonuclanma gibi).
      - degilse yeni sinyal hic acilmaz (kapasite doluyken daha guvenilir
        olmayan bir aday icin yer acilmaz).
    Kapasite dolu degilse dogrudan True doner - hicbir sey degismez.
    """
    cursor.execute('''
        SELECT id, weighted_probability FROM consensus_predictions
        WHERE decision='signal' AND outcome IS NULL
        ORDER BY weighted_probability ASC
    ''')
    acik = cursor.fetchall()
    if len(acik) < AYNI_ANDA_ACIK_KAPASITE:
        return True

    en_zayif_id, en_zayif_olasilik = acik[0]
    if yeni_olasilik is not None and en_zayif_olasilik is not None and yeni_olasilik > en_zayif_olasilik:
        cursor.execute(
            "UPDATE consensus_predictions SET outcome='VOID', settled_at=CURRENT_TIMESTAMP "
            "WHERE id=? AND outcome IS NULL",
            (en_zayif_id,),
        )
        return True
    return False


def _ensure_schema():
    """Sema garantisi tek yerden (settlement.ensure_schema) yonetilir."""
    settlement.ensure_schema()
    settlement.backfill_ghost_losses()
    settlement.backfill_premature_fh_losses()
    settlement.backfill_false_void_stale_progress()
    settlement.backfill_void_reconsidered()

def run_orchestrator():
    print("🧠 Başlatılıyor: Sinyal Avcısı (Konsensüs Orkestratörü)", flush=True)
    _ensure_schema()

    # Arsivden takim profillerini kur (bir kez, acilista).
    # Bunlar botlarin macin ilk dakikalarinda kullanacagi mac oncesi bilgidir.
    try:
        prematch.build_profiles()
    except Exception as e:
        print(f"⚠️  Takim profilleri kurulamadi: {e}")

    # Olculmus taban oranlari hesapla (veri biriktikce her aciliste tazelenir)
    try:
        baserates.build()
        baserates.tazele()
    except Exception as e:
        print(f"⚠️  Taban oranlar hesaplanamadi: {e}")

    # Oran profili tablosu (arsiv oranlari -> tarihsel sonuc)
    try:
        odds_profile.build()
        odds_profile.tazele()
    except Exception as e:
        print(f"⚠️  Oran profilleri hesaplanamadi: {e}")
    
    # BOT KADROSU
    # Eski kadroda 18 bottan 12'si birebir ayni formulu kullaniyordu; bu yuzden
    # "18 bot oy verdi" demek aslinda tek botun oyunu 18 kez saymak anlamina
    # geliyordu. Yeni kadroda her bot AYRI bir bilgi ailesine bakar ve baktigi
    # veri yoksa durustce cekilir. Bir toplulugun deger uretmesi, uyelerinin
    # BAGIMSIZ hatalar yapmasina baglidir.
    # NOT: OddsProfileBot gecici olarak devre disi. Sebep: canli 1X2 oranini
    # MAC ONCESI oran bantlariyla karsilastiriyordu; mac ilerledikce beraberlik
    # favorilestigi icin her mac 'dengeli' gorunuyor ve bot ayirt edemiyordu.
    # Ayrica guclu sinyal (ust/alt orani, 8.2 puan) canli API'de yok; elimizdeki
    # 1X2 sadece 4.9 puanlik zayif sinyal veriyor. Dogru veri kaynagi bulununca
    # yeniden ele alinacak.
    bots = [PrematchProphetBot(), BaseRateBot()] + tum_uzmanlar()

    consensus_engine = ConsensusEngine()
    
    # Hangi maç için en son ne zaman sinyal ürettik? (Spam önleyici)
    # {match_id: last_signal_timestamp}
    signal_cooldowns = {}
    COOLDOWN_SECONDS = 300 # Aynı maça 5 dakikada bir sinyal at

    while True:
        try:
            conn = connect()
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

                # Dakika 0 ama skor 0 degil: API'nin dakika alani guvenilmez demektir
                # (mac gercekte devam ediyor ama "henuz baslamamis gibi" gorunuyor).
                # Boyle bir durumda "minute <= 45 -> Ilk Yari" varsayimi yanlis sinyal
                # uretir (ornek: Balkani-Bohemian 1-1 iken dakika 0 geldi, sistem
                # "mac yeni basladi" sanip "Ilk Yari 2.5 Ust" sinyali uretmisti).
                home_score = match['home_score'] or 0
                away_score = match['away_score'] or 0
                if minute == 0 and (home_score > 0 or away_score > 0):
                    continue

                # Sinyal kuralı: İlk yarı 35'e kadar (35-45 arası riskli), maç sonu 80'e kadar (80-90 arası riskli) sinyal ara
                if (35 < minute < 46) or (minute >= 80):
                    continue
                    
                # Eğer maç cooldown içerisindeyse atla
                if match_id in signal_cooldowns:
                    if (current_time - signal_cooldowns[match_id]) < COOLDOWN_SECONDS:
                        continue
                        
                # Bir mac icin her yarida EN FAZLA bir sinyal. Gol geldikten sonra
                # ayni yarida daha yuksek bir esik acmak (0.5 -> 1.5 gibi) hem ilk
                # sinyali ekranda gizliyor hem de gol sonrasi gereksiz risk yaratiyor.
                if _has_signal_for_half(cursor, match_id, minute):
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
                
                if consensus_result.decision == "signal" and not _kapasite_kontrolu(cursor, consensus_result.weighted_probability):
                    # Kapasite (10) dolu ve bu aday acik olanlarin en zayifindan
                    # daha guvenilir degil - sinyal hic acilmiyor.
                    continue

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
                    
                    # SINYAL ANINDAKI PIYASA FIYATINI KAYDET.
                    # Bu, "kac tuttu" degil "oranin ustunde mi tuttu" sorusunu
                    # cevaplayabilmemiz icin sart. Sadece sinyal uretildiginde
                    # cagriliyor (her mac icin degil) - kota israfi olmasin diye.
                    try:
                        odds_mod.kaydet(match_id, match["source_match_id"], minute)
                    except Exception as oe:
                        print(f"⚠️  Oran kaydedilemedi: {oe}")

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

            # VOID yazilmis sinyalleri kesin sonuca ulasip ulasmadigini kontrol
            # eder - kullanici talebi. settle_pending ile ayni cadansta calisir
            # (kapasite kontrolunden VOID olan maclar hala canli olabilir).
            try:
                settlement.reconcile_void_signals()
            except Exception as ve:
                print(f"⚠️  VOID yeniden kontrol hatasi: {ve}")

            # Kalici olarak asla cozulemeyecek VOID sinyalleri sil - kullanici talebi.
            try:
                settlement.delete_unresolvable_void()
            except Exception as de:
                print(f"⚠️  VOID silme hatasi: {de}")

            # Tum sinyalleri sonuclanmis maclari FINISHED'e kilitle - aksi
            # halde RapidAPI'nin bir onceki gunden tasidigi stale kayitlar,
            # sirf status='LIVE' oldugu icin restart-bypass'tan (tracked_ids)
            # sonsuza kadar yararlanip her deploy'da ekrana geri donuyordu.
            try:
                settlement.finalize_fully_settled_matches()
            except Exception as fe:
                print(f"⚠️  Mac kilitleme hatasi: {fe}")

            # GUVENLIK AGI: match tracking katmaninda ne olursa olsun, hicbir
            # sinyal 3 saatten uzun PENDING kalamaz. Ayri try/except - biri
            # patlarsa digeri yine de calissin.
            try:
                settlement.void_timed_out_signals()
            except Exception as ve:
                print(f"⚠️  Zaman asimi guvenlik agi hatasi: {ve}")

            # GUVENLIK AGI 2: last_progress_at zincirinden BAGIMSIZ, daha hizli
            # tetiklenen kontrol - 30dk+ PENDING VE macin dakikasi hala <20 ise
            # feed'in bu mac icin donmus/kesilmis oldugu kesindir.
            try:
                settlement.void_stuck_signals()
            except Exception as se2:
                print(f"⚠️  Takilma guvenlik agi hatasi: {se2}")

        except Exception as e:
            print(f"❌ Orkestratör Hatası: {e}")
            
        # 15 saniyede bir yeni sinyal tara
        time.sleep(15)

if __name__ == "__main__":
    run_orchestrator()
