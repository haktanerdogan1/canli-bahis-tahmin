"""7msport.com canli veri parse yardimcilari - YEREL makinede
(sevenm_client.py icinde) calisir. Bu dosya DOGRUDAN CALISTIRILMAZ.

NEDEN VAR: Ucuncu bagimsiz canli veri kaynagi (Flashscore + SofaScore'un
yaninda). Flashscore/SofaScore'un aksine bu site HICBIR bot korumasi
kullanmiyor (2026-08-25'te dogrulandi: duz `requests` ile 403/Cloudflare
yok) VE Playwright/tarayici GEREKTIRMIYOR - iki eski-usul JS "dizi dosyasi"
(fen.js: takim/lig, csxl.js: durum/skor/zaman) duz metin olarak cekiliyor.
Bu da Flashscore'dan (Chromium acmak, ~1GB+ bellek) COK daha hafif.

VERI FORMATI: fen.js'de "sDt[macId]=[...]" ve csxl.js'de "sDt2[macId]=[...]"
seklinde JS dizi literalleri var - bunlar zaten gecerli Python literal
sozdizimine denk geldigi icin ast.literal_eval ile guvenle parse ediliyor
(regex ile disina cikmiyoruz). Sutun anlamlari sitenin kendi soccer_f3.aspx
cevabindaki Wr() fonksiyonunun parametre sirasindan cikarildi VE 2026-08-25'te
3 farkli canli mactada (2x Half Time, skor birebir; 1x 2. yari, hesaplanan
dakika Flashscore'un kendi dakikasiyla ~2dk fark - olcum gecikmesi kadar)
Flashscore'un kendi verisiyle CAPRAZ DOGRULANDI.

isstart KODLARI (sDt2[mid][0]):
  1 = ilk yari canli (dakika = difftime'dan hesaplanir, 1-45 arasi kirpilir)
  2 = devre arasi (Half Time)
  3 = ikinci yari canli (dakika = difftime'dan hesaplanir + 45, 46-90 kirpilir)
  8 = uzatma (dakika hesaplanmiyor - "extra time" olarak gecilir, sunucu
      tarafi zaten bu durumu dakikasiz LIVE olarak isliyor)
  4,6,10,12,13,14,15 = bitmis (cesitli bitis nedenleri, hepsi "Finished")
  17 = HENUZ BASLAMAMIS/PLANLANMIS mac (2026-09-09 olcumu: TESHIS-STAGE
       tanisi ile dogrulandi - orchestrator'in canli maclarinin %82'si bu
       kodu tasiyordu, HEPSI skor 0-0 VE difftime alani BOS - "henuz
       baslamadi" disinda baska bir yorumla tutarsiz. fetch_matches() bu
       kodu artik en basta ELER, csxl.js her nedense gunun TUM fikstur
       listesini (baslamamislar dahil) donduruyor gibi gorunuyor.)
  TANIMADIGIMIZ baska bir kod gorulurse TAHMIN EDILMIYOR - oldugu gibi
  gonderiliyor, sunucu (api.py:_fs_parse_stage) bunu "bilinmeyen stage"
  olarak loglar (Flashscore/SofaScore ile ayni kesif ilkesi).

ZAMAN VARSAYIMI: sj/difftime alanlari "YYYY,MM,DD,HH,MI,SS" formatinda ve
Cin saatiyle (+0800/CST) ifade ediliyor gibi gorunuyor - dogrulama sirasinda
UTC+8 varsayimiyla hesaplanan dakika Flashscore'la tutarliydi."""
import re
import ast
from collections import defaultdict
from datetime import datetime, timedelta

FEN_URL = "https://js-live.7mdt.com/datafile/fen.js"
CSXL_URL = "https://js-live.7mdt.com/livedts/csxl.js"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"}

_FINISHED_CODES = {4, 6, 10, 12, 13, 14, 15}
_NOT_STARTED_CODES = {17}


def _parse_js_array(text, varname):
    """'sDt[123]=[...]' / 'sDt2[123]=[...]' satirlarini {id: liste} sozlugune
    cevirir. Deger kismi zaten gecerli Python liste literaline denk geldigi
    icin ast.literal_eval ile GUVENLI parse ediliyor (eval degil)."""
    out = {}
    pattern = re.escape(varname) + r"\[(\d+)\]=(\[.*?\]);"
    for m in re.finditer(pattern, text):
        mid = int(m.group(1))
        try:
            out[mid] = ast.literal_eval(m.group(2))
        except Exception:
            continue
    return out


def _minute_from_difftime(difftime_str, offset_min, lo, hi, _tally=None):
    # GECICI TESHIS (2026-09-09): orchestrator tarafinda canli maclarin
    # %91'i "dakika bilinmiyor" diye elendigi olculdu - _tally, bu fonksiyonun
    # None dondugu her yolu (difftime yok / parse hatasi / negatif fark)
    # sayar, fetch_matches() tek bir ozet satirinda basar. Bulununca SILINECEK.
    if not difftime_str:
        if _tally is not None:
            _tally["no_difftime"] += 1
        return None
    try:
        y, mo, d, h, mi, se = (int(x) for x in difftime_str.split(","))
        period_start = datetime(y, mo, d, h, mi, se)
    except Exception:
        if _tally is not None:
            _tally["parse_error"] += 1
        return None
    now_cst = datetime.utcnow() + timedelta(hours=8)
    elapsed_min = (now_cst - period_start).total_seconds() / 60
    if elapsed_min < 0:
        if _tally is not None:
            _tally["negative_elapsed"] += 1
            _tally.setdefault("negative_elapsed_sample", []).append(round(elapsed_min, 1))
        return None
    if _tally is not None:
        _tally["ok"] += 1
    return max(lo, min(hi, int(elapsed_min) + offset_min))


