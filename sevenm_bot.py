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
  TANIMADIGIMIZ baska bir kod gorulurse TAHMIN EDILMIYOR - oldugu gibi
  gonderiliyor, sunucu (api.py:_fs_parse_stage) bunu "bilinmeyen stage"
  olarak loglar (Flashscore/SofaScore ile ayni kesif ilkesi).

ZAMAN VARSAYIMI: sj/difftime alanlari "YYYY,MM,DD,HH,MI,SS" formatinda ve
Cin saatiyle (+0800/CST) ifade ediliyor gibi gorunuyor - dogrulama sirasinda
UTC+8 varsayimiyla hesaplanan dakika Flashscore'la tutarliydi."""
import re
import ast
from datetime import datetime, timedelta

FEN_URL = "https://js-live.7mdt.com/datafile/fen.js"
CSXL_URL = "https://js-live.7mdt.com/livedts/csxl.js"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"}

_FINISHED_CODES = {4, 6, 10, 12, 13, 14, 15}


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


def _minute_from_difftime(difftime_str, offset_min, lo, hi):
    if not difftime_str:
        return None
    try:
        y, mo, d, h, mi, se = (int(x) for x in difftime_str.split(","))
        period_start = datetime(y, mo, d, h, mi, se)
    except Exception:
        return None
    now_cst = datetime.utcnow() + timedelta(hours=8)
    elapsed_min = (now_cst - period_start).total_seconds() / 60
    if elapsed_min < 0:
        return None
    return max(lo, min(hi, int(elapsed_min) + offset_min))


def _stage_text(isstart, difftime):
    if isstart == 1:
        m = _minute_from_difftime(difftime, 0, 1, 45)
        return str(m) if m is not None else ""
    if isstart == 2:
        return "Half Time"
    if isstart == 3:
        m = _minute_from_difftime(difftime, 45, 46, 90)
        return str(m) if m is not None else ""
    if isstart == 8:
        return "Extra Time"
    if isstart in _FINISHED_CODES:
        return "Finished"
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

    out = []
    for mid, v2 in sdt2.items():
        v1 = sdt.get(mid)
        if not v1 or len(v1) < 4 or len(v2) < 7:
            continue
        home, away, league = v1[2], v1[3], v1[0]
        if not home or not away:
            continue
        isstart = v2[0]
        bc = v2[6]
        difftime = v2[5]
        score_h, score_a = _parse_score(bc)
        out.append({
            "mid": str(mid), "home": home, "away": away,
            "league": league or "Unknown League",
            "home_logo": "", "away_logo": "",
            "score_h": score_h, "score_a": score_a,
            "stage": _stage_text(isstart, difftime),
        })
    return out
