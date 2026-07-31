"""
Ortak ozellik (feature) katmani.

NEDEN VAR:
  18 botun 12'si BIREBIR ayni formulu kullaniyordu:
      intensity = (delta_sot * 0.15) + (delta_cor * 0.08)
  Aralarindaki tek fark isme bakan kucuk carpanlardi ve bunlarin bir kismi hic
  calismiyordu (ornegin bot_h2h icindeki 'if "momentum" in self.name' kontrolu,
  botun adi 'bot_5_h2h' oldugu icin asla dogru olmuyordu - olu kod).

  Sonucu veride de gorunuyordu: her sinyalde pozitif bot sayisi 12-14 arasindaydi,
  17 sinyalin 15'inde tam olarak 13. Botlar bagimsiz degildi; 18 botun oyu, tek
  botun oyunun 18 kez sayilmasindan ibaretti. Toplulugun (ensemble) deger uretmesi
  ancak uyeler BAGIMSIZ HATALAR yaptiginda mumkundur.

BU MODUL:
  Ham snapshot'lardan zengin bir ozellik kumesi cikarir. Her bot bu ozelliklerin
  FARKLI bir alt kumesine bakar; boylece gercekten farkli bilgi aileleri olusur.
"""
from typing import Any, Dict, Optional


def _g(snap, key, default=0):
    if not snap:
        return default
    v = snap.get(key, default)
    return default if v is None else v


def extract(ctx: Dict[str, Any]) -> Dict[str, Optional[float]]:
    """Botlarin kullanacagi tum turetilmis olcumleri tek seferde hesaplar."""
    latest = ctx.get("latest")
    s5 = ctx.get("snap_5")
    s10 = ctx.get("snap_10")
    minute = ctx.get("current_minute", 0) or 0
    hs = ctx.get("home_score") or 0
    aws = ctx.get("away_score") or 0

    f: Dict[str, Optional[float]] = {
        "minute": minute,
        "toplam_gol": hs + aws,
        "skor_farki": abs(hs - aws),
        "berabere": 1.0 if hs == aws else 0.0,
        "veri_var": 1.0 if latest else 0.0,
    }

    # --- Mutlak birikimler ---
    sut_h, sut_a = _g(latest, "home_shots"), _g(latest, "away_shots")
    sot_h, sot_a = _g(latest, "home_shots_on_target"), _g(latest, "away_shots_on_target")
    kor_h, kor_a = _g(latest, "home_corners"), _g(latest, "away_corners")
    xg_h, xg_a = _g(latest, "home_xg", 0.0), _g(latest, "away_xg", 0.0)
    pos_h, pos_a = _g(latest, "home_possession"), _g(latest, "away_possession")
    teh_h, teh_a = _g(latest, "home_dangerous_attacks"), _g(latest, "away_dangerous_attacks")
    kirmizi = _g(latest, "home_red_cards") + _g(latest, "away_red_cards")

    f["sut_toplam"] = sut_h + sut_a
    f["sot_toplam"] = sot_h + sot_a
    f["korner_toplam"] = kor_h + kor_a
    f["xg_toplam"] = round(xg_h + xg_a, 3)
    f["tehlikeli_atak"] = teh_h + teh_a
    f["kirmizi_kart"] = kirmizi

    # --- Dakikaya normalize hizlar (tempo ailesi) ---
    dk = max(1, minute)
    f["sut_hizi"] = round(f["sut_toplam"] / dk * 10, 3)        # 10 dk basina sut
    f["xg_hizi"] = round(f["xg_toplam"] / dk * 10, 4)          # 10 dk basina xG
    f["korner_hizi"] = round(f["korner_toplam"] / dk * 10, 3)

    # --- Verimlilik ailesi ---
    f["isabet_orani"] = round(f["sot_toplam"] / f["sut_toplam"], 3) if f["sut_toplam"] else None
    # Gol basina uretilen xG: yuksekse "hak edip atamamis" demektir
    f["xg_gol_farki"] = round(f["xg_toplam"] - f["toplam_gol"], 3)

    # --- Hakimiyet ailesi ---
    f["topla_oynama_farki"] = abs(pos_h - pos_a) if (pos_h or pos_a) else None
    f["sut_ustunlugu"] = abs(sut_h - sut_a)

    # --- Degisim (momentum) ailesi: son 5 ve 10 dk ---
    if latest and s5:
        f["d5_sot"] = (sot_h + sot_a) - (_g(s5, "home_shots_on_target") + _g(s5, "away_shots_on_target"))
        f["d5_sut"] = (sut_h + sut_a) - (_g(s5, "home_shots") + _g(s5, "away_shots"))
        f["d5_korner"] = (kor_h + kor_a) - (_g(s5, "home_corners") + _g(s5, "away_corners"))
        f["d5_xg"] = round((xg_h + xg_a) - (_g(s5, "home_xg", 0.0) + _g(s5, "away_xg", 0.0)), 3)
    else:
        f["d5_sot"] = f["d5_sut"] = f["d5_korner"] = f["d5_xg"] = None

    if latest and s10:
        f["d10_sot"] = (sot_h + sot_a) - (_g(s10, "home_shots_on_target") + _g(s10, "away_shots_on_target"))
        f["d10_sut"] = (sut_h + sut_a) - (_g(s10, "home_shots") + _g(s10, "away_shots"))
    else:
        f["d10_sot"] = f["d10_sut"] = None

    # --- Ivme: oyun HIZLANIYOR mu? (son 5 dk, onceki 5 dk'ya gore) ---
    if f["d5_sut"] is not None and f["d10_sut"] is not None:
        onceki5 = f["d10_sut"] - f["d5_sut"]
        f["ivme"] = f["d5_sut"] - onceki5
    else:
        f["ivme"] = None

    # --- Mac oncesi profil (varsa) ---
    pre = ctx.get("prematch")
    f["pre_iy_oran"] = pre["fh_goal_rate"] if pre else None
    f["pre_ust15_oran"] = pre["over_15_rate"] if pre else None
    f["pre_beklenen_gol"] = pre["beklenen_gol"] if pre else None
    f["pre_guven"] = pre["guven"] if pre else None

    return f


def veri_kalitesi(f: Dict[str, Any], gerekli) -> float:
    """Botun ihtiyac duydugu alanlarin kaci gercekten dolu? 0.0 - 1.0"""
    if not gerekli:
        return 0.0
    dolu = sum(1 for k in gerekli if f.get(k) is not None)
    return round(dolu / len(gerekli), 2)
