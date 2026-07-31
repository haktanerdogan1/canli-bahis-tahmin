"""
Uzman botlar - her biri FARKLI bir bilgi ailesine bakar.

TASARIM GEREKCESI:
  Eski yapida 18 botun 12'si birebir ayni formulu kullaniyordu. Bunun sebebi
  kopyala-yapistir ile yeni bot uretilmesiydi: her yeni bot, oncekinin dosyasi
  kopyalanip adi degistirilerek olusturulmustu. Sonucta 18 bot tek bir bota
  esdegerdi ve konsensus bir tiyatrodan ibaretti.

  Bu modul o hatayi YAPISAL olarak imkansiz kilar:
    * Ortak is (BotPrediction kurma, veri kalitesi, eksik veri yonetimi)
      Specialist taban sinifinda TEK yerde durur.
    * Her bot yalnizca kendi score() fonksiyonunu yazar.
    * Her bot NEEDS listesiyle hangi olcumlere ihtiyac duydugunu bildirir;
      o olcumler yoksa DURUSTCE 'insufficient_data' doner, uydurma yapmaz.

  Boylece iki botun ayni olmasi ancak score() fonksiyonlarini bilerek ayni
  yazmakla mumkun olur - kazara olmaz.
"""
from typing import Any, Dict, List, Optional

from app.bots.base_bot import BaseGoalBot
from app.core import features as F
from app.schemas.bot_prediction import BotPrediction

SINYAL_ESIGI = 0.62


class Specialist(BaseGoalBot):
    """Tum uzman botlarin ortak iskeleti."""

    NEEDS: List[str] = []
    TABAN = 0.50

    def score(self, f: Dict[str, Any]) -> Optional[float]:
        raise NotImplementedError

    def why(self, f: Dict[str, Any]) -> List[str]:
        return []

    # --- ortak is: alt siniflar buna dokunmaz ---
    def predict(self, ctx: Dict[str, Any]) -> BotPrediction:
        f = F.extract(ctx)
        minute = f["minute"]
        latest = ctx.get("latest")
        snap_id = latest.get("id") if latest else None
        ortak = dict(
            match_id=str(ctx.get("match_id_db")), bot_name=self.name,
            bot_version=self.version, minute=minute,
            period="H1" if minute <= 45 else "H2", market="total_goals",
            snapshot_id=snap_id,
        )

        kalite = F.veri_kalitesi(f, self.NEEDS)
        if kalite < 1.0:
            eksik = [k for k in self.NEEDS if f.get(k) is None]
            return BotPrediction(
                **ortak, decision="insufficient_data", probability=None,
                confidence="low", data_quality=kalite,
                reasons=[f"{self.__doc__.strip().splitlines()[0]}"],
                warnings=[f"Eksik olcum: {', '.join(eksik)}"],
            )

        p = self.score(f)
        if p is None:
            return BotPrediction(
                **ortak, decision="insufficient_data", probability=None,
                confidence="low", data_quality=0.0,
                reasons=["Bu mac durumu botun uzmanlik alanina girmiyor."],
            )

        p = max(0.05, min(0.93, p))
        return BotPrediction(
            **ortak,
            decision="goal" if p >= SINYAL_ESIGI else "no_goal",
            probability=round(p, 3),
            confidence="high" if p >= 0.75 else ("medium" if p >= SINYAL_ESIGI else "low"),
            data_quality=kalite,
            reasons=self.why(f) or ["-"],
        )


# ─────────────────────────────────────────────────────────────────────────
# TEMPO AILESI - oyunun hacmi ve hizi
# ─────────────────────────────────────────────────────────────────────────

class TempoScannerBot(Specialist):
    """Tempo: 10 dakikaya dusen sut sayisi."""
    NEEDS = ["sut_hizi"]

    def __init__(self):
        super().__init__(name="bot_tempo_scanner", version="v2.0.0")

    def score(self, f):
        # Ortalama bir macta 10 dk'da ~2.5 sut olur. 5+ sut yuksek tempo demektir.
        return 0.38 + min(0.42, f["sut_hizi"] * 0.075)

    def why(self, f):
        return [f"10 dakikada {f['sut_hizi']:.1f} sut (ortalama ~2.5)."]


