"""SofaScore canli veri istemcisi - YEREL makinede calisir, Railway'de DEGIL.

flashscore_xg_client.py'nin YANINDA calisan IKINCI/bagimsiz kaynak - bkz.
sofascore_bot.py docstring'i (mimari, mekanizma, ONEMLI SINIRLAMA notu).
api.py:/api/admin/live-sync artik coklu kaynak destekliyor ("source" alani) -
bu istemci "ss" kaynagiyla yazar, fs_ maclarina DOKUNMAZ (izole, bkz. api.py
_fs_close_stale prefix parametresi).

NEDEN COK SEYREK CALISIYOR (Flashscore'un 15sn'sine karsi 300sn varsayilan):
SofaScore bu tur otomatik istemcileri IP bazli hizla engelliyor (gozlemlenen:
~10-15 istek / birkac dakika icinde 403). Engellenirse KADEMELI backoff'a girer
(900sn -> 1800 -> 3600 ... -> 4sa, 6 ardisik engelde 6sa devre kesici) ve
warmup'i tekrar dener - surekli hata basıp CPU/log/IP kirletmez. Ilk basarili
dongude backoff sayaci sifirlanir. Ayrintili gerekce main()'deki sabitlerde.

ILK SURUMDEKI HATA (2026-08-24, duzeltildi): bu istemci flashscore_xg_client.py'nin
BATCH_SIZE disiplinini (bir turda SADECE birkac macin istatistigini cekmek)
UYGULAMIYORDU - kabul edilen TUM canli maclarin istatistigini art arda cekiyordu
(gozlemlenen: 76 mac = 76 ardisik istek TEK turda). Bu, engellenmenin asil
sebebi olarak degerlendirildi. Artik Flashscore ile AYNI desen: kucuk bir
BATCH_SIZE + en uzun suredir taranmayan mac once + istekler arasi rastgele
bekleme (insan/organik taramaya benzesin diye, ardisik-hizli-istek deseni
azalsin diye).

KULLANIM:
    export BACKUP_SECRET=<Railway'deki BACKUP_SECRET degeri>
    python3 sofascore_client.py
    # farkli bir API adresi icin: python3 sofascore_client.py --api-base https://...
"""
import os
import sys
import time
import random
import argparse

import requests
from playwright.sync_api import sync_playwright

from sofascore_bot import get_live_summary, scrape_stats, WARMUP_URL

DEFAULT_API_BASE = "https://web-production-f1dba.up.railway.app"
CYCLE_PAUSE_SECONDS = 300
BATCH_SIZE = 5
BETWEEN_REQUEST_DELAY = (1.5, 4.0)  # (min, max) saniye - istekler arasi rastgele bekleme

# Kademeli backoff + devre kesici (2026-08-31). ESKI DAVRANIS: her engelde sabit
# 900sn bekle, tekrar dene - engelliyken de her 900sn'de bir vurdugu icin IP
# surekli sicak kaliyordu (gozlem: /tmp/sofascore_client.log'da 150+ ardisik
# "engellemis gorunuyor"). YENI: ardisik her engelde bekleme KATLANIR; cok uzun
# surerse uzun bir "devre kesici" molasina girilir ki IP - ve kullanicinin kendi
# baglantisi (ban tum IP'yi vuruyor, memory) - dogal olarak iyilessin. Ilk
# basarili dongude sayac sifirlanir.
BLOCK_BACKOFF_BASE_SECONDS = 900       # 1. engel 900, 2. 1800, 3. 3600, 4. 7200 ...
BLOCK_BACKOFF_MAX_SECONDS = 4 * 3600   # tek bir backoff beklemesinin tavani
CIRCUIT_BREAK_AFTER_BLOCKS = 6         # bu kadar ardisik engelden sonra
CIRCUIT_BREAK_SECONDS = 6 * 3600       # tam mola (IP sogusun)
SILENT_BLOCK_MIN_ELAPSED = 20          # 0 canli mac + >=Xsn surdu = sessiz engel
                                      # (normalde bos liste ~1sn'de doner)

