"""Flashscore xG istemcisi - YEREL makinede (terminalde) calistirilir, Railway'de DEGIL.

NEDEN VAR: flashscore_xg_bot.py'yi Railway'in 'web' servisine 4. surec olarak
eklemek denendi - Chromium container'da "Page/Target crashed" verdi ve servisin
toplam bellek kullanimi 1GB limitine dayandi (bkz. git log, "Flashscore xG bot'u
GECICI olarak devre disi birak"). O deneme geri alindi.

BU DOSYA O RISKI TAMAMEN ORTADAN KALDIRIYOR: agir isi (Chromium acmak, sayfa
render etmek) SENIN bilgisayarinda yapiyoruz, Railway'e sadece SONUCU (iki
ondalik sayi) kucuk bir API istegiyle gonderiyoruz. Railway container'i hic
Chromium calistirmiyor - bellek riski sifir.

BEDELI: bu script SADECE SEN CALISTIRDIGIN surece xG verisi akar. Terminali
kapatirsan/bilgisayarini kapatirsan xG zenginlestirmesi durur (digger her sey -
sinyal uretimi, TheSports verisi- calismaya devam eder, sadece xG botlari
tekrar insufficient_data'ya doner).

KULLANIM:
    export BACKUP_SECRET=<Railway'deki BACKUP_SECRET degeri>
    python3 flashscore_xg_client.py
    # farkli bir API adresi icin: python3 flashscore_xg_client.py --api-base https://...
"""
import os
import sys
import time
import argparse

import requests
from playwright.sync_api import sync_playwright

from flashscore_xg_bot import (
    _get_live_matches, _find_match, _scrape_xg, _chromium_executable_path,
    BATCH_SIZE, MIN_MINUTE, MAX_MINUTE, CYCLE_PAUSE_SECONDS,
)

DEFAULT_API_BASE = "https://web-production-f1dba.up.railway.app"

_last_scraped_at = {}


def _hedef_maclari_getir(api_base, secret):
    r = requests.get(
        f"{api_base}/api/admin/xg-targets",
        headers={"x-backup-secret": secret},
        timeout=20,
    )
    r.raise_for_status()
    maclar = r.json().get("maclar", [])
    maclar = [m for m in maclar if MIN_MINUTE <= (m.get("minute") or 0) <= MAX_MINUTE]
    maclar.sort(key=lambda m: _last_scraped_at.get(m["match_id"], 0))
    return maclar[:BATCH_SIZE]


def _xg_gonder(api_base, secret, match_id, home_xg, away_xg):
    r = requests.post(
        f"{api_base}/api/admin/xg-update",
        headers={"x-backup-secret": secret},
        json={"match_id": match_id, "home_xg": home_xg, "away_xg": away_xg},
        timeout=20,
    )
    r.raise_for_status()
    return r.json()


def run_cycle(api_base, secret):
    hedefler = _hedef_maclari_getir(api_base, secret)
    if not hedefler:
        return 0, 0

    islenen = bulunan = 0
    exe_path = _chromium_executable_path()

    with sync_playwright() as p:
        launch_kwargs = {"headless": True, "args": ["--no-sandbox", "--disable-dev-shm-usage"]}
        if exe_path:
            launch_kwargs["executable_path"] = exe_path
        browser = p.chromium.launch(**launch_kwargs)
        page = browser.new_page()

        try:
            live_matches = _get_live_matches(page)
        except Exception as e:
            print(f"⚠️  Flashscore canli liste cekilemedi: {e}", flush=True)
            browser.close()
            return 0, 0

        for m in hedefler:
            match_id, home_team, away_team = m["match_id"], m["home_team"], m["away_team"]
            _last_scraped_at[match_id] = time.time()
            islenen += 1
            if not home_team or not away_team:
                continue

            found, score = _find_match(home_team, away_team, live_matches)
            if not found:
                continue

            try:
                xg = _scrape_xg(page, found["mid"])
            except Exception as e:
                print(f"⚠️  {home_team} - {away_team} xG cekilemedi: {e}", flush=True)
                continue
            if xg is None:
                continue

            home_xg, away_xg = xg
            try:
                _xg_gonder(api_base, secret, match_id, home_xg, away_xg)
            except Exception as e:
                print(f"⚠️  {home_team} - {away_team} API'ye yazilamadi: {e}", flush=True)
                continue

            bulunan += 1
            print(f"✅ xG: {home_team} {home_xg} - {away_xg} {away_team} "
                  f"(eslesme skoru={score:.2f})", flush=True)

        browser.close()

    return islenen, bulunan


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-base", default=DEFAULT_API_BASE)
    args = parser.parse_args()

    secret = os.environ.get("BACKUP_SECRET")
    if not secret:
        print("HATA: BACKUP_SECRET ortam degiskeni tanimli degil. "
              "export BACKUP_SECRET=... calistirip tekrar dene.", flush=True)
        sys.exit(1)

    print(f"🚀 Flashscore xG istemcisi baslatiliyor -> {args.api_base}", flush=True)
    while True:
        start = time.time()
        try:
            islenen, bulunan = run_cycle(args.api_base, secret)
        except Exception as e:
            print(f"⚠️  Dongu hatasi: {e}", flush=True)
            islenen, bulunan = 0, 0
        elapsed = time.time() - start
        print(f"📊 {islenen} mac denendi, {bulunan} xG bulundu ({elapsed:.1f}sn). "
              f"{CYCLE_PAUSE_SECONDS}sn bekleniyor... (durdurmak icin Ctrl+C)", flush=True)
        time.sleep(CYCLE_PAUSE_SECONDS)


if __name__ == "__main__":
    main()
