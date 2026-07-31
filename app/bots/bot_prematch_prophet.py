"""
bot_19_prematch_prophet — Mac Oncesi Kahin.

NEDEN VAR:
  Diger botlarin TAMAMI canli momentuma (son 5-10 dk'daki sut/korner artisi) bakiyor.
  Bunun yapisal bir kor noktasi var: macin ilk 15-20 dakikasinda henuz karsilastirilacak
  veri yoktur, dolayisiyla sistem hicbir sinyal uretemez. Olculdu: uretilen en erken
  sinyal 15. dakikaydi, ilk 20 dakikada toplam 3 sinyal vardi.

  Oysa "Ilk Yari 0.5 Ust" gibi en degerli marketler tam da macin basinda oynanir.

BU BOT:
  Canli veriye HIC bakmaz. Sadece iki takimin arsivden cikarilmis tarihsel gol
  profiline bakar (33.525 maclik arsiv, 3.504 takim). Boylece macin 1. dakikasinda
  bile bilgiye dayali karar verebilir.

DURUSTLUK KURALI:
  Takim arsivde bulunamazsa veya yeterli mac yoksa 'insufficient_data' doner -
  uydurma olasilik uretmez. Veri kalitesi (data_quality) elimizdeki mac sayisina
  gore dusurulur, boylece konsensus motoru bu botu az veri varken daha az dinler.
"""
from typing import Any, Dict

from app.bots.base_bot import BaseGoalBot
from app.schemas.bot_prediction import BotPrediction

# Arsivden olculen taban oranlar (33.525 mac):
#   ilk yarida en az 1 gol -> %61.6
#   mac sonu 1.5 ust       -> %73.7
TABAN_IY = 0.616
TABAN_MS15 = 0.737

# KALICILIK KATSAYILARI - out-of-sample olculmus.
#
# Her takimin maclari kronolojik ikiye bolunup, ilk yarisindan cikan profilin
# ikinci yarisini ne kadar tahmin ettigi olculdu (1.558 takim, >=20 mac):
#
#   ozellik                gecmiste gorunen fark -> gelecege tasinan   korelasyon
#   ilk yari gol orani           42.6 puan            9.5 puan  (%22)     0.387
#   mac sonu 1.5 ust             32.8 puan            4.7 puan  (%14)     0.150
#   (2. yari gol egilimi         31.7 puan            2.9 puan  (%9)      0.110  -> GURULTU, kullanilmadi)
#
# Yani bir takimin gecmis profili gelecegi ZAYIF tahmin ediyor. Profildeki
# sapmanin tamamini kullanmak ASIRI OZGUVEN uretir; bu yuzden sapma olculmus
# kalicilik oraniyla carpilarak tabana dogru cekiliyor (shrinkage).
KALICILIK_IY = 0.22
KALICILIK_MS = 0.14


class PrematchProphetBot(BaseGoalBot):
    def __init__(self):
        super().__init__(name="bot_19_prematch_prophet", version="v1.0.0")

    def predict(self, ctx: Dict[str, Any]) -> BotPrediction:
        minute = ctx.get("current_minute", 0) or 0
        match_id = ctx.get("match_id_db")
        latest = ctx.get("latest")
        pre = ctx.get("prematch")

        def bos(reason, warn=None):
            return BotPrediction(
                match_id=str(match_id), bot_name=self.name, bot_version=self.version,
                minute=minute, period="H1" if minute <= 45 else "H2",
                market="total_goals", decision="insufficient_data",
                probability=None, confidence="low", data_quality=0.0,
                reasons=[reason], warnings=[warn] if warn else [],
                snapshot_id=latest.get("id") if latest else None,
            )

        if not pre:
            return bos("Takim arsivde bulunamadi - mac oncesi profil yok.")

        n = pre.get("veri_mac_sayisi", 0)
        if n < 5:
            return bos(f"Yetersiz gecmis mac ({n}) - guvenilir profil cikarilamaz.")

        skor = (ctx.get("home_score") or 0) + (ctx.get("away_score") or 0)

        # Ilk yaridaysak ve henuz gol yoksa -> "ilk yari golu gelir mi?"
        # Aksi halde -> "mac sonu bir gol daha gelir mi?"
        if minute <= 45 and skor == 0:
            taban, oran = TABAN_IY, pre["fh_goal_rate"]
            hedef = "ilk yari golu"
            # Dakika ilerledikce ilk yaridaki gol ihtimali azalir (kalan sure kisaliyor)
            kalan = max(0.0, (45 - minute) / 45.0)
            oran = oran * (0.35 + 0.65 * kalan)
        else:
            taban, oran = TABAN_MS15, pre["over_15_rate"]
            hedef = "mac sonu ek gol"

        # IKI KADEMELI KUCULTME:
        #   1. kalicilik: profilin gelecegi ne kadar tahmin ettigi (olculmus)
        #   2. guven     : elimizdeki mac sayisi (az macli takima az guven)
        # Ikisi de sapmayi tabana dogru ceker. Bu olmadan bot asiri ozguvenli
        # konusur - ornegin gecmiste %84 gorunen bir takima %84 der, oysa
        # olculmus kalicilikla dogru cevap ~%66'dir.
        kalicilik = KALICILIK_IY if (minute <= 45 and skor == 0) else KALICILIK_MS
        g = pre.get("guven", 0.5)
        olasilik = taban + (oran - taban) * kalicilik * g
        olasilik = max(0.05, min(0.92, olasilik))

        karar = "goal" if olasilik >= 0.62 else "no_goal"

        return BotPrediction(
            match_id=str(match_id), bot_name=self.name, bot_version=self.version,
            minute=minute, period="H1" if minute <= 45 else "H2",
            market="total_goals", decision=karar, probability=round(olasilik, 3),
            confidence="high" if olasilik >= 0.75 else ("medium" if olasilik >= 0.62 else "low"),
            data_quality=round(g, 2),
            reasons=[
                f"Mac oncesi profil ({n} mac): {hedef} beklentisi %{100*oran:.0f} "
                f"(taban %{100*taban:.0f}).",
                f"Olculmus kalicilik %{100*kalicilik:.0f} uygulandi -> %{100*olasilik:.0f}.",
                f"Beklenen toplam gol: {pre['beklenen_gol']:.2f}.",
            ],
            warnings=[] if g >= 0.7 else [f"Dusuk veri guveni ({g}) - tahmin tabana cekildi."],
            sample_size=n,
            snapshot_id=latest.get("id") if latest else None,
        )
