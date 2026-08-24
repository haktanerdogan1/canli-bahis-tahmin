"""SofaScore canli veri istemcisi - YEREL makinede calisir, Railway'de DEGIL.

flashscore_xg_client.py'nin YANINDA calisan IKINCI/bagimsiz kaynak - bkz.
sofascore_bot.py docstring'i (mimari, mekanizma, ONEMLI SINIRLAMA notu).
api.py:/api/admin/live-sync artik coklu kaynak destekliyor ("source" alani) -
bu istemci "ss" kaynagiyla yazar, fs_ maclarina DOKUNMAZ (izole, bkz. api.py
_fs_close_stale prefix parametresi).

NEDEN COK SEYREK CALISIYOR (Flashscore'un 15sn'sine karsi 300sn varsayilan):
SofaScore bu tur otomatik istemcileri IP bazli hizla engelliyor (gozlemlenen:
~10-15 istek / birkac dakika icinde 403). Engellenirse COOLDOWN_ON_BLOCK_SECONDS
kadar bekleyip warmup'i tekrar dener - surekli hata basıp CPU/log kirletmez.

KULLANIM:
    export BACKUP_SECRET=<Railway'deki BACKUP_SECRET degeri>
    python3 sofascore_client.py
    # farkli bir API adresi icin: python3 sofascore_client.py --api-base https://...
"""
import os
import sys
import time
import argparse

import requests
from playwright.sync_api import sync_playwright

from sofascore_bot import get_live_summary, scrape_stats, WARMUP_URL

DEFAULT_API_BASE = "https://web-production-f1dba.up.railway.app"
CYCLE_PAUSE_SECONDS = 300
COOLDOWN_ON_BLOCK_SECONDS = 900


def _live_sync_gonder(api_base, secret, live_summary):
    payload = {"source": "ss", "matches": [
        {"ext_id": m["mid"], "home": m["home"], "away": m["away"], "league": m["league"],
         "home_logo": m["home_logo"], "away_logo": m["away_logo"],
         "score_h": m["score_h"], "score_a": m["score_a"], "stage": m["stage"]}
        for m in live_summary
    ]}
    r = requests.post(
        f"{api_base}/api/admin/live-sync",
        headers={"x-backup-secret": secret}, json=payload, timeout=30,
    )
    r.raise_for_status()
    return r.json()


def _stats_gonder(api_base, secret, ext_id, stats):
    r = requests.post(
        f"{api_base}/api/admin/live-stats-update",
        headers={"x-backup-secret": secret},
        json={"source": "ss", "ext_id": ext_id, "stats": stats}, timeout=20,
    )
    r.raise_for_status()
    return r.json()


def run_cycle(api_base, secret, page):
    live_summary = get_live_summary(page)
    if not live_summary:
        return 0, 0, 0

    sonuc = _live_sync_gonder(api_base, secret, live_summary)
    print(f"🔄 [sofascore] live-sync: {sonuc.get('islenen', 0)} mac islendi "
          f"({sonuc.get('yeni', 0)} yeni)", flush=True)
    kabul_edilen = set(sonuc.get("kabul_edilen") or [])

    hedefler = [m for m in live_summary if m["mid"] in kabul_edilen]
    islenen = basarili = 0
    for m in hedefler:
        islenen += 1
        try:
            stats = scrape_stats(page, m["mid"])
        except Exception as e:
            print(f"⚠️  [sofascore] {m['home']} - {m['away']} istatistik cekilemedi: {e}", flush=True)
            continue
        if not stats:
            continue
        try:
            _stats_gonder(api_base, secret, m["mid"], stats)
        except Exception as e:
            print(f"⚠️  [sofascore] {m['home']} - {m['away']} API'ye yazilamadi: {e}", flush=True)
            continue
        basarili += 1
        print(f"✅ [sofascore] {m['home']} - {m['away']}: {stats}", flush=True)

    return len(live_summary), islenen, basarili


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-base", default=DEFAULT_API_BASE)
    args = parser.parse_args()

    secret = os.environ.get("BACKUP_SECRET")
    if not secret:
        print("HATA: BACKUP_SECRET ortam degiskeni tanimli degil. "
              "export BACKUP_SECRET=... calistirip tekrar dene.", flush=True)
        sys.exit(1)

    print(f"🚀 SofaScore canli veri istemcisi baslatiliyor -> {args.api_base} "
          f"({CYCLE_PAUSE_SECONDS}sn'de bir, engellenirse {COOLDOWN_ON_BLOCK_SECONDS}sn bekler)", flush=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = browser.new_page()
        try:
            page.goto(WARMUP_URL, timeout=30000)
            page.wait_for_timeout(2000)
        except Exception as e:
            print(f"⚠️  Ilk yukleme basarisiz: {e}", flush=True)

        while True:
            start = time.time()
            pause = CYCLE_PAUSE_SECONDS
            try:
                canli, islenen, basarili = run_cycle(args.api_base, secret, page)
            except Exception as e:
                msg = str(e)
                canli, islenen, basarili = 0, 0, 0
                if "http_403" in msg or "Forbidden" in msg:
                    print(f"⚠️  SofaScore engellemis gorunuyor, {COOLDOWN_ON_BLOCK_SECONDS}sn bekleniyor.", flush=True)
                    pause = COOLDOWN_ON_BLOCK_SECONDS
                    try:
                        page.goto(WARMUP_URL, timeout=30000)
                    except Exception:
                        pass
                else:
                    print(f"⚠️  Dongu hatasi: {e}", flush=True)
            elapsed = time.time() - start
            print(f"📊 {canli} canli mac, {islenen} istatistik icin denendi, "
                  f"{basarili} basarili ({elapsed:.1f}sn). {pause}sn bekleniyor... "
                  f"(durdurmak icin Ctrl+C)", flush=True)
            time.sleep(pause)

        browser.close()


if __name__ == "__main__":
    main()
