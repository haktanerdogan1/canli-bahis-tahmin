#!/usr/bin/env python3
"""Bugunku Iddaa.com maclarini bizim arsivle (odds_profile.py'nin %5'lik ince
dilimleri) canli olarak kiyaslayip yerel bir HTML sayfasi acan arac.

2026-08-25 guncellemesi (kullanici talebi): artik tek market (IY gol/MS 1.5)
degil, sayfa icinde SECILEBILIR 21 market var - MS KG Var, MS 0.5/1.5/2.5/
3.5/4.5 Ust, IY 0.5/1.5/2.5 Ust, IY KG Var, 2.Y KG Var, IY+2.Y KG Var, ve
IY/MS 9'lu kombinasyon (0/1, 1/2 gibi - 0=beraberlik). Her mac icin ONCEDEN
hesaplanmis favori gucu, TUM marketlerin dilim verisiyle birlikte sayfaya
gomuluyor - market degistirmek yeniden veri cekmeden, aninda JS ile oluyor.

ESLESME ETIKETI: TUM arsiv oranlari %5'lik bir BANT icindeki (ornegin
0.65-0.70 favori gucune sahip TUM gecmis maclar) sonuc oranidir - hicbir
zaman birebir/ozdes oran eslesmesi degildir. Bir macin dustugu bantta
secili market icin yeterli ornek varsa "kendi dilimi (guvenilir)", yoksa
komsu banda (±0.05) bakilip bulunursa "komsu dilimden (az ornek)"
yaziliyor, o da yoksa "veri yok" (kullanici raporu 2026-08-25: "birebir"
etiketi yanlis izlenim veriyordu, sanki ayni oranli maclar bulunuyormus
gibi - oysa hep %5'lik bant kullaniliyor).

GECMIS ORNEK MACLAR (kullanici talebi 2026-08-25): bir maca tiklayinca
AYNI dilime dusen gercek gecmis maclar (takim adi + IY/MS skoru) acilir -
secili markete gore her ornegin tuttu/tutmadi durumu isaretlenir.

NASIL CALISIR:
  1) Iddaa.com'un kendi canli API'sinden BUGUN oynanacak, henuz baslamamis
     maclarin 1X2 acilis oranlarini ceker (iddaa_odds_client.py ile AYNI
     kaynak) ve favori gucunu (de-vig) hesaplar.
  2) Railway'deki /api/archive-market-bins'ten (admin secret GEREKMEZ) TUM
     marketlerin %5'lik dilim oranlarini ceker.
  3) Ikisini tek bir HTML sayfasina gomer, tarayicida acar - market secimi
     ve siralama sayfa icinde JS ile aninda calisir.

Bu arac canli-bahis-tahmin'in KENDI arsiv mantigini kullaniyor ama hicbir
sinyale/bota yazmiyor - sadece goruntuleme.

KULLANIM:
    python3 iddaa_karsilastirma.py
"""
import datetime
import json
import os
import tempfile
import webbrowser

import requests

API_BASE = "https://web-production-f1dba.up.railway.app"
IDDAA_EVENTS_URL = "https://sportsbookv2.iddaa.com/sportsbook/events?st=1&type=0&version=0"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"}
TR_TZ = datetime.timezone(datetime.timedelta(hours=3))


def devig_1x2(ev, beraberlik, dep):
    try:
        t = [1.0 / ev, 1.0 / beraberlik, 1.0 / dep]
    except (TypeError, ZeroDivisionError):
        return None
    s = sum(t)
    if s <= 0:
        return None
    return [x / s for x in t]


def fetch_market_bins():
    r = requests.get(f"{API_BASE}/api/archive-market-bins", timeout=20)
    r.raise_for_status()
    return r.json()


def fetch_bin_examples():
    r = requests.get(f"{API_BASE}/api/archive-bin-examples", params={"ornek": 8}, timeout=30)
    r.raise_for_status()
    return r.json().get("dilimler", {})


def _pick_1x2(markets):
    for mk in (markets or []):
        if mk.get("t") == 1 and mk.get("st") == 1:
            o1 = ox = o2 = None
            for o in (mk.get("o") or []):
                n = o.get("n")
                if n == "1":
                    o1 = o.get("odd")
                elif n == "0":
                    ox = o.get("odd")
                elif n == "2":
                    o2 = o.get("odd")
            return o1, ox, o2
    return None, None, None


