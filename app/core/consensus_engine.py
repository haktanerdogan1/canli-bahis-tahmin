from typing import List, Dict, Optional
from datetime import datetime
from pydantic import BaseModel, Field
from app.schemas.bot_prediction import BotPrediction

# CANLI VERI SARTI (2026-09-10, kullanici: "sonuclar rezalet" - o gun
# isabet %55.6, olculmus taban oranin [%61.6] BILE altinda).
#
# Bu dort bot, macin canli istatistigine HIC bakmadan her zaman oy verir:
# tarihsel taban oran, skor/sure durumu, mac oncesi tahmin ve acilis orani.
# Canli istatistik kapsamasi dar oldugunda (RapidAPI cogu ligde sut/xG/korner
# vermiyor) geri kalan 14 bot "insufficient_data" ile cekiliyor ve sinyal
# SADECE bu dortluyle uretiliyordu - canli loglarda gorulen desen:
#     oy_veren=4 pos=2 neg=2 eksik=14 mutabakat=0.5 final_prob=0.648
# Yani "CANLI SINYAL" diye paylasilan sey, iceriginde macin o anina dair
# TEK BIR bilgi tasimayan bir mac-oncesi tahmindi; 2'ye 2 bolunmus bir oy
# esigi tam sinirda geciyordu. Sonuc yazi-tura.
#
# Kural: bir sinyal (guclu_aday) ancak GERCEK canli istatistikle calisan
# EN AZ BIR bot "gol" dediyse acilir. Aksi halde en fazla "izleme".
# NOT (Astra K5): sart bot SINIFINA baglanmaz - GameStateBot da Specialist
# ailesindendir ama canli istatistik GEREKTIRMEZ. O yuzden acik isim listesi.
CANLI_VERI_GEREKTIRMEYEN = frozenset({
    "bot_base_rate",
    "bot_game_state",
    "bot_19_prematch_prophet",
    "bot_odds_profile",
})

