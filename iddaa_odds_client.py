"""İddaa.com'dan HER GÜN oynanan (henüz başlamamış) futbol maçlarının
Maç Sonucu (1X2) ve İlk Yarı / Maç Sonu Alt-Üst oranlarını çeken istemci.

Railway'de (supervisor.py'nin bir süreci olarak) çalışır - Flashscore'un
aksine Playwright GEREKTİRMEZ (7msport gibi düz `requests`), hiç bot
koruması gözlemlenmedi (2026-08-24, plain curl bile 200 dönüyor).

NEDEN VAR: bot_odds_profile.py (arşivdeki 33.525 maçın AÇILIŞ oranını
sonuçla eşleştiren, out-of-sample doğrulanmış bir bot) canlı bir 1X2 oran
kaynağına ihtiyaç duyuyordu ama eski kaynak (sürekli güncellenen CANLI/
in-play oran) maç ilerledikçe beraberlik favorileşip her maçı "dengeli"
gösterdiği için devre dışı bırakılmıştı (bkz. git log, orchestrator.py'deki
eski NOT). Bu istemci bunun yerine MAÇ BAŞLAMADAN ÖNCEKİ (açılış) oranını
çekiyor - aynı kaynak (İddaa) + aynı "açılış oranı" mantığı arşivle birebir
tutarlı. api.py:_iddaa_transfer_odds maç canlıya geçtiğinde bu değeri TEK
SEFERLİK dondurup live_odds'a yazıyor - "maç ilerledikçe bozulma" sorunu yok.

AYRICA (kullanıcı talebi 2026-08-24): sadece bot_odds_profile için değil,
HER GÜN gördüğümüz maç+oranı kalıcı bir arşive (iddaa_odds_archive)
yazıyoruz - api.py sonuçlanan maçları kendi canlı verimizden otomatik
dolduruyor (bkz. _iddaa_backfill_results). Zamanla kendi Iddaa-kaynaklı
oran+sonuç veri setimiz oluşuyor.

VERİ KAYNAĞI: sportsbookv2.iddaa.com/sportsbook/events - market kodları
get_market_config'ten çözüldü: t=1,st=1 = Maç Sonucu (1X2); t=2,st=60 =
"1. Yarı Alt/Üst {0}"; t=2,st=101 = "Alt/Üst {0}" (maç sonu).

KULLANIM:
    export BACKUP_SECRET=<Railway'deki BACKUP_SECRET değeri>
    python3 iddaa_odds_client.py
"""
import os
import sys
import time
import argparse

import requests

DEFAULT_API_BASE = "https://web-production-f1dba.up.railway.app"
CYCLE_PAUSE_SECONDS = 300
EVENTS_URL = "https://sportsbookv2.iddaa.com/sportsbook/events?st=1&type=0&version=0"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"}

MT_1X2 = (1, 1)
MT_FH_OU = (2, 60)   # "1. Yarı Alt/Üst {0}"
MT_FT_OU = (2, 101)  # "Alt/Üst {0}" (maç sonu)

# Tercih edilen çizgiler - birden fazla varsa bunlara en yakini/aynisi secilir.
FH_PREFERRED_LINES = ("0.5", "1.5")
FT_PREFERRED_LINES = ("1.5", "2.5")


def _find_market(markets, mt):
    """Verilen (t, st) ikilisine uyan TÜM market girişlerini döner (bir
    maçta aynı market tipinin birden fazla çizgisi/varyantı olabilir)."""
    t, st = mt
    return [mk for mk in (markets or []) if mk.get("t") == t and mk.get("st") == st]