def fetch_today_matches():
    r = requests.get(IDDAA_EVENTS_URL, headers=HEADERS, timeout=25)
    r.raise_for_status()
    events = ((r.json().get("data") or {}).get("events")) or []

    bugun = datetime.datetime.now(TR_TZ).date()
    out = []
    for e in events:
        if e.get("sid") != 1 or e.get("s") != 0:
            continue
        ts = e.get("d")
        if not ts:
            continue
        kickoff = datetime.datetime.fromtimestamp(ts, tz=TR_TZ)
        if kickoff.date() != bugun:
            continue
        o1, ox, o2 = _pick_1x2(e.get("m") or [])
        if not (o1 and ox and o2):
            continue
        p = devig_1x2(o1, ox, o2)
        if not p:
            continue
        out.append({
            "home": e.get("hn"), "away": e.get("an"),
            "saat": kickoff.strftime("%H:%M"),
            "odd_1": o1, "odd_x": ox, "odd_2": o2,
            "favori": round(max(p[0], p[2]), 4),
        })
    out.sort(key=lambda m: m["saat"])
    return out


PAGE_TEMPLATE = """<!doctype html>
<html lang="tr"><head><meta charset="utf-8">
<title>Iddaa Arsiv Karsilastirma - {tarih}</title>
<style>
  body {{ font-family: -apple-system, sans-serif; background:#111; color:#eee; margin:0; padding:24px; }}
  h1 {{ font-size:20px; margin-bottom:4px; }}
  p.sub {{ color:#999; margin-top:0; }}
  .toolbar {{ display:flex; gap:12px; align-items:center; margin:16px 0; flex-wrap:wrap; }}
  .tabs {{ display:flex; gap:4px; background:#1c1c1c; border:1px solid #444; border-radius:6px; padding:3px; }}
  .tab {{ background:none; border:none; color:#aaa; padding:6px 14px; border-radius:4px; font-size:13px; cursor:pointer; }}
  .tab.active {{ background:#0038ff; color:#fff; }}
  .wizard-bar {{ display:flex; gap:12px; margin:20px 0; flex-wrap:wrap; }}
  .wizard-btn {{ flex:1; min-width:220px; padding:14px 18px; border-radius:10px; border:1px solid #444;
    font-size:15px; font-weight:600; cursor:pointer; color:#fff; text-align:left; }}
  .wizard-btn.guvenli {{ background:linear-gradient(135deg,#0a5c20,#0a7d2c); }}
  .wizard-btn.riskli {{ background:linear-gradient(135deg,#8a5200,#c98a00); }}
  #kuponPanel {{ margin-bottom:8px; }}
  .kupon-box {{ background:#181818; border:1px solid #333; border-radius:10px; padding:18px; margin-bottom:12px; }}
  .kupon-box h3 {{ margin:0 0 10px 0; font-size:16px; }}
  .kupon-ozet {{ display:flex; gap:24px; margin-top:12px; flex-wrap:wrap; }}
  .kupon-ozet .kutu {{ background:#111; border:1px solid #333; border-radius:8px; padding:10px 16px; }}
  .kupon-ozet .kutu .deger {{ font-size:20px; font-weight:700; }}
  .kupon-ozet .kutu .etiket {{ color:#888; font-size:11px; text-transform:uppercase; }}
  select {{ background:#1c1c1c; color:#eee; border:1px solid #444; padding:8px 12px; border-radius:6px; font-size:14px; }}
  table {{ border-collapse: collapse; width:100%; margin-top:8px; }}
  th, td {{ padding:8px 12px; text-align:left; border-bottom:1px solid #333; font-size:14px; }}
  th {{ color:#aaa; font-weight:600; text-transform:uppercase; font-size:11px; letter-spacing:0.05em; cursor:pointer; }}
  tr:hover {{ background:#1a1a1a; }}
  .birebir {{ color:#0a7d2c; font-weight:600; }}
  .toleransli {{ color:#c98a00; font-weight:600; }}
  .veriyok {{ color:#666; }}
  .badge {{ font-size:10px; padding:2px 6px; border-radius:4px; margin-left:6px; }}
  .badge.birebir {{ background:#0a7d2c22; }}
  .badge.toleransli {{ background:#c98a0022; }}
  .not {{ color:#777; font-size:12px; margin-top:20px; }}
  tr.tiklanabilir {{ cursor:pointer; }}
  .modal-overlay {{ position:fixed; inset:0; background:#000a; display:flex; align-items:center; justify-content:center; z-index:10; }}
  .modal {{ background:#181818; border:1px solid #333; border-radius:10px; padding:20px; width:min(720px, 92vw); max-height:80vh; overflow:auto; }}
  .modal-header {{ display:flex; justify-content:space-between; align-items:center; margin-bottom:8px; }}
  .modal-header h2 {{ font-size:16px; margin:0; }}
  .modal-close {{ background:none; border:none; color:#aaa; font-size:20px; cursor:pointer; }}
  .modal p.hint {{ color:#888; font-size:12px; margin-top:0; }}
  .hit {{ color:#0a7d2c; font-weight:600; }}
  .miss {{ color:#8a2020; }}
</style></head>
<body>
  <h1>Iddaa vs Arsiv Karsilastirma</h1>
  <p class="sub">{tarih_okunur} - {mac_sayisi} mac</p>

  <div class="wizard-bar">
    <button class="wizard-btn guvenli" id="btnGuvenli">🛡️ Günün En Güvenli Kuponu</button>
    <button class="wizard-btn riskli" id="btnYuksekOran">🎯 Denenecek Yüksek Oranlı Kupon</button>
  </div>
  <div id="kuponPanel"></div>

  <div class="toolbar">
    <label for="market">Market:</label>
    <select id="market"></select>
    <label for="esik">Eşik:</label>
    <input type="number" id="esik" value="55" min="0" max="100" step="1" style="width:60px;background:#1c1c1c;color:#eee;border:1px solid #444;border-radius:6px;padding:6px 8px;">
    <span style="color:#888;">%</span>
    <div class="tabs">
      <button class="tab active" id="tabEslesen" data-mod="eslesen">Eşik Üstü</button>
      <button class="tab" id="tabTumu" data-mod="tumu">Tüm Maçlar</button>
    </div>
    <span id="ornekBilgi" style="color:#888;font-size:12px;"></span>
  </div>
  <table>
    <thead><tr>
      <th>Saat</th><th>Mac</th><th>1X2 (acilis)</th><th>Favori Gucu</th><th>Arsiv Orani</th><th>Ornek</th><th>Eslesme</th>
    </tr></thead>
    <tbody id="tbody"></tbody>
  </table>
  <p class="not">Favori gucu = 1X2 oranlari marjdan arindirilip (de-vig) favorinin gercek kazanma ihtimali.
  Arsiv oranlari BUTUN durumlarda %5'lik bir BANT icindeki (ornegin 0.65-0.70 arasi favori gucune sahip TUM
  maclar) gecmis maclarin gercek sonuc oranidir - hicbir zaman birebir/ozdes oran eslesmesi degildir.
  "Kendi dilimi (guvenilir)" = macin dustugu bu 5 puanlik bantta tek basina yeterli ornek var. "Komsu dilimden
  (az ornek)" = kendi banti zayifti, ±0.05 uzaktaki komsu banttan oran alindi. Bir satira tiklayinca ayni
  banda dusen gercek gecmis maclari gorebilirsin. Bu sayfa hicbir sinyale/bota baglanmiyor, sadece bilgi
  amaclidir.</p>

  <div id="modalOverlay" class="modal-overlay" style="display:none;">
    <div class="modal">
      <div class="modal-header">
        <h2 id="modalTitle"></h2>
        <button class="modal-close" id="modalClose">×</button>
      </div>
      <p class="hint" id="modalHint"></p>
      <table>
        <thead><tr><th>Tarih</th><th>Mac</th><th>Lig</th><th>İY (sonuç)</th><th>MS (sonuç)</th><th>Seçili markette</th></tr></thead>
        <tbody id="modalBody"></tbody>
      </table>
    </div>
  </div>

<script>
const marketLabels = {market_labels_json};
const bins = {bins_json};
const matches = {matches_json};
const binExamples = {bin_examples_json};

function binKeyFor(favori) {{
  let lo = Math.min(Math.floor(favori / 0.05) * 0.05, 0.95);
  return lo.toFixed(2) + '-' + (lo + 0.05).toFixed(2);
}}

function lookup(market, favori) {{
  const marketBins = bins[market] || {{}};
  const exactKey = binKeyFor(favori);
  if (marketBins[exactKey]) {{
    return {{...marketBins[exactKey], eslesme: 'birebir', dilim: exactKey}};
  }}
  const idx = Math.floor(favori / 0.05);
  for (const d of [-1, 1]) {{
    const nIdx = Math.max(0, Math.min(idx + d, 19));
    const lo = nIdx * 0.05;
    const key = lo.toFixed(2) + '-' + (lo + 0.05).toFixed(2);
    if (marketBins[key]) {{
      return {{...marketBins[key], eslesme: 'toleransli', dilim: key}};
    }}
  }}
  return null;
}}

function sonuc1x2(h, a) {{
  if (h > a) return '1';
  if (h === a) return '0';
  return '2';
}}

function marketOutcomes(hs, aws, fhh, fha) {{
  const out = {{
    ms_1: hs > aws ? 1 : 0,
    ms_x: hs === aws ? 1 : 0,
    ms_2: hs < aws ? 1 : 0,
    ms_kg: (hs > 0 && aws > 0) ? 1 : 0,
    ms_over_05: (hs + aws) > 0.5 ? 1 : 0,
    ms_over_15: (hs + aws) > 1.5 ? 1 : 0,
    ms_over_25: (hs + aws) > 2.5 ? 1 : 0,
    ms_over_35: (hs + aws) > 3.5 ? 1 : 0,
    ms_over_45: (hs + aws) > 4.5 ? 1 : 0,
  }};
  if (fhh === null || fha === null || fhh === undefined || fha === undefined) return out;
  const ikinciH = hs - fhh, ikinciA = aws - fha;
  out.iy_kg = (fhh > 0 && fha > 0) ? 1 : 0;
  out.iy2_kg = (ikinciH > 0 && ikinciA > 0) ? 1 : 0;
  out.iy_ve_iy2_kg = (fhh > 0 && fha > 0 && ikinciH > 0 && ikinciA > 0) ? 1 : 0;
  out.iy_over_05 = (fhh + fha) > 0.5 ? 1 : 0;
  out.iy_over_15 = (fhh + fha) > 1.5 ? 1 : 0;
  out.iy_over_25 = (fhh + fha) > 2.5 ? 1 : 0;
  const combo = 'iyms_' + sonuc1x2(fhh, fha) + sonuc1x2(hs, aws);
  Object.keys(marketLabels).forEach(k => {{
    if (k.startsWith('iyms_')) out[k] = (k === combo) ? 1 : 0;
  }});
  return out;
}}

const SONUC_ADI = {{'1': 'Ev Kazandı', '0': 'Beraberlik', '2': 'Deplasman Kazandı'}};

function openModal(dilim, macAdi) {{
  const market = document.getElementById('market').value;
  let ornekler = (binExamples[dilim] || []).map(o => {{
    const sonuc = marketOutcomes(o.ms_h, o.ms_a, o.iy_h, o.iy_a);
    return {{...o, tutti: sonuc[market]}};
  }});
  // Tutanlar (1) en ustte, sonra bilinmeyen (undefined), sonra tutmayanlar (0)
  ornekler.sort((a, b) => {{
    const va = a.tutti === 1 ? 2 : (a.tutti === 0 ? 0 : 1);
    const vb = b.tutti === 1 ? 2 : (b.tutti === 0 ? 0 : 1);
    return vb - va;
  }});
  document.getElementById('modalTitle').textContent = `${{macAdi}} — favori dilimi ${{dilim}}`;
  document.getElementById('modalHint').textContent =
    `Ayni dilime dusen ${{ornekler.length}} gercek gecmis mac, "${{marketLabels[market]}}" marketine gore (tutanlar en ustte):`;
  document.getElementById('modalBody').innerHTML = ornekler.map(o => {{
    let sonucTxt = '<span class="veriyok">bilinmiyor</span>';
    if (o.tutti !== undefined) {{
      sonucTxt = o.tutti ? '<span class="hit">✓ tuttu</span>' : '<span class="miss">✗ tutmadi</span>';
    }}
    let iyTxt = '-';
    if (o.iy_h !== null && o.iy_h !== undefined) {{
      const iyKod = sonuc1x2(o.iy_h, o.iy_a);
      iyTxt = `${{o.iy_h}}-${{o.iy_a}} <span style="color:#888">(${{SONUC_ADI[iyKod]}})</span>`;
    }}
    const msKod = sonuc1x2(o.ms_h, o.ms_a);
    const msTxt = `${{o.ms_h}}-${{o.ms_a}} <span style="color:#888">(${{SONUC_ADI[msKod]}})</span>`;
    return `<tr>
      <td>${{(o.tarih || '').slice(0, 10)}}</td>
      <td>${{o.ev_sahibi}} - ${{o.deplasman}}</td>
      <td>${{o.lig || '-'}}</td>
      <td>${{iyTxt}}</td>
      <td>${{msTxt}}</td>
      <td>${{sonucTxt}}</td>
    </tr>`;
  }}).join('') || '<tr><td colspan="6" class="veriyok">Bu dilim icin ornek yok.</td></tr>';
  document.getElementById('modalOverlay').style.display = 'flex';
}}

function closeModal() {{
  document.getElementById('modalOverlay').style.display = 'none';
}}

// Kupon sihirbazlari (kullanici talebi 2026-08-25): "gunun en guvenli
// kuponu" ve "yuksek oran denenecek kupon". ONEMLI DURUSTLUK NOTU: burada
// gosterilen "kombine oran" GERCEK Iddaa odeme orani DEGIL - sadece
// arsiv olasiliklarinin (bagimsizlik varsayimiyla) carpimindan NAIF
// turetilmis bir tahmin (marj/korelasyon yok). Gercek market bazli Iddaa
// oranlarini (sadece 1X2'yi degil) cekmiyoruz - bu yuzden "kesin oran"
// diye sunulmuyor, sadece fikir/siralama amacli.
const KUPON_MIN_ORNEK = 300;  // az orneke dayali "guvenli" secim olmasin
// SADECE "MS 0.5 Ust" (90 dakikada en az 1 gol) neredeyse her zaman tutuyor
// (%95+) - gercek bahiste odemesi de yok denecek kadar dusuk (~1.02),
// kupon sihirbazlarinda anlamli degil. NOT: "IY 0.5 Ust" (ilk yaride en az
// 1 gol, ~%55-70) BUNUN GIBI DEGIL - projenin ana konusu, trivial degil,
// disarida BIRAKILMADI (once yanlislikla ikisi de haric tutulmustu).
const KUPON_HARIC_MARKET = new Set(['ms_over_05']);
// Kullanici geri bildirimi (2026-08-25): "1/1 kombinasyonu riskli, KG Var/
// MS1/IY 0.5 Ust daha banko". IY/MS 9'lu kombinasyonlar main_leagues
// arsivini (46k mac) 9'a boldugu icin AYNI ornek sayisinda (KUPON_MIN_ORNEK
// esigini gecse bile) daha az istikrarli/daha noktasal bir tahmin - kupon
// sihirbazlarindan tamamen cikarildi (ana tabloda/manuel incelemede hala
// secilebilirler, sadece otomatik sihirbaz onerilerinde yok).
function kuponIcinUygunMu(mk) {{
  return !KUPON_HARIC_MARKET.has(mk) && !mk.startsWith('iyms_');
}}

function tumMarketSecenekleri(m) {{
  return Object.keys(marketLabels).filter(kuponIcinUygunMu).map(mk => {{
    const s = lookup(mk, m.favori);
    return (s && s.ornek >= KUPON_MIN_ORNEK) ? {{market: mk, ...s}} : null;
  }}).filter(Boolean);
}}

function enGuvenliKuponUret() {{
  // Kullanici talebi: "cok mac oluyor, KG Var/IY Gol Olur gibi seylerle
  // sentezle, 4-5 maks 6 mac olsun". Ilk deneme (%55-85 bandinda banttaki
  // EN YUKSEGI secmek) hep "MS 1.5 Ust" (~%85) secip 6 bacakta bile 5.00'a
  // ulasamadi (sadece 2.72x) - cunku o kadar yuksek olasilikli secimler
  // odds'a az katki yapiyor. Simdi bant DARALTILDI (%55-72, "1.5 Ust"un
  // tipik %80+ araligini disliyor, KG Var/IY Gol Var'in tipik araligini
  // ICERIYOR) VE banttaki EN DUSUK olasilik (= en cok odds katkisi, hala
  // guvenli) seciliyor - boylece 4-6 bacakla 5.00'a ulasmak cok daha
  // gerceklesir hale geliyor.
  const HEDEF_ORAN = 5.0;
  const MAKS_BACAK = 6;
  const MIN_BACAK = 4;
  const BAND_MIN = 0.55, BAND_MAX = 0.72;
  const adaylar = matches.map(m => {{
    const secenekler = tumMarketSecenekleri(m)
      .filter(s => s.eslesme === 'birebir' && s.oran >= BAND_MIN && s.oran <= BAND_MAX);
    if (!secenekler.length) return null;
    secenekler.sort((a, b) => a.oran - b.oran);  // banttaki en cok odds katkisi yapan (en dusuk)
    return {{...m, secim: secenekler[0]}};
  }}).filter(Boolean);
  adaylar.sort((a, b) => a.secim.oran - b.secim.oran);

  const secilenler = [];
  let kombineOran = 1;
  for (const aday of adaylar) {{
    if (secilenler.length >= MAKS_BACAK) break;
    if (kombineOran >= HEDEF_ORAN && secilenler.length >= MIN_BACAK) break;
    secilenler.push(aday);
    kombineOran *= (1 / aday.secim.oran);
  }}
  return secilenler;
}}

function yuksekOranliKuponUret() {{
  // Dar bant + az bacak (4) - genis bant/cok bacak (5) kombine olasiligi
  // ~%0.5'e kadar dusurup 180x gibi gerceklikten kopuk oranlar veriyordu.
  // Amac "yuksek ama hala akla yatkin denenecek" bir kupon.
  const MIN_OLASILIK = 0.42, MAX_OLASILIK = 0.60;
  const adaylar = matches.map(m => {{
    const secenekler = tumMarketSecenekleri(m).filter(s => s.oran >= MIN_OLASILIK && s.oran <= MAX_OLASILIK);
    if (!secenekler.length) return null;
    secenekler.sort((a, b) => a.oran - b.oran);
    return {{...m, secim: secenekler[0]}};
  }}).filter(Boolean);
  adaylar.sort((a, b) => a.secim.oran - b.secim.oran);
  return adaylar.slice(0, 4);
}}

function renderKupon(legs, baslik, renkSinif) {{
  const panel = document.getElementById('kuponPanel');
  if (!legs.length) {{
    panel.innerHTML = `<div class="kupon-box"><h3>${{baslik}}</h3>
      <p class="veriyok">Bugunku maclarda bu kriterlere uyan yeterli secenek bulunamadi.</p></div>`;
    return;
  }}
  const kombineOlasilik = legs.reduce((acc, l) => acc * l.secim.oran, 1);
  const kombineOran = 1 / kombineOlasilik;
  panel.innerHTML = `<div class="kupon-box">
    <h3>${{baslik}}</h3>
    <table>
      <thead><tr><th>Saat</th><th>Mac</th><th>Market</th><th>Olasilik</th><th>Ornek</th><th>Guven</th></tr></thead>
      <tbody>
        ${{legs.map(l => `<tr>
          <td>${{l.saat}}</td>
          <td>${{l.home}} - ${{l.away}}</td>
          <td>${{marketLabels[l.secim.market]}}</td>
          <td class="${{renkSinif}}">%${{(l.secim.oran * 100).toFixed(1)}}</td>
          <td>${{l.secim.ornek.toLocaleString('tr-TR')}}</td>
          <td>${{l.secim.eslesme === 'birebir' ? 'kendi dilimi' : 'komşu dilimden'}}</td>
        </tr>`).join('')}}
      </tbody>
    </table>
    <div class="kupon-ozet">
      <div class="kutu"><div class="deger">${{legs.length}}</div><div class="etiket">Bacak Sayısı</div></div>
      <div class="kutu"><div class="deger">%${{(kombineOlasilik * 100).toFixed(1)}}</div><div class="etiket">Tahmini Toplam Olasılık</div></div>
      <div class="kutu"><div class="deger">${{kombineOran.toFixed(2)}}</div><div class="etiket">Naif Kombine Oran</div></div>
      <div class="kutu">
        <div class="etiket">Yatırım (₺)</div>
        <input type="number" id="yatirimMiktari" value="100" min="0" step="10"
          style="width:90px;background:#111;color:#eee;border:1px solid #333;border-radius:6px;padding:4px 8px;font-size:16px;">
      </div>
      <div class="kutu"><div class="deger" id="potansiyelKazanc">${{(100 * kombineOran).toFixed(0)}} ₺</div><div class="etiket">Tahmini Getiri</div></div>
    </div>
    <p class="not" style="margin-top:12px;">⚠️ Bu bir Iddaa kuponu DEĞİL - "Naif Kombine Oran" gerçek Iddaa ödeme
    oranlarından değil, arşiv olasılıklarının (bağımsızlık varsayımıyla) çarpımından hesaplandı. Gerçek market
    bazlı Iddaa oranlarını çekmiyoruz, marj/korelasyon içermiyor - gerçek kuponda oran ve getiri FARKLI olacaktır.
    Sadece hangi maçların/marketlerin arşive göre daha güvenli ya da daha yüksek potansiyelli olduğunu
    göstermek için.</p>
  </div>`;

  document.getElementById('yatirimMiktari').addEventListener('input', (e) => {{
    const yatirim = Number(e.target.value) || 0;
    document.getElementById('potansiyelKazanc').textContent = `${{(yatirim * kombineOran).toFixed(0)}} ₺`;
  }});
}}

document.getElementById('btnGuvenli').addEventListener('click', () => {{
  renderKupon(enGuvenliKuponUret(), '🛡️ Günün En Güvenli Kuponu (4-6 maç, kombine oran hedefi 5.00)', 'birebir');
}});
document.getElementById('btnYuksekOran').addEventListener('click', () => {{
  renderKupon(yuksekOranliKuponUret(), '🎯 Denenecek Yüksek Oranlı Kupon (orta olasılıklı, yüksek potansiyel 4 seçim)', 'toleransli');
}});

let filtreMod = 'eslesen';

function render() {{
  const market = document.getElementById('market').value;
  const esik = (Number(document.getElementById('esik').value) || 0) / 100;
  let rows = matches.map(m => {{
    const sonuc = lookup(market, m.favori);
    return {{...m, sonuc}};
  }});
  const toplamSayisi = rows.length;
  const esikUstuSayisi = rows.filter(r => r.sonuc && r.sonuc.oran >= esik).length;
  if (filtreMod === 'eslesen') {{
    rows = rows.filter(r => r.sonuc && r.sonuc.oran >= esik);
  }}
  rows.sort((a, b) => {{
    const oa = a.sonuc ? a.sonuc.oran : -1;
    const ob = b.sonuc ? b.sonuc.oran : -1;
    return ob - oa;
  }});
  document.getElementById('ornekBilgi').textContent =
    filtreMod === 'eslesen'
      ? `${{esikUstuSayisi}} / ${{toplamSayisi}} mac %${{(esik * 100).toFixed(0)}} esigini geciyor`
      : `${{toplamSayisi}} mac gosteriliyor (${{esikUstuSayisi}} tanesi %${{(esik * 100).toFixed(0)}} esigini geciyor)`;
  const tbody = document.getElementById('tbody');
  tbody.innerHTML = rows.map((r, i) => {{
    let oranTxt = '<span class="veriyok">veri yok</span>';
    let ornekTxt = '-';
    let eslesmeTxt = '<span class="veriyok">-</span>';
    if (r.sonuc) {{
      const cls = r.sonuc.eslesme === 'birebir' ? 'birebir' : 'toleransli';
      oranTxt = `<span class="${{cls}}">%${{(r.sonuc.oran * 100).toFixed(1)}}</span>`;
      ornekTxt = r.sonuc.ornek.toLocaleString('tr-TR');
      const etiket = r.sonuc.eslesme === 'birebir' ? 'kendi dilimi (güvenilir)' : 'komşu dilimden (az örnek)';
      eslesmeTxt = `<span class="badge ${{cls}}">${{etiket}}</span>`;
    }}
    const tiklanabilir = r.sonuc ? 'tiklanabilir' : '';
    return `<tr class="${{tiklanabilir}}" data-idx="${{i}}">
      <td>${{r.saat}}</td>
      <td>${{r.home}} - ${{r.away}}</td>
      <td>${{r.odd_1.toFixed(2)}} / ${{r.odd_x.toFixed(2)}} / ${{r.odd_2.toFixed(2)}}</td>
      <td>%${{(r.favori * 100).toFixed(1)}}</td>
      <td>${{oranTxt}}</td>
      <td>${{ornekTxt}}</td>
      <td>${{eslesmeTxt}}</td>
    </tr>`;
  }}).join('');

  tbody.querySelectorAll('tr.tiklanabilir').forEach((tr, i) => {{
    const r = rows[Number(tr.dataset.idx)];
    tr.addEventListener('click', () => openModal(r.sonuc.dilim, `${{r.home}} - ${{r.away}}`));
  }});
}}

document.getElementById('modalClose').addEventListener('click', closeModal);
document.getElementById('modalOverlay').addEventListener('click', (e) => {{
  if (e.target.id === 'modalOverlay') closeModal();
}});

const sel = document.getElementById('market');
Object.entries(marketLabels).forEach(([key, label]) => {{
  const opt = document.createElement('option');
  opt.value = key; opt.textContent = label;
  sel.appendChild(opt);
}});
sel.value = 'iy_over_05';
sel.addEventListener('change', () => {{ closeModal(); render(); }});
document.getElementById('esik').addEventListener('input', () => {{ closeModal(); render(); }});

document.querySelectorAll('.tab').forEach(btn => {{
  btn.addEventListener('click', () => {{
    filtreMod = btn.dataset.mod;
    document.querySelectorAll('.tab').forEach(b => b.classList.toggle('active', b === btn));
    closeModal();
    render();
  }});
}});

render();
</script>
</body></html>"""