class AttackVolumeBot(Specialist):
    """Hucum hacmi: tehlikeli atak sayisi."""
    NEEDS = ["tehlikeli_atak", "minute"]

    def __init__(self):
        super().__init__(name="bot_attack_volume", version="v2.0.0")

    def score(self, f):
        if f["tehlikeli_atak"] <= 0:
            return None  # bu veri saglayici tarafindan doldurulmamis
        hiz = f["tehlikeli_atak"] / max(1, f["minute"]) * 10
        return 0.36 + min(0.44, hiz * 0.028)

    def why(self, f):
        return [f"Toplam {f['tehlikeli_atak']} tehlikeli atak."]


# ─────────────────────────────────────────────────────────────────────────
# KALITE AILESI - sansin degil, uretilen pozisyonun kalitesi
# ─────────────────────────────────────────────────────────────────────────

class XGSniperBot(Specialist):
    """xG birikim hizi: uretilen pozisyonlarin gol degeri."""
    NEEDS = ["xg_hizi"]

    def __init__(self):
        super().__init__(name="bot_xg_sniper", version="v2.0.0")

    def score(self, f):
        if f["xg_toplam"] <= 0:
            return None
        # 10 dk'da 0.30 xG normal, 0.60+ cok yuksek
        return 0.35 + min(0.45, f["xg_hizi"] * 0.75)

    def why(self, f):
        return [f"Toplam {f['xg_toplam']:.2f} xG, 10 dakikada {f['xg_hizi']:.2f}."]


class FinishingGapBot(Specialist):
    """Bitiricilik acigi: hak edilen gol ile atilan gol farki."""
    NEEDS = ["xg_gol_farki"]

    def __init__(self):
        super().__init__(name="bot_finishing_gap", version="v2.0.0")

    def score(self, f):
        if f["xg_toplam"] <= 0.2:
            return None
        # Takimlar cok pozisyon uretip atamadiysa gol "gecikmis" demektir.
        # Bu, ortalamaya donus (regression to the mean) beklentisidir.
        return 0.45 + max(-0.15, min(0.35, f["xg_gol_farki"] * 0.28))

    def why(self, f):
        d = f["xg_gol_farki"]
        yorum = "hak edip atamamis" if d > 0.5 else ("fazlasini atmis" if d < -0.5 else "dengeli")
        return [f"xG {f['xg_toplam']:.2f} - gol {f['toplam_gol']} = {d:+.2f} ({yorum})."]


class ShotAccuracyBot(Specialist):
    """Isabet orani: sutlarin kaci kaleyi buluyor."""
    NEEDS = ["isabet_orani", "sot_toplam"]

    def __init__(self):
        super().__init__(name="bot_shot_accuracy", version="v2.0.0")

    def score(self, f):
        if f["sut_toplam"] < 4:
            return None  # cok az sutla isabet orani anlamsiz
        return 0.34 + f["isabet_orani"] * 0.55 + min(0.12, f["sot_toplam"] * 0.012)

    def why(self, f):
        return [f"{f['sut_toplam']} sutun {f['sot_toplam']}'i isabetli (%{100*f['isabet_orani']:.0f})."]


# ─────────────────────────────────────────────────────────────────────────
# DEGISIM AILESI - son dakikalarda ne oldu
# ─────────────────────────────────────────────────────────────────────────

class MomentumSurgeBot(Specialist):
    """Momentum: son 5 dakikadaki baski."""
    NEEDS = ["d5_sot", "d5_korner"]

    def __init__(self):
        super().__init__(name="bot_momentum_surge", version="v2.0.0")

    def score(self, f):
        return 0.40 + min(0.40, f["d5_sot"] * 0.13 + f["d5_korner"] * 0.07)

    def why(self, f):
        return [f"Son 5 dk: {f['d5_sot']} isabetli sut, {f['d5_korner']} korner."]


