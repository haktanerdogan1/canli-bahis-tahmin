"""7msport.com canli veri istemcisi - YEREL makinede calisir, Railway'de DEGIL.

Flashscore + SofaScore'un YANINDA calisan UCUNCU bagimsiz kaynak - bkz.
sevenm_bot.py docstring'i (dogrulama, veri formati, isstart kod haritasi).
Flashscore/SofaScore'dan farkli olarak Playwright/Chromium GEREKTIRMIYOR -
duz `requests` ile iki JS dizi dosyasi cekiliyor, bu yuzden cok daha hafif
ve (gozlemlenene gore) bot korumasi da yok. api.py:/api/admin/live-sync
"7m" kaynagiyla yazar - fs_/ss_ maclarina DOKUNMAZ (izole).

KULLANIM:
    export BACKUP_SECRET=<Railway'deki BACKUP_SECRET degeri>
    python3 sevenm_client.py
"""
import os
import sys
import time
import argparse

import requests

from sevenm_bot import fetch_matches

DEFAULT_API_BASE = "https://web-production-f1dba.up.railway.app"
CYCLE_PAUSE_SECONDS = 20


def _live_sync_gonder(session, api_base, secret, matches):
    payload = {"source": "7m", "matches": [
        {"ext_id": m["mid"], "home": m["home"], "away": m["away"], "league": m["league"],
         "home_logo": m["home_logo"], "away_logo": m["away_logo"],
         "score_h": m["score_h"], "score_a": m["score_a"], "stage": m["stage"]}
        for m in matches
    ]}
    r = session.post(
        f"{api_base}/api/admin/live-sync",
        headers={"x-backup-secret": secret}, json=payload, timeout=30,
    )
    r.raise_for_status()
    return r.json()


def run_cycle(session, api_base, secret):
    matches = fetch_matches(session)
    if not matches:
        return 0, 0
    sonuc = _live_sync_gonder(session, api_base, secret, matches)
    return len(matches), sonuc.get("yeni", 0)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-base", default=DEFAULT_API_BASE)
    args = parser.parse_args()

    secret = os.environ.get("BACKUP_SECRET")
    if not secret:
        print("HATA: BACKUP_SECRET ortam degiskeni tanimli degil. "
              "export BACKUP_SECRET=... calistirip tekrar dene.", flush=True)
        sys.exit(1)

    print(f"🚀 7msport canli veri istemcisi baslatiliyor -> {args.api_base} "
          f"({CYCLE_PAUSE_SECONDS}sn'de bir)", flush=True)

    session = requests.Session()
    while True:
        start = time.time()
        try:
            toplam, yeni = run_cycle(session, args.api_base, secret)
        except Exception as e:
            print(f"⚠️  Dongu hatasi: {e}", flush=True)
            toplam, yeni = 0, 0
        elapsed = time.time() - start
        print(f"📊 {toplam} mac islendi ({yeni} yeni) ({elapsed:.1f}sn). "
              f"{CYCLE_PAUSE_SECONDS}sn bekleniyor... (durdurmak icin Ctrl+C)", flush=True)
        time.sleep(CYCLE_PAUSE_SECONDS)


if __name__ == "__main__":
    main()