def _stage_text(isstart, difftime, _tally=None):
    if isstart == 1:
        m = _minute_from_difftime(difftime, 0, 1, 45, _tally)
        return str(m) if m is not None else ""
    if isstart == 2:
        if _tally is not None:
            _tally["half_time"] += 1
        return "Half Time"
    if isstart == 3:
        m = _minute_from_difftime(difftime, 45, 46, 90, _tally)
        return str(m) if m is not None else ""
    if isstart == 8:
        if _tally is not None:
            _tally["extra_time"] += 1
        return "Extra Time"
    if isstart in _FINISHED_CODES:
        if _tally is not None:
            _tally["finished"] += 1
        return "Finished"
    if _tally is not None:
        _tally["unknown_isstart"] += 1
        _tally.setdefault("unknown_isstart_codes", {})
        _tally["unknown_isstart_codes"][isstart] = _tally["unknown_isstart_codes"].get(isstart, 0) + 1
    return f"7m_unknown_isstart_{isstart}"


def _parse_score(bc):
    if not bc or "-" not in bc:
        return 0, 0
    try:
        h, a = bc.split("-", 1)
        return int(h), int(a)
    except Exception:
        return 0, 0


def fetch_matches(session):
    """fen.js (takim/lig) + csxl.js (durum/skor/zaman) cekip mac id'sine gore
    birlestirir. Sadece HER IKI dosyada da bulunan (yani gecerli takim adi
    olan) maclar dondurulur."""
    fen_text = session.get(FEN_URL, headers=HEADERS, timeout=15).text
    csxl_text = session.get(CSXL_URL, headers=HEADERS, timeout=15).text
    sdt = _parse_js_array(fen_text, "sDt")
    sdt2 = _parse_js_array(csxl_text, "sDt2")

    # GECICI TESHIS (2026-09-09) - bkz. _minute_from_difftime/_stage_text.
    _tally = defaultdict(int)

    out = []
    skipped_no_v1 = skipped_no_names = 0
    for mid, v2 in sdt2.items():
        v1 = sdt.get(mid)
        if not v1 or len(v1) < 4 or len(v2) < 7:
            skipped_no_v1 += 1
            continue
        home, away, league = v1[2], v1[3], v1[0]
        if not home or not away:
            skipped_no_names += 1
            continue
        isstart = v2[0]
        bc = v2[6]
        difftime = v2[5]
        score_h, score_a = _parse_score(bc)
        # DUZELTME (2026-09-09): isstart=17 = henuz baslamamis/planlanmis mac
        # (bkz. dosya basindaki isstart KODLARI notu + TESHIS-STAGE olcumu:
        # canli sanilan maclarin %82'si bu kodu tasiyordu, HEPSI 0-0 skor VE
        # BOS difftime - orchestrator bunlari "LIVE ama dakikasiz" diye
        # gereksiz yere degerlendirip eliyordu). csxl.js gunun tum fikstur
        # listesini donduruyor gibi gorunuyor - bu maclari en basta atla,
        # matches tablosuna hic girmesinler.
        if isstart in _NOT_STARTED_CODES:
            _tally["not_started"] += 1
            continue
        # GECICI TESHIS (2026-09-09) devami: 17 disinda baska tanimadigimiz
        # bir kod cikarsa (gercekten "bilinmeyen" kalan durumlar) ayni
        # skor/difftime ornekleme mantigi burada devam ediyor.
        if isstart not in (1, 2, 3, 8) and isstart not in _FINISHED_CODES:
            _tally["unknown_score_00"] += (1 if (score_h == 0 and score_a == 0) else 0)
            _tally.setdefault("unknown_difftime_samples", [])
            if len(_tally["unknown_difftime_samples"]) < 5:
                _tally["unknown_difftime_samples"].append(difftime)
        out.append({
            "mid": str(mid), "home": home, "away": away,
            "league": league or "Unknown League",
            "home_logo": "", "away_logo": "",
            "score_h": score_h, "score_a": score_a,
            "stage": _stage_text(isstart, difftime, _tally),
        })

    print(f"[sevenm_bot] 🔎 TESHIS-STAGE sdt2={len(sdt2)} sdt={len(sdt)} "
          f"eslesen={len(out)} atlanan_v1={skipped_no_v1} atlanan_isim={skipped_no_names} "
          f"not_started={_tally['not_started']} "
          f"| ok={_tally['ok']} half_time={_tally['half_time']} extra_time={_tally['extra_time']} "
          f"finished={_tally['finished']} no_difftime={_tally['no_difftime']} "
          f"parse_error={_tally['parse_error']} negative_elapsed={_tally['negative_elapsed']} "
          f"unknown_isstart={_tally['unknown_isstart']} "
          f"unknown_codes={dict(_tally.get('unknown_isstart_codes', {}))} "
          f"unknown_score_00={_tally['unknown_score_00']} "
          f"unknown_difftime_ornek={_tally.get('unknown_difftime_samples', [])} "
          f"neg_sample={_tally.get('negative_elapsed_sample', [])[:5]}", flush=True)

    return out
