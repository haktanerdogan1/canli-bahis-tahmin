#!/usr/bin/env python3
"""Bugunku Iddaa.com maclarini bizim arsivle (odds_profile.py'nin %5'lik ince
dilimleri) canli olarak kiyaslayip yerel bir HTML sayfasi acan arac.

2026-08-25 guncellemesi (kullanici talebi): artik tek market (IY gol/MS 1.5)
degil, sayfa icinde SECILEBILIR 21 market var - MS KG Var, MS 0.5/1.5/2.5/
3.5/4.5 Ust, IY 0.5/1.5/2.5 Ust, IY KG Var, 2.Y KG Var, IY+2.Y KG Var, ve
IY/MS 9'lu kombinasyon (0/1, 1/2 gibi - 0=beraberlik). Her mac icin ONCEDEN
hesaplanmis favori gucu, TUM marketlerin dilim verisiyle birlikte sayfaya
gomuluyor - market degistirmek yeniden veri cekmeden, aninda JS ile oluyor.

ESLESME ETIKETI: bir macin favori gucu dustugu %5'lik dilimde secili market
icin yeterli ornek varsa "birebir uyusma", yoksa komsu dilime (±0.05
tolerans) bakilip bulunursa "toleranslı" yaziliyor, o da yoksa "veri yok".

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
  <div class="toolbar">
    <label for="market">Market:</label>
    <select id="market"></select>
    <span id="ornekBilgi" style="color:#888;font-size:12px;"></span>
  </div>
  <table>
    <thead><tr>
      <th>Saat</th><th>Mac</th><th>1X2 (acilis)</th><th>Favori Gucu</th><th>Arsiv Orani</th><th>Ornek</th><th>Eslesme</th>
    </tr></thead>
    <tbody id="tbody"></tbody>
  </table>
  <p class="not">Favori gucu = 1X2 oranlari marjdan arindirilip (de-vig) favorinin gercek kazanma ihtimali.
  Arsiv oranlari 33.000+ (bazi marketlerde 100.000+) maclik gecmis arsivde AYNI favori gucune (%5'lik dilim)
  sahip maclarin gercek sonuc oranidir. "Birebir uyuşma" = o dilimde yeterli ornek var. "Toleranslı" = o dilim
  zayifti, komsu (±0.05) dilime bakildi. Bir satira tiklayinca ayni dilime dusen gercek gecmis maclari
  gorebilirsin. Bu sayfa hicbir sinyale/bota baglanmiyor, sadece bilgi amaclidir.</p>

  <div id="modalOverlay" class="modal-overlay" style="display:none;">
    <div class="modal">
      <div class="modal-header">
        <h2 id="modalTitle"></h2>
        <button class="modal-close" id="modalClose">×</button>
      </div>
      <p class="hint" id="modalHint"></p>
      <table>
        <thead><tr><th>Tarih</th><th>Mac</th><th>Lig</th><th>İY</th><th>MS</th><th>Secili markette</th></tr></thead>
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

function openModal(dilim, macAdi) {{
  const market = document.getElementById('market').value;
  const ornekler = binExamples[dilim] || [];
  document.getElementById('modalTitle').textContent = `${{macAdi}} — favori dilimi ${{dilim}}`;
  document.getElementById('modalHint').textContent =
    `Ayni dilime dusen ${{ornekler.length}} gercek gecmis mac, "${{marketLabels[market]}}" marketine gore:`;
  document.getElementById('modalBody').innerHTML = ornekler.map(o => {{
    const sonuc = marketOutcomes(o.ms_h, o.ms_a, o.iy_h, o.iy_a);
    const tutti = sonuc[market];
    let sonucTxt = '<span class="veriyok">bilinmiyor</span>';
    if (tutti !== undefined) {{
      sonucTxt = tutti ? '<span class="hit">✓ tuttu</span>' : '<span class="miss">✗ tutmadi</span>';
    }}
    const iyTxt = (o.iy_h === null || o.iy_h === undefined) ? '-' : `${{o.iy_h}}-${{o.iy_a}}`;
    return `<tr>
      <td>${{(o.tarih || '').slice(0, 10)}}</td>
      <td>${{o.ev_sahibi}} - ${{o.deplasman}}</td>
      <td>${{o.lig || '-'}}</td>
      <td>${{iyTxt}}</td>
      <td>${{o.ms_h}}-${{o.ms_a}}</td>
      <td>${{sonucTxt}}</td>
    </tr>`;
  }}).join('') || '<tr><td colspan="6" class="veriyok">Bu dilim icin ornek yok.</td></tr>';
  document.getElementById('modalOverlay').style.display = 'flex';
}}

function closeModal() {{
  document.getElementById('modalOverlay').style.display = 'none';
}}

function render() {{
  const market = document.getElementById('market').value;
  const rows = matches.map(m => {{
    const sonuc = lookup(market, m.favori);
    return {{...m, sonuc}};
  }});
  rows.sort((a, b) => {{
    const oa = a.sonuc ? a.sonuc.oran : -1;
    const ob = b.sonuc ? b.sonuc.oran : -1;
    return ob - oa;
  }});
  const tbody = document.getElementById('tbody');
  tbody.innerHTML = rows.map((r, i) => {{
    let oranTxt = '<span class="veriyok">veri yok</span>';
    let ornekTxt = '-';
    let eslesmeTxt = '<span class="veriyok">-</span>';
    if (r.sonuc) {{
      const cls = r.sonuc.eslesme === 'birebir' ? 'birebir' : 'toleransli';
      oranTxt = `<span class="${{cls}}">%${{(r.sonuc.oran * 100).toFixed(1)}}</span>`;
      ornekTxt = r.sonuc.ornek.toLocaleString('tr-TR');
      const etiket = r.sonuc.eslesme === 'birebir' ? 'birebir uyuşma' : 'toleranslı';
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