class AccelerationBot(Specialist):
    """Ivme: oyun hizlaniyor mu, yavasliyor mu."""
    NEEDS = ["ivme"]

    def __init__(self):
        super().__init__(name="bot_acceleration", version="v2.0.0")

    def score(self, f):
        # Mutlak hacme degil, hacmin DEGISIMINE bakar - digerlerinden farki bu.
        return 0.48 + max(-0.20, min(0.32, f["ivme"] * 0.09))

    def why(self, f):
        yon = "hizlaniyor" if f["ivme"] > 0 else ("yavasliyor" if f["ivme"] < 0 else "sabit")
        return [f"Son 5 dk, onceki 5 dk'ya gore {f['ivme']:+d} sut ({yon})."]


class CornerPressureBot(Specialist):
    """Duran top baskisi: korner hizi."""
    NEEDS = ["korner_hizi"]

    def __init__(self):
        super().__init__(name="bot_corner_pressure", version="v2.0.0")

    def score(self, f):
        if f["korner_toplam"] <= 0:
            return None
        return 0.40 + min(0.38, f["korner_hizi"] * 0.15)

    def why(self, f):
        return [f"{f['korner_toplam']} korner, 10 dakikada {f['korner_hizi']:.1f}."]


# ─────────────────────────────────────────────────────────────────────────
# MAC DURUMU AILESI - skor ve sure, istatistikten bagimsiz
# ─────────────────────────────────────────────────────────────────────────

class GameStateBot(Specialist):
    """Mac durumu: skor farki ve kalan sure."""
    NEEDS = ["minute", "skor_farki"]

    def __init__(self):
        super().__init__(name="bot_game_state", version="v2.0.0")

    def score(self, f):
        kalan = max(0, 95 - f["minute"])
        # Beraberlik veya tek farkli skor -> iki taraf da oynamak zorunda
        acik = 0.5 if f["skor_farki"] <= 1 else 0.0
        # Cok farkli skorda oyun soner
        soguk = -0.18 if f["skor_farki"] >= 3 else 0.0
        return 0.30 + (kalan / 95) * 0.30 + acik * 0.30 + soguk

    def why(self, f):
        return [f"{f['minute']}' oynandi, skor farki {f['skor_farki']}, "
                f"{max(0, 95 - f['minute'])} dk kaldi."]


class RedCardBot(Specialist):
    """Kirmizi kart etkisi: sayisal ustunluk oyunu acar."""
    NEEDS = ["kirmizi_kart", "minute"]

    def __init__(self):
        super().__init__(name="bot_red_card", version="v2.0.0")

    def score(self, f):
        if f["kirmizi_kart"] <= 0:
            return None  # kirmizi yoksa bu botun soyleyecegi sey yok
        kalan = max(0, 95 - f["minute"]) / 95
        return 0.50 + min(0.30, f["kirmizi_kart"] * 0.16) * (0.4 + 0.6 * kalan)

    def why(self, f):
        return [f"{f['kirmizi_kart']} kirmizi kart - sayisal ustunluk alani aciyor."]


class DrawBreakerBot(Specialist):
    """Beraberlik kirici: esit skorda son bolum baskisi."""
    NEEDS = ["berabere", "minute"]

    def __init__(self):
        super().__init__(name="bot_draw_breaker", version="v2.0.0")

    def score(self, f):
        if not f["berabere"] or f["minute"] < 55:
            return None  # sadece esit skorlu maclarin son bolumunde konusur
        return 0.46 + min(0.30, (f["minute"] - 55) * 0.011)

    def why(self, f):
        return [f"{f['minute']}' esit skor - iki taraf da galibiyet ariyor."]