class ConsensusResult(BaseModel):
    match_id: str
    snapshot_id: Optional[int] = None
    consensus_version: str = "v1.0.0"
    positive_bot_count: int
    negative_bot_count: int
    insufficient_data_count: int
    weighted_probability: float
    canli_veri_oyu: int = 0  # gercek canli istatistikle "gol" diyen bot sayisi
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
            # Piyasa fiyatindan arsiv esleme - out-of-sample dogrulandi, cokmedi.
            # 2026-08-24: iddaa_odds_client.py (acilis orani) ile yeniden aktif
            # edildi (bkz. orchestrator.py). Baslangic agirligi mutevazi tutuldu -
            # yeterli sonuclanmis sinyal birikince (bkz. /api/admin/panel/
            # istatistikler) olculup ayarlanmali, digerlerinden farkli olarak
            # henuz CANLI performansi yok (sadece arsiv-ici out-of-sample var).
            "bot_odds_profile":         0.06,
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
        canli_veri_oyu = 0  # bkz. CANLI_VERI_GEREKTIRMEYEN

        total_weight = 0.0
        weighted_prob_sum = 0.0

        for p in predictions:
            if p.decision == "insufficient_data":
                insufficient_count += 1
                continue

            weight = self.bot_weights.get(p.bot_name, 0.05)

            # Veri kalitesine göre ağırlığı cezalandır
            effective_weight = weight * p.data_quality

            # DUZELTME (2026-09-08, GPT-6 Astra ikinci-gorus incelemesi):
            # agirligi 0.00 olan bir bot (ornegin bot_draw_breaker, olculmus
            # %22.2 isabetle bilerek devre disi birakildi) eskiden BURADA
            # hala pos_count/neg_count'a (dolayisiyla oy_veren VE mutabakat
            # esigine) katiliyordu - sadece weighted_prob_sum'a katkisi 0
            # oluyordu. Yani "agirlik 0 = bu botu dinleme" niyeti tam
            # uygulanmiyordu: o bot yine de mutabakati 0.49'dan 0.51'e
            # cekip bir sinyali TETIKLEYEBILIYORDU, olasiligina hic
            # guvenilmemesine ragmen. Artik effective_weight<=0 olan bir
            # oy hicbir sayaca (oy_veren, mutabakat, final_prob) girmiyor -
            # agirligi sifirlanmis bir bot artik GERCEKTEN devre disi.
            if effective_weight <= 0:
                continue

            if p.decision == "goal":
                pos_count += 1
                # Sinyali ancak GERCEK canli veriye bakan bir bot ACABILIR.
                if p.bot_name not in CANLI_VERI_GEREKTIRMEYEN:
                    canli_veri_oyu += 1
            else:
                neg_count += 1

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
        # GECICI/OLCULMUS AYAR (2026-09-08): canli istatistik kapsami (sut/
        # korner/xG - flashscore kaynakli) o an OLCULEN sadece ~%5 - bu
        # yuzden Specialist ailesindeki 14 bot COGU macta insufficient_data
        # donuyor, oy_veren neredeyse HICBIR ZAMAN 5'e ulasmiyordu. Esik
        # gecici olarak 4'e indirildi - AMA bu, 4 "her zaman veri bulunan"
        # bot (base_rate, game_state, 19_prematch_prophet, odds_profile)
        # TEK BASINA esigi karsilayabilmesi demekti: canli loglarda macin
        # 1-4. dakikasinda, HICBIR canli istatistik toplanmamisken, sadece
        # bu 4 botla "guclu_aday" sinyalleri atildigi GORULDU (2026-09-09,
        # kullanici tarafindan fark edildi - "CANLI SİNYAL" diye paylasilan
        # ama fiilen mac-oncesi tahminden farksiz sinyaller). KULLANICI
        # KARARI (2026-09-09): esik tekrar 5'e cikarildi - bir sinyalin
        # en az BIR Specialist-aile (canli veri gerektiren) botun oyunu
        # icermesi sart kosuluyor, yalnizca "her zaman hazir" 4 bot artik
        # YETMIYOR. Bu, dun 5->4 indirilme gerekcesini (dusuk kapsam) geri
        # getiriyor - sinyal sayisi yine azalacak, kullanici bunu bilerek
        # veri kalitesini sinyal miktarina tercih etti. (Ayrica bkz.
        # orchestrator.py MIN_SIGNAL_MINUTE=10 - ayni sorunun ikinci,
        # bagimsiz bir onlemi.)
        #
        # TEKRAR 4'E INDIRILDI (2026-09-09, ayni gun, birkac saat sonra):
        # 5 esigi ile canli TESHIS loglarinda saatlerce NEREDEYSE HICBIR
        # mac "izleme"/"guclu_aday" seviyesine ulasamadi (v4_api_bot'un
        # istatistik kapsami MAX_STATS_PER_CYCLE=7 ile hala dar) - sinyal
        # sayisi neredeyse sifira dustu. Esigi 5'e cikarmanin GERCEK
        # amaci (yukaridaki not) "4 hazir bot TEK BASINA sinyal actirmasin"
        # idi - o spesifik sorun artik BAGIMSIZ olarak MIN_SIGNAL_MINUTE=10
        # tarafindan cozuluyor (mac baslar baslamaz sinyal acilamiyor,
        # dk>=10 sart). Ayni korumayi iki kez (esik + dakika kapisi)
        # uygulamak gereksiz sikilik - dakika kapisi tek basina yeterli,
        # esik tekrar 4'e cekildi.
        #
        # 2026-09-10 EKI: "dakika kapisi tek basina yeterli" varsayimi URETIMDE
        # YANLIS CIKTI. MIN_SIGNAL_MINUTE=10 sinyali sadece ERTELIYOR; ayni 4
        # hazir bot 10., 15., 20. dakikada da tek baslarina esigi geciyor.
        # O gunun canli loglari (dk 10-25 arasi, feed'de 20-26 mac):
        #     oy_veren=4 pos=3 neg=1 eksik=14   -> guclu_aday
        #     oy_veren=4 pos=2 neg=2 eksik=14   -> guclu_aday (2'ye 2!)
        # Gunun isabeti %55.6 - olculmus taban oranin (%61.6) ALTINDA, yani
        # sistem rastgeleden kotu. Cozum esigi oynatmak DEGIL (o denendi,
        # 5<->4 gidip geldi): sinyalin ICINDE canli veri OLMASINI sart kosmak.
        # Sayisal esikler AYNEN korunuyor - sadece "hic canli veri yoksa
        # sinyal degil, izleme" kurali ekleniyor.
        if oy_veren < 4:
            signal_level = "eksik_veri"
        elif mutabakat >= 0.50 and final_prob >= 0.63:
            if canli_veri_oyu >= 1:
                signal_level = "guclu_aday"
                decision = "signal"
            else:
                # Esikleri geciyor ama tek bir canli-veri botu bile "gol"
                # demedi - bu bir mac-oncesi tahmin, canli sinyal degil.
                signal_level = "izleme"
        elif mutabakat >= 0.40 and final_prob >= 0.55:
            signal_level = "izleme"

        return ConsensusResult(
            match_id=match_id,
            snapshot_id=snapshot_id,
            positive_bot_count=pos_count,
            negative_bot_count=neg_count,
            insufficient_data_count=insufficient_count,
            canli_veri_oyu=canli_veri_oyu,
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