def main():
    print("[iddaa_karsilastirma] Arsiv market dilimleri cekiliyor...")
    market_data = fetch_market_bins()
    print(f"[iddaa_karsilastirma] {len(market_data.get('bins', {}))} market yuklendi.")

    print("[iddaa_karsilastirma] Iddaa.com'dan bugunku maclar cekiliyor...")
    matches = fetch_today_matches()
    print(f"[iddaa_karsilastirma] {len(matches)} mac bulundu (bugun, henuz baslamamis).")

    print("[iddaa_karsilastirma] Dilim basina gecmis ornek maclar cekiliyor...")
    bin_examples = fetch_bin_examples()
    print(f"[iddaa_karsilastirma] {sum(len(v) for v in bin_examples.values())} ornek mac yuklendi.")

    now = datetime.datetime.now(TR_TZ)
    html = PAGE_TEMPLATE.format(
        tarih=now.strftime("%d/%m/%Y"),
        tarih_okunur=now.strftime("%d %B %Y, %A"),
        mac_sayisi=len(matches),
        market_labels_json=json.dumps(market_data.get("market_labels", {}), ensure_ascii=False),
        bins_json=json.dumps(market_data.get("bins", {}), ensure_ascii=False),
        matches_json=json.dumps(matches, ensure_ascii=False),
        bin_examples_json=json.dumps(bin_examples, ensure_ascii=False),
    )

    fd, path = tempfile.mkstemp(suffix=".html", prefix="iddaa_karsilastirma_")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[iddaa_karsilastirma] Sayfa yazildi: {path}")
    webbrowser.open(f"file://{path}")


if __name__ == "__main__":
    main()