_last_scraped_at = {}


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

    # SADECE en uzun suredir taranmayan BATCH_SIZE kadar maci hedefle - flashscore_xg_client.py
    # ile ayni ilke (tum canli maclara degil, kucuk bir gruba, zamanla hepsi kapsanir).
    havuz = [m for m in live_summary if m["mid"] in kabul_edilen]

    # 2026-09-05: SINYAL-ILGILI MACLARA DARALT. Onceki davranis tum canli
    # maclar arasinda round-robin donuyordu - halbuki orchestrator'in bugunku
    # kurallariyla (bkz. app/core/orchestrator.py) maclarin cogunda ZATEN
    # sinyal uretilmiyor, o maclarin istatistigini cekmek SofaScore'a bosuna
    # istek atmak demekti. Her bosa istek de yeni bir IP engellenme riski
    # (olculen: 528 basarili cekime karsilik 181 engellenme, ve engel
    # kullanicinin KENDI tarayicisini da vuruyor). Artik oncelik sirasi:
    #   1) ilk yari + henuz gol yok  -> "Ilk Yari 0.5 Ust" hala iyi kalibre
    #      (%70.3), sinyal GERCEKTEN buradan cikiyor
    #   2) ikinci yari                -> "Mac Sonu X.5 Ust" marketleri acik
    # Bu iki gruba hic aday yoksa o turda HIC istatistik istegi atilmiyor
    # (sadece 1 canli-liste istegi) - sakin saatlerde ayak izi ~7 istekten
    # 2 istege duser.
    def _oncelik(m):
        stage = str(m.get("stage") or "")
        gol = (m.get("score_h") or 0) + (m.get("score_a") or 0)
        if stage.isdigit():
            dk = int(stage)
            if dk <= 45:
                return 0 if gol == 0 else 3   # ilk yari: golsuzler en oncelikli
            return 1                           # ikinci yari: MS marketleri acik
        return 3                               # devre arasi/bitmis/bilinmeyen

    havuz = [m for m in havuz if _oncelik(m) < 3]
    if not havuz:
        print("↷ [sofascore] sinyal-ilgili canli mac yok - istatistik istegi atlandi", flush=True)
        return len(live_summary), 0, 0

    havuz.sort(key=lambda m: (_oncelik(m), _last_scraped_at.get(m["mid"], 0)))
    hedefler = havuz[:BATCH_SIZE]

    islenen = basarili = 0
    for i, m in enumerate(hedefler):
        if i > 0:
            time.sleep(random.uniform(*BETWEEN_REQUEST_DELAY))
        _last_scraped_at[m["mid"]] = time.time()
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
          f"({CYCLE_PAUSE_SECONDS}sn'de bir; engelde kademeli backoff "
          f"{BLOCK_BACKOFF_BASE_SECONDS}sn -> {CIRCUIT_BREAK_SECONDS // 3600}sa devre kesici)", flush=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = browser.new_page()
        try:
            page.goto(WARMUP_URL, timeout=30000)
            page.wait_for_timeout(2000)
        except Exception as e:
            print(f"⚠️  Ilk yukleme basarisiz: {e}", flush=True)

        consecutive_blocks = 0
        while True:
            start = time.time()
            pause = CYCLE_PAUSE_SECONDS
            blok = False
            try:
                canli, islenen, basarili = run_cycle(args.api_base, secret, page)
            except Exception as e:
                msg = str(e)
                canli, islenen, basarili = 0, 0, 0
                if "http_403" in msg or "http_429" in msg or "Forbidden" in msg:
                    blok = True
                else:
                    print(f"⚠️  Dongu hatasi: {e}", flush=True)
            elapsed = time.time() - start

            # Sessiz engel: canli mac listesi BOS dondu ama fetch uzun surdu.
            # Normalde bos liste ~1sn'de doner; 20sn+ = istek takiliyor / kisitlaniyor
            # (page.evaluate'in 30sn timeout'una dayaniyor). Bunu da engel say.
            if not blok and canli == 0 and elapsed >= SILENT_BLOCK_MIN_ELAPSED:
                blok = True

            if blok:
                consecutive_blocks += 1
                if consecutive_blocks >= CIRCUIT_BREAK_AFTER_BLOCKS:
                    pause = CIRCUIT_BREAK_SECONDS
                    print(f"⛔ SofaScore {consecutive_blocks} kez ust uste engelledi - "
                          f"devre kesici: {pause / 3600:.0f} saat tam mola.", flush=True)
                else:
                    pause = min(
                        BLOCK_BACKOFF_BASE_SECONDS * (2 ** (consecutive_blocks - 1)),
                        BLOCK_BACKOFF_MAX_SECONDS,
                    )
                    print(f"⚠️  SofaScore engellemis gorunuyor (ardisik {consecutive_blocks}), "
                          f"{pause:.0f}sn bekleniyor.", flush=True)
                try:
                    page.goto(WARMUP_URL, timeout=30000)
                except Exception:
                    pass
            else:
                if consecutive_blocks:
                    print(f"✅ SofaScore tekrar cevap veriyor (onceki ardisik engel: "
                          f"{consecutive_blocks}) - backoff sayaci sifirlandi.", flush=True)
                consecutive_blocks = 0

            # Robotik/tam-saniyeli araliklardan kacinmak icin +-%15 jitter
            # (surekli TAM ayni araliklarla istek atmak da tespit isareti olabilir).
            pause_j = pause * random.uniform(0.85, 1.15)
            print(f"📊 {canli} canli mac, {islenen} istatistik icin denendi, "
                  f"{basarili} basarili ({elapsed:.1f}sn). {pause_j:.0f}sn bekleniyor... "
                  f"(durdurmak icin Ctrl+C)", flush=True)
            time.sleep(pause_j)

        browser.close()


if __name__ == "__main__":
    main()
