#!/usr/bin/env python3
"""Bugunku Iddaa.com maclarini bizim 33k+ maclik arsivle (odds_profile.py'nin
%5'lik ince dilimleri) canli olarak kiyaslayip yerel bir HTML sayfasi acan
arac (kullanici talebi 2026-08-25: "eldeki idda verilerini bugun icin idda.com
maclariyla karsilastir, bana bunu yapan bir local ac").

NASIL CALISIR:
  1) Iddaa.com'un kendi canli API'sinden (sportsbookv2.iddaa.com) BUGUN
     (Turkiye takvim gunu) oynanacak, henuz baslamamis futbol maclarinin
     1X2 acilis oranlarini ceker (iddaa_odds_client.py ile AYNI kaynak).
  2) Railway'deki /api/odds-profile-fine-bins'ten (admin secret GEREKMEZ,
     sadece aggregate arsiv istatistigi) guncel %5'lik dilim oranlarini ceker.
  3) Her mac icin oranlari marjdan arindirip (de-vig) favori gucunu bulur,
     ayni dilime dusen arsiv IY gol / MS 1.5 ust oranini eslestirir.
  4) Sonucu IY gol olasiligina gore siralayip tek sayfalik bir HTML olarak
     yazar ve tarayicida acar.

Bu arac canli-bahis-tahmin'in KENDI arsiv mantigini (odds_profile.py)
kullaniyor ama hicbir sinyal/bot'a yazmiyor - sadece goruntuleme.

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

FINE_BIN_SIZE = 0.05


def devig_1x2(ev, beraberlik, dep):
    try:
        t = [1.0 / ev, 1.0 / beraberlik, 1.0 / dep]
    except (TypeError, ZeroDivisionError):
        return None
    s = sum(t)
    if s <= 0:
        return None
    return [x / s for x in t]


def fine_bin(favori):
    lo = int(favori / FINE_BIN_SIZE) * FINE_BIN_SIZE
    lo = min(lo, 0.95)
    return f"{lo:.2f}-{lo + FINE_BIN_SIZE:.2f}"


def fetch_fine_bins():
    r = requests.get(f"{API_BASE}/api/odds-profile-fine-bins", timeout=20)
    r.raise_for_status()
    data = r.json()
    return {d["bin"]: d for d in data.get("dilimler", [])}


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
        out.append({
            "home": e.get("hn"), "away": e.get("an"),
            "kickoff": kickoff, "odd_1": o1, "odd_x": ox, "odd_2": o2,
        })
    return out


def build_html(rows):
    def esc(s):
        return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    body_rows = []
    for r in rows:
        renk = "#0a7d2c" if r["iy_orani"] and r["iy_orani"] >= 0.65 else ("#8a6d00" if r["iy_orani"] and r["iy_orani"] >= 0.55 else "#555")
        iy_txt = f'%{r["iy_orani"]*100:.1f}' if r["iy_orani"] is not None else "veri yok"
        ms_txt = f'%{r["ms15_orani"]*100:.1f}' if r["ms15_orani"] is not None else "veri yok"
        ornek_txt = r["ornek"] if r["ornek"] is not None else "-"
        body_rows.append(f"""
        <tr>
          <td>{r['kickoff'].strftime('%H:%M')}</td>
          <td>{esc(r['home'])} - {esc(r['away'])}</td>
          <td>{r['odd_1']:.2f} / {r['odd_x']:.2f} / {r['odd_2']:.2f}</td>
          <td>%{r['favori']*100:.1f}</td>
          <td style="color:{renk};font-weight:600">{iy_txt}</td>
          <td>{ms_txt}</td>
          <td>{ornek_txt}</td>
        </tr>""")

    return f"""<!doctype html>
<html lang="tr"><head><meta charset="utf-8">
<title>Iddaa Arsiv Karsilastirma - {datetime.datetime.now(TR_TZ).strftime('%d/%m/%Y')}</title>
<style>
  body {{ font-family: -apple-system, sans-serif; background:#111; color:#eee; margin:0; padding:24px; }}
  h1 {{ font-size:20px; margin-bottom:4px; }}
  p.sub {{ color:#999; margin-top:0; }}
  table {{ border-collapse: collapse; width:100%; margin-top:16px; }}
  th, td {{ padding:8px 12px; text-align:left; border-bottom:1px solid #333; font-size:14px; }}
  th {{ color:#aaa; font-weight:600; text-transform:uppercase; font-size:11px; letter-spacing:0.05em; }}
  tr:hover {{ background:#1a1a1a; }}
  .not {{ color:#777; font-size:12px; margin-top:20px; }}
</style></head>
<body>
  <h1>Iddaa vs Arsiv Karsilastirma</h1>
  <p class="sub">{datetime.datetime.now(TR_TZ).strftime('%d %B %Y, %A')} - IY gol olasiligina gore siralandi ({len(rows)} mac)</p>
  <table>
    <tr><th>Saat</th><th>Mac</th><th>1X2 (acilis)</th><th>Favori Gucu</th><th>Arsiv: IY Gol</th><th>Arsiv: MS 1.5 Ust</th><th>Ornek</th></tr>
    {''.join(body_rows)}
  </table>
  <p class="not">Favori gucu = oranlar marjdan arindirilip (de-vig) favorinin gercek kazanma ihtimali. "Arsiv" kolonlari
  33.000+ maclik gecmis Iddaa arsivinde AYNI favori gucune (%5'lik dilim) sahip maclarin gercek sonuc oranidir
  (odds_profile.py / bkz. canli-bahis-tahmin projesi). Bu sayfa hicbir sinyale/bota baglanmiyor, sadece bilgi amaclidir.</p>
</body></html>"""


def main():
    print("[iddaa_karsilastirma] Arsiv dilim oranlari cekiliyor...")
    bins = fetch_fine_bins()
    print(f"[iddaa_karsilastirma] {len(bins)} dilim yuklendi.")

    print("[iddaa_karsilastirma] Iddaa.com'dan bugunku maclar cekiliyor...")
    matches = fetch_today_matches()
    print(f"[iddaa_karsilastirma] {len(matches)} mac bulundu (bugun, henuz baslamamis).")

    rows = []
    for m in matches:
        p = devig_1x2(m["odd_1"], m["odd_x"], m["odd_2"])
        if not p:
            continue
        favori = max(p[0], p[2])
        b = bins.get(fine_bin(favori))
        rows.append({
            **m, "favori": favori,
            "iy_orani": b["iy_gol_orani"] if b else None,
            "ms15_orani": b["ms15_orani"] if b else None,
            "ornek": b["ornek"] if b else None,
        })

    rows.sort(key=lambda r: (r["iy_orani"] is None, -(r["iy_orani"] or 0)))

    html = build_html(rows)
    fd, path = tempfile.mkstemp(suffix=".html", prefix="iddaa_karsilastirma_")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[iddaa_karsilastirma] Sayfa yazildi: {path}")
    webbrowser.open(f"file://{path}")


if __name__ == "__main__":
    main()
