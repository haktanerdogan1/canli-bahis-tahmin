"""
Tum canli-bahis servislerini tek noktadan ayakta tutan supervisor.

Yonettigi surecler:
  - api            -> uvicorn api:app (FastAPI + index.html)
  - thesports_bot   -> TheSports API canli veri cekici (RapidAPI'nin yerine, kota
                       tukendigi icin degistirildi - bkz. git log)
  - orchestrator    -> 18 AI bot konsensus motoru (app/core/orchestrator.py)
  - flashscore_xg_bot -> Flashscore'dan canli xG cekip live_snapshots'a yazar
                       (TheSports Basic pakette xG yok - bkz. flashscore_xg_bot.py)

Not: sofascore (live_bot.py), sporkolik (sporkolik_bot.py) ve eski v4_api_bot.py
(RapidAPI) artik calistirilmiyor - kod referans/rollback icin projede duruyor.

Her surec coker/kapanirsa otomatik olarak yeniden baslatilir (basit backoff ile).
Loglar logs/<isim>.log dosyalarina yazilir.

Kullanim:
    python3 supervisor.py

Durdurmak icin: Ctrl+C (ya da launchd uzerinden calisiyorsa launchctl stop/unload)
"""
import os
import subprocess
import sys
import threading
import time
import signal

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(PROJECT_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

PYTHON = sys.executable

ENV = os.environ.copy()
ENV["PYTHONPATH"] = PROJECT_DIR
ENV["PYTHONUNBUFFERED"] = "1"

PORT = os.environ.get("PORT", "8000")

# Not: sofascore (live_bot.py) ve sporkolik (sporkolik_bot.py) botlari devre disi.
# Gercek canli veri kaynagi artik TheSports (thesports_bot.py) - RapidAPI'nin
# kotasi tukendigi icin degistirildi (bkz. git log). Eski v4_api_bot.py (RapidAPI)
# koddan silinmedi, gerekirse buradaki satiri geri alip hizlica rollback yapilabilir.
SERVICES = {
    "api": {
        "cmd": [PYTHON, "-m", "uvicorn", "api:app", "--host", "0.0.0.0", "--port", PORT],
    },
    "thesports_bot": {
        "cmd": [PYTHON, "-u", "thesports_bot.py"],
    },
    "orchestrator": {
        "cmd": [PYTHON, "-u", "-m", "app.core.orchestrator"],
    },
    # sevenm_client: 7msport.com canli veri kaynagi (bkz. sevenm_bot.py/
    # sevenm_client.py docstring'leri - hic bot korumasi yok, Playwright
    # GEREKTIRMIYOR, sadece `requests` ile iki duz JS dizi dosyasi cekiyor).
    # Bu yuzden flashscore_xg_bot'un aksine Railway'in bellek limitine
    # takilma riski yok - guvenle burada, ana 'web' servisinde calisabilir.
    # 2026-08-24: kullanicinin "PC kapansa da calissin" talebiyle eklendi -
    # oncesinde SADECE yerel makinede (launchd ile) calisiyordu, canli veri
    # akisi kullanicinin bilgisayarina bagimliydi.
    "sevenm_client": {
        "cmd": [PYTHON, "-u", "sevenm_client.py", "--api-base", f"http://127.0.0.1:{PORT}"],
    },
    # flashscore_xg_bot: GECICI OLARAK DEVRE DISI (bkz. git log). Ilk canli
    # denemede Chromium container'da tekrar tekrar "Page/Target crashed" verdi
    # ve servisin toplam bellek kullanimi 1GB limitine dayandi (0.999GB olculdu) -
    # tum sistemi (api dahil) OOM riskine soktugu icin geri alindi. Ayri bir
    # Railway servisinde izole calistirilmadan tekrar acilmamali.
    # "flashscore_xg_bot": {
    #     "cmd": [PYTHON, "-u", "flashscore_xg_bot.py"],
    # },
}

MAX_LOG_BYTES = 5 * 1024 * 1024  # 5MB, asarsa .old'a tasi

_procs = {}
_fail_streak = {name: 0 for name in SERVICES}
_shutdown = False


def _log_path(name):
    return os.path.join(LOG_DIR, f"{name}.log")


def _rotate_log_if_needed(name):
    path = _log_path(name)
    if os.path.exists(path) and os.path.getsize(path) > MAX_LOG_BYTES:
        old_path = path + ".old"
        try:
            if os.path.exists(old_path):
                os.remove(old_path)
            os.rename(path, old_path)
        except OSError:
            pass


def _pump_output(name, proc, log_file):
    """Alt surecin ciktisini hem ana stdout'a (Railway/terminal gorsun diye)
    hem de yerel log dosyasina yazar."""
    try:
        for raw_line in iter(proc.stdout.readline, b""):
            try:
                line = raw_line.decode("utf-8", errors="replace")
            except Exception:
                line = str(raw_line)
            if not line:
                break
            print(f"[{name}] {line.rstrip()}", flush=True)
            try:
                log_file.write(line)
                log_file.flush()
            except Exception:
                pass
    except Exception:
        pass


def start_service(name):
    cfg = SERVICES[name]
    _rotate_log_if_needed(name)
    log_file = open(_log_path(name), "a", encoding="utf-8")
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    log_file.write(f"\n===== [{ts}] supervisor: starting {name} =====\n")
    log_file.flush()

    proc = subprocess.Popen(
        cfg["cmd"],
        cwd=PROJECT_DIR,
        env=ENV,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    pump_thread = threading.Thread(
        target=_pump_output, args=(name, proc, log_file), daemon=True
    )
    pump_thread.start()
    _procs[name] = {"proc": proc, "log_file": log_file, "started_at": time.time(), "pump": pump_thread}
    print(f"[supervisor] {name} started (pid={proc.pid})", flush=True)


def stop_all():
    for name, info in _procs.items():
        proc = info["proc"]
        if proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
    deadline = time.time() + 10
    for name, info in _procs.items():
        proc = info["proc"]
        remaining = max(0, deadline - time.time())
        try:
            proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            proc.kill()
    for info in _procs.values():
        try:
            info["log_file"].close()
        except Exception:
            pass


def _handle_signal(signum, frame):
    global _shutdown
    _shutdown = True


def main():
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    print(f"[supervisor] proje dizini: {PROJECT_DIR}", flush=True)
    for name in SERVICES:
        start_service(name)

    while not _shutdown:
        time.sleep(5)
        for name, cfg in SERVICES.items():
            info = _procs.get(name)
            if info is None:
                continue
            proc = info["proc"]
            ret = proc.poll()
            if ret is not None:
                uptime = time.time() - info["started_at"]
                print(f"[supervisor] {name} durdu (exit={ret}, uptime={uptime:.1f}s)", flush=True)

                if uptime < 10:
                    _fail_streak[name] += 1
                else:
                    _fail_streak[name] = 0

                backoff = min(300, 5 * (2 ** _fail_streak[name]))
                print(f"[supervisor] {name} icin {backoff}s sonra yeniden baslatilacak", flush=True)
                time.sleep(backoff)
                if _shutdown:
                    break
                start_service(name)

    print("[supervisor] kapatiliyor...", flush=True)
    stop_all()
    print("[supervisor] tum servisler durduruldu.", flush=True)


if __name__ == "__main__":
    main()
