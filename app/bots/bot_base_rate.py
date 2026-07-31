"""
bot_base_rate — Tarihsel Taban Oran botu.

ISI:
  Diger botlarin hicbirinin yapmadigi seyi yapar: hicbir formul uydurmaz,
  hicbir katsayi tahmin etmez. Sadece KENDI ARSIVIMIZE bakar ve sorar:

      "Bu dakikada, bu skorda, gecmiste maclarin yuzde kacinda gol daha geldi?"

  Cevabi oldugu gibi doner. Bu yuzden sistemin "capasi"dir - digerleri
  istatistik yorumlarken bu bot sadece gecmisin ne dedigini soyler.

NEDEN AYRI BIR BOT:
  Diger uzmanlarin ic katsayilari (0.38, 0.075 gibi) elle konulmus tahminlerdi.
  Bu bot onlarin yanina OLCULMUS bir referans koyar. Konsensus agirliklari
  ileride bot performansina gore ogrenildiginde, hangisinin daha isabetli
  oldugu veriyle ortaya cikacak.

DURUSTLUK:
  Yeterli ornek yoksa (MIN_ORNEK altinda) daha genis kirilima duser; hicbir
  veri yoksa 'insufficient_data' doner. Az veriyle kesin konusmaz.
"""
from typing import Any, Dict

import baserates
from app.bots.base_bot import BaseGoalBot
from app.schemas.bot_prediction import BotPrediction


class BaseRateBot(BaseGoalBot):
    def __init__(self):
        super().__init__(name="bot_base_rate", version="v1.0.0")

    def predict(self, ctx: Dict[str, Any]) -> BotPrediction:
        minute = ctx.get("current_minute", 0) or 0
        gol = (ctx.get("home_score") or 0) + (ctx.get("away_score") or 0)
        latest = ctx.get("latest")
        ortak = dict(
            match_id=str(ctx.get("match_id_db")), bot_name=self.name,
            bot_version=self.version, minute=minute,
            period="H1" if minute <= 45 else "H2", market="total_goals",
            snapshot_id=latest.get("id") if latest else None,
        )

        try:
            p, ornek, kirilim = baserates.oran(minute, gol)
        except Exception as e:
            return BotPrediction(**ortak, decision="insufficient_data", probability=None,
                                 confidence="low", data_quality=0.0,
                                 reasons=["Taban oran tablosu okunamadi."],
                                 warnings=[str(e)])

        if p is None:
            return BotPrediction(**ortak, decision="insufficient_data", probability=None,
                                 confidence="low", data_quality=0.0,
                                 reasons=["Taban oran tablosu henuz olusturulmamis."])

        # Veri kalitesi ornek sayisina bagli: 60 ornek dusuk, 500+ yuksek guven
        kalite = max(0.3, min(1.0, ornek / 500.0))

        return BotPrediction(
            **ortak,
            decision="goal" if p >= 0.62 else "no_goal",
            probability=round(p, 3),
            confidence="high" if p >= 0.75 else ("medium" if p >= 0.62 else "low"),
            data_quality=round(kalite, 2),
            reasons=[
                f"Gecmis veri ({kirilim} kirilimi, {ornek:,} ornek): "
                f"{minute}. dakikada {gol} gol atilmisken maclarin "
                f"%{100*p:.1f}'inde bir gol daha gelmis."
            ],
            warnings=[] if ornek >= 300 else [f"Ornek sayisi dusuk ({ornek})."],
            sample_size=ornek,
        )
