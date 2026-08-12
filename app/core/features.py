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


def _g(snap, key, default=None):
    """Ham bir stat alanini okur. VARSAYILAN None: bir kaynagin (ornegin
    TheSports'un henuz esleyemedigimiz sut/korner/xG alanlari) veriyi hic
    doldurmadigi durumla, veriyi doldurup GERCEKTEN 0 yazdigi durum birbirinden
    ayrilmali - aksi halde botlar 'veri yok'u sessizce '0 sut/korner/xG oldu'
    sanip guvenle oy verir (bkz. orchestrator/consensus_engine.py'deki
    'en az 5 bot gercek veriyle oy vermis olmali' kuralinin nasil delindigi -
    proje sohbet gecmisi, 2026-08-05)."""
    if not snap:
        return default
    v = snap.get(key, default)
    return default if v is None else v


def _toplam(a, b):
    """Iki tarafin ayni stat'ini toplar; ikisinden biri bile None ise (veri
    kaynagi bu alani doldurmuyor) sonuc None - sessizce 0 varsayilmaz."""
    return None if (a is None or b is None) else a + b


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

    # --- Mutlak birikimler (kaynak doldurmadiysa None - asagida _toplam ile tasinir) ---
    sut_h, sut_a = _g(latest, "home_shots"), _g(latest, "away_shots")
    sot_h, sot_a = _g(latest, "home_shots_on_target"), _g(latest, "away_shots_on_target")
    kor_h, kor_a = _g(latest, "home_corners"), _g(latest, "away_corners")
    xg_h, xg_a = _g(latest, "home_xg"), _g(latest, "away_xg")
    pos_h, pos_a = _g(latest, "home_possession"), _g(latest, "away_possession")
    teh_h, teh_a = _g(latest, "home_dangerous_attacks"), _g(latest, "away_dangerous_attacks")
    kh, ka = _g(latest, "home_red_cards"), _g(latest, "away_red_cards")

    f["sut_toplam"] = _toplam(sut_h, sut_a)
    f["sot_toplam"] = _toplam(sot_h, sot_a)
    f["korner_toplam"] = _toplam(kor_h, kor_a)
    xg_toplam = _toplam(xg_h, xg_a)
    f["xg_toplam"] = round(xg_toplam, 3) if xg_toplam is not None else None
    f["tehlikeli_atak"] = _toplam(teh_h, teh_a)
    f["kirmizi_kart"] = _toplam(kh, ka)

    # --- Dakikaya normalize hizlar (tempo ailesi) ---
    dk = max(1, minute)
    f["sut_hizi"] = round(f["sut_toplam"] / dk * 10, 3) if f["sut_toplam"] is not None else None
    f["xg_hizi"] = round(f["xg_toplam"] / dk * 10, 4) if f["xg_toplam"] is not None else None
    f["korner_hizi"] = round(f["korner_toplam"] / dk * 10, 3) if f["korner_toplam"] is not None else None

    # --- Verimlilik ailesi ---
    f["isabet_orani"] = (round(f["sot_toplam"] / f["sut_toplam"], 3)
                          if (f["sot_toplam"] is not None and f["sut_toplam"]) else None)
    # Gol basina uretilen xG: yuksekse "hak edip atamamis" demektir
    f["xg_gol_farki"] = round(f["xg_toplam"] - f["toplam_gol"], 3) if f["xg_toplam"] is not None else None

    # --- Hakimiyet ailesi ---
    f["topla_oynama_farki"] = abs(pos_h - pos_a) if (pos_h is not None and pos_a is not None) else None
    f["sut_ustunlugu"] = abs(sut_h - sut_a) if (sut_h is not None and sut_a is not None) else None

    # --- Degisim (momentum) ailesi: son 5 ve 10 dk (iki ucta da None yoksa) ---
    s5_sot = _toplam(_g(s5, "home_shots_on_target"), _g(s5, "away_shots_on_target")) if s5 else None
    s5_sut = _toplam(_g(s5, "home_shots"), _g(s5, "away_shots")) if s5 else None
    s5_kor = _toplam(_g(s5, "home_corners"), _g(s5, "away_corners")) if s5 else None
    s5_xg = _toplam(_g(s5, "home_xg"), _g(s5, "away_xg")) if s5 else None

    f["d5_sot"] = (f["sot_toplam"] - s5_sot) if (f["sot_toplam"] is not None and s5_sot is not None) else None
    f["d5_sut"] = (f["sut_toplam"] - s5_sut) if (f["sut_toplam"] is not None and s5_sut is not None) else None
    f["d5_korner"] = (f["korner_toplam"] - s5_kor) if (f["korner_toplam"] is not None and s5_kor is not None) else None
    f["d5_xg"] = round(f["xg_toplam"] - s5_xg, 3) if (f["xg_toplam"] is not None and s5_xg is not None) else None

    s10_sot = _toplam(_g(s10, "home_shots_on_target"), _g(s10, "away_shots_on_target")) if s10 else None
    s10_sut = _toplam(_g(s10, "home_shots"), _g(s10, "away_shots")) if s10 else None

    f["d10_sot"] = (f["sot_toplam"] - s10_sot) if (f["sot_toplam"] is not None and s10_sot is not None) else None
    f["d10_sut"] = (f["sut_toplam"] - s10_sut) if (f["sut_toplam"] is not None and s10_sut is not None) else None

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