def _pick_ou(markets, mt, preferred_lines):
    """t/st'ye uyan Alt-Üst marketleri arasindan tercih edilen bir cizgiyi
    (yoksa ilkini) secip (line, over_odd, under_odd) doner."""
    found = _find_market(markets, mt)
    if not found:
        return None, None, None
    chosen = None
    for line in preferred_lines:
        for mk in found:
            if mk.get("sov") == line:
                chosen = mk
                break
        if chosen:
            break
    if not chosen:
        chosen = found[0]
    over_odd = under_odd = None
    for o in (chosen.get("o") or []):
        if o.get("n") == "Üst":
            over_odd = o.get("odd")
        elif o.get("n") == "Alt":
            under_odd = o.get("odd")
    try:
        line = float(chosen.get("sov"))
    except (TypeError, ValueError):
        line = None
    return line, over_odd, under_odd


def _pick_1x2(markets):
    found = _find_market(markets, MT_1X2)
    if not found:
        return None, None, None
    o1 = ox = o2 = None
    for o in (found[0].get("o") or []):
        n = o.get("n")
        if n == "1":
            o1 = o.get("odd")
        elif n == "0":
            ox = o.get("odd")
        elif n == "2":
            o2 = o.get("odd")
    return o1, ox, o2


def fetch_prematch_events(session):
    """Henüz başlamamış (s==0) futbol (sid==1) maçlarının oranlarını çeker."""
    r = session.get(EVENTS_URL, headers=HEADERS, timeout=25)
    r.raise_for_status()
    data = r.json()
    events = ((data.get("data") or {}).get("events")) or []

    out = []
    for e in events:
        if e.get("sid") != 1 or e.get("s") != 0:
            continue
        home, away = e.get("hn"), e.get("an")
        eid = e.get("i")
        if not home or not away or not eid:
            continue
        markets = e.get("m") or []
        odd_1, odd_x, odd_2 = _pick_1x2(markets)
        fh_line, fh_over, fh_under = _pick_ou(markets, MT_FH_OU, FH_PREFERRED_LINES)
        ft_line, ft_over, ft_under = _pick_ou(markets, MT_FT_OU, FT_PREFERRED_LINES)
        if not (odd_1 and odd_x and odd_2):
            continue
        out.append({
            "event_id": eid, "home": home, "away": away,
            "league": str(e.get("ci") or ""),
            "odd_1": odd_1, "odd_x": odd_x, "odd_2": odd_2,
            "fh_over_line": fh_line, "fh_over_odd": fh_over, "fh_under_odd": fh_under,
            "ms_over_line": ft_line, "ms_over_odd": ft_over, "ms_under_odd": ft_under,
        })
    return out


def run_cycle(session, api_base, secret):
    events = fetch_prematch_events(session)
    if not events:
        return 0, 0
    r = session.post(
        f"{api_base}/api/admin/iddaa-odds-sync",
        headers={"x-backup-secret": secret}, json={"events": events}, timeout=30,
    )
    r.raise_for_status()
    d = r.json()
    return d.get("yazilan", 0), d.get("sonuc_dolduruldu", 0)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-base", default=DEFAULT_API_BASE)
    args = parser.parse_args()

    secret = os.environ.get("BACKUP_SECRET")
    if not secret:
        print("HATA: BACKUP_SECRET ortam degiskeni tanimli degil. "
              "export BACKUP_SECRET=... calistirip tekrar dene.", flush=True)
        sys.exit(1)

    print(f"🚀 İddaa oran istemcisi baslatiliyor -> {args.api_base} "
          f"({CYCLE_PAUSE_SECONDS}sn'de bir)", flush=True)
    session = requests.Session()
    while True:
        start = time.time()
        try:
            yazilan, sonuc_dolan = run_cycle(session, args.api_base, secret)
        except Exception as e:
            print(f"⚠️  Dongu hatasi: {e}", flush=True)
            yazilan, sonuc_dolan = 0, 0
        elapsed = time.time() - start
        print(f"📊 {yazilan} mac oran kaydi guncellendi, {sonuc_dolan} eski kayda sonuc "
              f"dolduruldu ({elapsed:.1f}sn). {CYCLE_PAUSE_SECONDS}sn bekleniyor...", flush=True)
        time.sleep(CYCLE_PAUSE_SECONDS)


if __name__ == "__main__":
    main()
