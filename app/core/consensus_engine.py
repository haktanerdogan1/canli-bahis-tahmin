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
    signal_level: str  # "none", "eksik_veri", "izleme", "guclu_aday" (eski "cok_guclu" kaldirildi - bkz. ConsensusEngine.evaluate)
    decision: str      # "signal", "no_signal"
    created_at: datetime = Field(default_factory=datetime.utcnow)

class ConsensusEngine:
    def __init__(self):
        # Ağırlıklar toplamı %100'e (1.0) eşit olmalı
        # AGIRLIKLAR - bilgi ailesine gore dengelenmis.
        # Ayni aileden botlar birbirine benzer hata yapar; bu yuzden aile bazinda
        # toplam agirlik sinirli tutuluyor. Boylece kalabalik bir aile konsensusu
        # tek basina ele geciremiyor.
        self.bot_weights = {
            # Mac oncesi aile (canli veri gerekmez, macin 1. dakikasindan calisir)
            "bot_19_prematch_prophet": 0.12,
            # Olculmus tarihsel taban oran - sistemin "capasi", tek uydurmayan bot
            "bot_base_rate":            0.13,
            # Piyasa fiyatindan arsiv esleme - out-of-sample dogrulandi, cokmedi
            "bot_odds_profile":         0.00,  # devre disi (bkz. orchestrator.py)
            "bot_form_asymmetry":      0.05,
            # Kalite ailesi (uretilen pozisyonun degeri)
            "bot_xg_sniper":           0.09,
            "bot_finishing_gap":       0.06,
            "bot_shot_accuracy":       0.06,
            # Tempo / hacim ailesi
            "bot_tempo_scanner":       0.06,
            "bot_attack_volume":       0.05,
            # Degisim ailesi (son dakikalardaki hareket)
            "bot_momentum_surge":      0.07,
            "bot_acceleration":        0.06,
            "bot_corner_pressure":     0.05,
            # Mac durumu ailesi (skor/sure, istatistikten bagimsiz)
            "bot_game_state":          0.07,
            # devre disi (2026-08-24 admin panel olcumu: 7 sinyalde %28.6 isabet,
            # iddia ettigi guvenle [%64.7] gerceklesen arasindaki fark [+0.361]
            # listedeki en kotusuydu - CLAUDE.md'nin "korelasyon <0.25 ise yazma"
            # ilkesine gore zaten uretimde olmamasi gereken seviyede)
            "bot_draw_breaker":        0.00,
            "bot_red_card":            0.03,
            # Zaman penceresi ailesi
            "bot_early_blitz":         0.03,
            "bot_late_drama":          0.03,
            # Hakimiyet
            "bot_possession_dominance": 0.00,
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
        
        # ESIK KURALLARI
        # Eskiden mutlak sayiya bakiliyordu ("en az 8 bot evet desin"). Bu, tum
        # botlar hemen her zaman oy verdigi icin hic devreye girmiyordu: olculdu,
        # pozitif bot sayisi her sinyalde 12-14 arasindaydi ve "cok_guclu" ile
        # "guclu_aday" seviyeleri arasinda isabet farki YOKTU (%72.7 vs %71.4).
        #
        # Yeni kadroda botlar veri yoksa cekildigi icin oy veren bot sayisi maca
        # gore degisir. Bu yuzden mutlak sayi yerine KATILANLAR ICINDEKI ORAN'a
        # bakiyoruz; ayrica en az kac botun konustugunu da sart kosuyoruz.
        oy_veren = pos_count + neg_count
        mutabakat = (pos_count / oy_veren) if oy_veren else 0.0

        # NOT: Botlar artik gercekten bagimsiz oldugu icin OYBIRLIGI NADIRDIR -
        # ve bu tasarim geregidir. Cok yuksek mutabakat sarti koyarsak sistem
        # hicbir zaman sinyal uretmez. Asagidaki esikler baslangic degeridir;
        # yeterli sonuclanmis sinyal birikince /api/metrics'teki kalibrasyon
        # verisine gore AYARLANMALIDIR (su an veri az, tahmin yurutmuyoruz).
        # "cok_guclu" (mutabakat>=0.70 ve final_prob>=0.70) yukaridaki NOT'un
        # dedigi gibi yeterli veri birikince olculdu (2026-08-24, 1292 sonuclanmis
        # sinyal, admin panel): cok_guclu %68.1 isabet, guclu_aday %70.0 isabet -
        # yani DAHA KATI esik DAHA KOTU sonuc veriyor. Ayirmak icin yeterli
        # (mutabakat, final_prob) kombinasyonu bazinda ham veri yok - yeni sayilar
        # uydurmak yerine (bkz. proje kurali: olcmeden iddia yok) kanitlanmamis
        # ek katiligi kaldirdik. guclu_aday zaten tek sinyal esigi.
        if oy_veren < 5:
            signal_level = "eksik_veri"
        elif mutabakat >= 0.50 and final_prob >= 0.63:
            signal_level = "guclu_aday"
            decision = "signal"
        elif mutabakat >= 0.40 and final_prob >= 0.55:
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
