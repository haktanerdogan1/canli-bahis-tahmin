"""
bot_odds_profile — Oran Profili botu.

ISI:
  O an oynanan macin CANLI 1X2 oranini alir, marjdan arindirir ve arsivdeki
  33.525 mac icinde AYNI DENGE PROFILINE sahip maclarin tarihsel sonucuna bakar.

  "Bu maca bahisci su fiyati veriyor. Gecmiste ayni fiyat verilen maclarin
   yuzde kacinda ilk yari golu geldi?"

NEDEN DIGERLERINDEN FARKLI:
  * bot_base_rate      -> sadece dakika+skora bakar (macin kim oldugunu bilmez)
  * prematch_prophet   -> takim istatistiklerine bakar (zayif sinyal, r=0.387)
  * BU BOT             -> piyasanin fiyatina bakar (bahiscinin TUM bilgisinin ozeti)

  Oran; form, sakatlik, motivasyon, kadro gibi bizim hic goremedigimiz
  bilgileri tek sayida tasir. Bu yuzden bagimsiz bir bilgi ailesidir.

OUT-OF-SAMPLE DOGRULANDI:
  Veri %60/%40 egitim-test ayrildi. Bantlar arasi fark test setinde AYNEN
  tekrarlandi (egitim %62.0/%55.8 -> test %63.1/%55.0). Takim profillerinin
  aksine cokmedi, bu yuzden kucultme (shrinkage) uygulanmiyor.

DURUSTLUK:
  Canli oran kaydedilmemisse veya band bulunamazsa 'insufficient_data' doner.
"""
from typing import Any, Dict

import odds_profile
from app.bots.base_bot import BaseGoalBot
from app.schemas.bot_prediction import BotPrediction


class OddsProfileBot(BaseGoalBot):
    def __init__(self):
        super().__init__(name="bot_odds_profile", version="v1.0.0")

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

        def bos(sebep):
            return BotPrediction(**ortak, decision="insufficient_data", probability=None,
                                 confidence="low", data_quality=0.0, reasons=[sebep])

        try:
            sonuc = odds_profile.profil_orani(ctx.get("match_id_db"))
        except Exception as e:
            return bos(f"Oran profili okunamadi: {e}")

        if not sonuc:
            return bos("Bu mac icin canli oran kaydi yok - profil eslesmesi yapilamadi.")

        band, ornek, iy_oran, ms_oran = sonuc

        # Ilk yarida ve henuz golsuzse "ilk yari golu", aksi halde "mac sonu" sorusu
        if minute <= 45 and gol == 0:
            p = iy_oran
            hedef = "ilk yari golu"
            # Dakika ilerledikce ilk yari icin kalan sure azalir
            kalan = max(0.0, (45 - minute) / 45.0)
            p = p * (0.35 + 0.65 * kalan)
        else:
            p = ms_oran
            hedef = "mac sonu 1.5 ust"

        p = max(0.05, min(0.92, p))
        kalite = min(1.0, ornek / 2000.0)

        band_adi = {"cok_dengesiz": "çok dengesiz (net favori)",
                    "dengesiz": "dengesiz",
                    "dengeli": "dengeli"}.get(band, band)

        return BotPrediction(
            **ortak,
            decision="goal" if p >= 0.62 else "no_goal",
            probability=round(p, 3),
            confidence="high" if p >= 0.75 else ("medium" if p >= 0.62 else "low"),
            data_quality=round(max(0.3, kalite), 2),
            reasons=[
                f"Piyasa bu maci '{band_adi}' fiyatliyor.",
                f"Arsivde ayni profildeki {ornek:,} macin %{100*iy_oran:.1f}'inde "
                f"ilk yari golu gelmis ({hedef} beklentisi %{100*p:.0f}).",
            ],
            sample_size=ornek,
        )