# ─────────────────────────────────────────────────────────────────────────
# ZAMAN PENCERESI AILESI
# ─────────────────────────────────────────────────────────────────────────

class EarlyBlitzBot(Specialist):
    """Erken firtina: ilk 25 dakikanin yogunlugu."""
    NEEDS = ["minute", "sut_hizi"]

    def __init__(self):
        super().__init__(name="bot_early_blitz", version="v2.0.0")

    def score(self, f):
        if f["minute"] > 25:
            return None  # yalnizca macin ilk bolumunde konusur
        return 0.40 + min(0.38, f["sut_hizi"] * 0.085)

    def why(self, f):
        return [f"Ilk {f['minute']} dakikada 10 dk basina {f['sut_hizi']:.1f} sut."]


class LateDramaBot(Specialist):
    """Gec drama: 70. dakika sonrasi."""
    NEEDS = ["minute", "sut_hizi", "skor_farki"]

    def __init__(self):
        super().__init__(name="bot_late_drama", version="v2.0.0")

    def score(self, f):
        if f["minute"] < 70:
            return None  # yalnizca macin son bolumunde konusur
        acik = 0.14 if f["skor_farki"] <= 1 else -0.10
        return 0.44 + min(0.26, f["sut_hizi"] * 0.05) + acik

    def why(self, f):
        return [f"{f['minute']}' - son bolum, skor farki {f['skor_farki']}."]


# ─────────────────────────────────────────────────────────────────────────
# HAKIMIYET AILESI
# ─────────────────────────────────────────────────────────────────────────

class PossessionDominanceBot(Specialist):
    """Hakimiyet: topla oynama ve sut ustunlugu dengesizligi."""
    NEEDS = ["topla_oynama_farki", "sut_ustunlugu"]

    def __init__(self):
        super().__init__(name="bot_possession_dominance", version="v2.0.0")

    def score(self, f):
        # Tek tarafli baski gol getirir; ama asiri hakimiyet tikanma da olabilir,
        # o yuzden etki sinirli tutuluyor.
        return 0.44 + min(0.20, f["topla_oynama_farki"] * 0.006) \
                    + min(0.18, f["sut_ustunlugu"] * 0.022)

    def why(self, f):
        return [f"Topla oynama farki %{f['topla_oynama_farki']:.0f}, "
                f"sut ustunlugu {f['sut_ustunlugu']}."]


# ─────────────────────────────────────────────────────────────────────────
# MAC ONCESI AILESI - canli veri gerektirmez
# ─────────────────────────────────────────────────────────────────────────

class FormAsymmetryBot(Specialist):
    """Form asimetrisi: iki takimin gol profili uyumu."""
    NEEDS = ["pre_beklenen_gol", "pre_guven"]

    def __init__(self):
        super().__init__(name="bot_form_asymmetry", version="v2.0.0")

    def score(self, f):
        # prematch_prophet ORANLARA bakar; bu bot BEKLENEN GOL SAYISINA bakar.
        # Ayni kaynaktan farkli bir olcum - bilerek ayri tutuldu.
        bg = f["pre_beklenen_gol"]
        ham = 0.30 + min(0.50, max(0.0, (bg - 1.5)) * 0.20)
        return 0.50 + (ham - 0.50) * f["pre_guven"]

    def why(self, f):
        return [f"Iki takimin mac basina ortalama {f['pre_beklenen_gol']:.2f} golu var "
                f"(guven {f['pre_guven']})."]


def tum_uzmanlar():
    """Konsensus motoruna verilecek uzman bot listesi."""
    return [
        TempoScannerBot(), AttackVolumeBot(),
        XGSniperBot(), FinishingGapBot(), ShotAccuracyBot(),
        MomentumSurgeBot(), AccelerationBot(), CornerPressureBot(),
        GameStateBot(), RedCardBot(), DrawBreakerBot(),
        EarlyBlitzBot(), LateDramaBot(),
        PossessionDominanceBot(), FormAsymmetryBot(),
    ]
