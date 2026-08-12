"""Flashscore canli veri istemcisi - YEREL makinede (terminalde) calisir, Railway'de DEGIL.

TARIHCE: Once sadece xG zenginlestirmesi icin yazildi (flashscore_xg_bot.py'yi
Railway'in 'web' servisine 4. surec olarak eklemek denendi - Chromium
container'da "Page/Target crashed" verdi ve servisin toplam bellek kullanimi
1GB limitine dayandi, bkz. git log). 2026-08-12'de TheSports hesabi "yetkisiz"
hatasi vermeye basladi ve TUM canli veri kaynagi durdu (kod tarafinda hicbir
sey degismedi - abonelik/plan sorunu). Bu yuzden bu istemci TheSports'un
YERINI TAMAMEN ALACAK sekilde genisletildi: artik sadece xG degil, TUM canli
mac senkronunu (skor/dakika/durum + detayli istatistikler) Flashscore'dan
cekip Railway'e yaziyor.

MIMARI: agir isi (Chromium acmak, sayfa render etmek) SENIN bilgisayarinda
yapiyoruz, Railway'e sadece SONUCU kucuk API istekleriyle gonderiyoruz.
Railway container'i hic Chromium calistirmiyor - bellek riski sifir.

IKI KATMANLI DONGU (her CYCLE_PAUSE_SECONDS'ta bir):
  1) TUM canli maclarin skor/dakika/lig ozeti TEK sayfa yuklemesiyle cekilir
     ve /api/admin/live-sync'e gonderilir (ucuz, kapsamli, her dongude).
  2) Kucuk bir grup (BATCH_SIZE) macin DETAYLI istatistik sayfasi ziyaret
     edilir (~15-25sn/mac, pahali) ve /api/admin/live-stats-update'e
     gonderilir - en uzun suredir taranmayan mac once islenir, boylece
     zamanla TUM canli maclar kapsanir.

BEDELI: bu script SADECE SEN CALISTIRDIGIN surece veri akar. Terminali
kapatirsan/bilgisayarini kapatirsan TUM canli veri (skor/dakika dahil) durur -
TheSports'un aksine burada "en azindan skor akar, sadece xG durur" diye bir
ara durum YOK, artik Flashscore skorun da tek kaynagi.

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
    get_live_summary, _find_match, _scrape_stats, _chromium_executable_path,
    BATCH_SIZE, MIN_MINUTE, MAX_MINUTE, CYCLE_PAUSE_SECONDS,
)

DEFAULT_API_BASE = "https://web-production-f1dba.up.railway.app"

_last_scraped_at = {}


def _stage_scrapeable(stage):
    """Istatistik sayfasini ziyaret etmeye deger mi? Mac cok yeni basladiysa
    (ilk birkac dakika istatistik neredeyse hep 0/bos, bkz. MIN_MINUTE) ya da
    90+ dakika gecmisse atla - ayni MIN_MINUTE/MAX_MINUTE penceresi thesports_bot
    doneminde de kullaniliyordu."""
    st = (stage or "").strip()
    if st.isdigit():
        n = int(st)
        return MIN_MINUTE <= n <= MAX_MINUTE
    return st.lower() in ("half time", "ht")


def _live_sync_gonder(api_base, secret, live_summary):
    payload = {"matches": [
        {"fs_id": m["mid"], "home": m["home"], "away": m["away"], "league": m["league"],
         "home_logo": m["home_logo"], "away_logo": m["away_logo"],
         "score_h": m["score_h"], "score_a": m["score_a"], "stage": m["stage"]}
        for m in live_summary
    ]}
    r = requests.post(
        f"{api_base}/api/admin/live-sync",
        headers={"x-backup-secret": secret},
        json=payload, timeout=30,
    )
    r.raise_for_status()
    return r.json()


def _stats_gonder(api_base, secret, fs_id, stats):
    r = requests.post(
        f"{api_base}/api/admin/live-stats-update",
        headers={"x-backup-secret": secret},
        json={"fs_id": fs_id, "stats": stats}, timeout=20,
    )
    r.raise_for_status()
    return r.json()


def run_cycle(api_base, secret):
    exe_path = _chromium_executable_path()

    with sync_playwright() as p:
        launch_kwargs = {"headless": True, "args": ["--no-sandbox", "--disable-dev-shm-usage"]}
        if exe_path:
            launch_kwargs["executable_path"] = exe_path
        browser = p.chromium.launch(**launch_kwargs)
        page = browser.new_page()

        try:
            live_summary = get_live_summary(page)
        except Exception as e:
            print(f"⚠️  Flashscore canli liste cekilemedi: {e}", flush=True)
            browser.close()
            return 0, 0

        kabul_edilen = None
        try:
            sonuc = _live_sync_gonder(api_base, secret, live_summary)
            print(f"🔄 live-sync: {sonuc.get('islenen', 0)} mac islendi "
                  f"({sonuc.get('yeni', 0)} yeni)", flush=True)
            kabul_edilen = set(sonuc.get("kabul_edilen") or [])
        except Exception as e:
            print(f"⚠️  live-sync gonderilemedi: {e}", flush=True)

        # SADECE sunucunun kabul ettigi (is_known_match'ten gecen) maclar icin
        # istatistik sayfasi ziyaret et - aksi halde batch slotlari filtrelenmis
        # (obscure) maclara bosuna harcanir (404 alinir).
        havuz = live_summary if kabul_edilen is None else [m for m in live_summary if m["mid"] in kabul_edilen]
        hedefler = [m for m in havuz if _stage_scrapeable(m["stage"])]
        hedefler.sort(key=lambda m: _last_scraped_at.get(m["mid"], 0))
        hedefler = hedefler[:BATCH_SIZE]

        islenen = bulunan = 0
        for m in hedefler:
            _last_scraped_at[m["mid"]] = time.time()
            islenen += 1
            try:
                stats = _scrape_stats(page, m["mid"])
            except Exception as e:
                print(f"⚠️  {m['home']} - {m['away']} istatistik cekilemedi: {e}", flush=True)
                continue
            if not stats:
                continue
            try:
                _stats_gonder(api_base, secret, m["mid"], stats)
            except Exception as e:
                print(f"⚠️  {m['home']} - {m['away']} API'ye yazilamadi: {e}", flush=True)
                continue
            bulunan += 1
            print(f"✅ {m['home']} - {m['away']}: {stats}", flush=True)

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

    print(f"🚀 Flashscore canli veri istemcisi baslatiliyor -> {args.api_base}", flush=True)
    while True:
        start = time.time()
        try:
            islenen, bulunan = run_cycle(args.api_base, secret)
        except Exception as e:
            print(f"⚠️  Dongu hatasi: {e}", flush=True)
            islenen, bulunan = 0, 0
        elapsed = time.time() - start
        print(f"📊 {islenen} mac istatistik icin denendi, {bulunan} basarili ({elapsed:.1f}sn). "
              f"{CYCLE_PAUSE_SECONDS}sn bekleniyor... (durdurmak icin Ctrl+C)", flush=True)
        time.sleep(CYCLE_PAUSE_SECONDS)


if __name__ == "__main__":
    main()
