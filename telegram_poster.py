"""Sinyalleri iki asamada otomatik Telegram grubuna paylasan istemci.

x_poster.py ile AYNI akis (once acik sinyal duyurulur, sonuclandiginda o
mesaja YANIT olarak ikinci bir sonuc mesaji atilir) ama Telegram Bot API
ucretsiz ve dogrudan REST oldugu icin cok daha basit - OAuth imzalama yok,
odeme/onay engeli yok (bkz. x_poster.py'nin 402 Payment Required sorunu).

Ayri bir 'telegram_posted_signals' tablosu kullanir (x_posted_signals'tan
BAGIMSIZ) - Telegram'a atilan bir sinyal, X icin de "atildi" sayilmasin diye
(kullanici talebi, 2026-08-29).

KULLANIM:
    export ADMIN_SECRET=... TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=...
    python3 telegram_poster.py
"""
import os
import sys
import time
import traceback
import faulthandler

import requests

DEFAULT_API_BASE = "https://web-production-f1dba.up.railway.app"
CYCLE_PAUSE_SECONDS = 120


def _log(message):
    """2026-09-09, GPT-6 Astra ikinci-gorus: onceki kod SADECE baslangicta
    veya bir istisna oldugunda logluyordu - iki uc basariyla "found: false"
    donup sessizce devam ederse (COK OLASI, Astra'nin tahmini: %80) surec
    saglikli calisirken loglarda TAMAMEN SESSIZ gorunuyordu. Bu, dunku ve
    bugunku "donmus" degerlendirmemizin GERCEK bir kanit degil, VARSAYIM
    oldugunu gosterdi. Artik HER asama (baslangic/bitis, sure, sleep)
    monotonic saatle loglaniyor - bir dahaki "sessizlik" oldugunda son
    ":start" satiri TAM OLARAK nerede kaldigini gosterecek (tahmin degil)."""
    print(f"[telegram_poster] pid={os.getpid()} mono={time.monotonic():.3f} {message}", flush=True)

_ANNOUNCE_TAGS = "#canlibahis #iddaatahminleri #bankokupon #futbol #GününKuponu"
_RESULT_TAGS = "#canlibahis #iddaatahminleri #bankokupon"


def _format_announce(sinyal):
    prob = sinyal.get("probability")
    guven = f"%{round(prob * 100)}" if prob is not None else "-"
    return (
        f"🔴 CANLI SİNYAL 🎯\n\n"
        f"🏟️ {sinyal['home']} - {sinyal['away']}\n"
        f"🎯 Tahmin: {sinyal['market']} | AI Güven: {guven}\n\n"
        f"⚡ matchrixapp.com\n\n"
        f"{_ANNOUNCE_TAGS}"
    )


def _format_result(sinyal):
    if sinyal["outcome"] == "WON":
        body = "✅ TUTTU! 🎯\n\nBotlarımız yine haklı çıktı 🔥\n👉 matchrixapp.com"
    else:
        body = "❌ Bu sefer olmadı.\n\nKayıp seriler normaldir, disiplinli kasa yönetimiyle devam 💪\n👉 matchrixapp.com"
    return f"{body}\n\n{_RESULT_TAGS}"


def _send_message(bot_token, chat_id, text, reply_to_message_id=None):
    payload = {"chat_id": chat_id, "text": text}
    if reply_to_message_id:
        payload["reply_to_message_id"] = reply_to_message_id
        payload["allow_sending_without_reply"] = True
    r = requests.post(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        json=payload, timeout=20,
    )
    r.raise_for_status()
    return str(r.json()["result"]["message_id"])


# Bu process CALISIRKEN Telegram'a FIILEN gonderilmis (announce) prediction
# id -> message_id eslemesi. Kullanici talebi (2026-09-07): "1den fazla
# paylasim yapiyor onu duzelt". Kok neden: mesaj Telegram'a gonderildikten
# SONRA backend'e "gonderildi" diye yaziliyordu (mark-announced) - o yazma
# istegi gecici bir sebeple (ag hatasi, o gun yasadigimiz "database is
# locked" gibi) basarisiz olursa, sinyal DB'de hala "gonderilmemis"
# gorunuyor ve bir SONRAKI dongude AYNI mesaj TEKRAR Telegram'a atiliyordu.
# Telegram mesaji GERI ALINAMAZ - DB yazimi ise retry ile duzeltilebilir.
# Bu yuzden dogru sira: ONCE bu process-ici hafizaya isle (Telegram'a asla
# ikinci kez gonderme garantisi), SONRA DB'ye yazmayi retry ile dene.
_ANNOUNCED_LOCALLY = {}
_RESULTED_LOCALLY = set()
_RESULTED_LOCALLY_MSG = {}


def _mark_with_retry(url, admin_secret, params, tries=4, pause=3):
    """DB'ye 'gonderildi' yazma islemini birkac kez dener (gecici kilitlenme/
    ag sorunu icin). Hepsi basarisiz olursa False doner - cagiran taraf
    Telegram'a TEKRAR GONDERMEZ (bkz. yukaridaki aciklama), sadece bir
    sonraki dongude DB yazimini tekrar dener."""
    for attempt in range(tries):
        try:
            requests.post(
                url, headers={"x-admin-secret": admin_secret},
                params=params, timeout=15,
            ).raise_for_status()
            return True
        except Exception as e:
            if attempt < tries - 1:
                time.sleep(pause)
            else:
                print(f"⚠️  DB işaretleme {tries} denemede de başarısız oldu ({params}): {e}", flush=True)
    return False


def _handle_pending(api_base, admin_secret, bot_token, chat_id):
    r = requests.get(
        f"{api_base}/api/admin/telegram-poster/next-pending",
        headers={"x-admin-secret": admin_secret}, timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    if not data.get("found"):
        return False

    pred_id = data["id"]
    if pred_id in _ANNOUNCED_LOCALLY:
        # Mesaj bu process icinde ZATEN gonderildi, sadece DB kaydi eksik
        # kalmis (onceki denemede basarisiz oldu) - TEKRAR GONDERMEDEN
        # sadece isaretlemeyi yeniden dene.
        message_id = _ANNOUNCED_LOCALLY[pred_id]
        print(f"ℹ️  {pred_id} bu oturumda zaten gönderilmişti, sadece DB kaydı yeniden deneniyor...", flush=True)
        _mark_with_retry(
            f"{api_base}/api/admin/telegram-poster/mark-announced", admin_secret,
            {"id": pred_id, "message_id": message_id},
        )
        return False

    message_id = _send_message(bot_token, chat_id, _format_announce(data))
    print(f"📢 Anons edildi: {data['home']} - {data['away']} (msg {message_id})", flush=True)
    _ANNOUNCED_LOCALLY[pred_id] = message_id

    _mark_with_retry(
        f"{api_base}/api/admin/telegram-poster/mark-announced", admin_secret,
        {"id": pred_id, "message_id": message_id},
    )
    return True


def _handle_settled(api_base, admin_secret, bot_token, chat_id):
    r = requests.get(
        f"{api_base}/api/admin/telegram-poster/next-settled",
        headers={"x-admin-secret": admin_secret}, timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    if not data.get("found"):
        return False

    pred_id = data["id"]
    if pred_id in _RESULTED_LOCALLY:
        print(f"ℹ️  {pred_id} sonucu bu oturumda zaten gönderilmişti, sadece DB kaydı yeniden deneniyor...", flush=True)
        _mark_with_retry(
            f"{api_base}/api/admin/telegram-poster/mark-resulted", admin_secret,
            {"id": pred_id, "message_id": _RESULTED_LOCALLY_MSG.get(pred_id, "")},
        )
        return False

    message_id = _send_message(
        bot_token, chat_id, _format_result(data),
        reply_to_message_id=data["announce_message_id"],
    )
    print(f"🏁 Sonuç paylaşıldı: {data['home']} - {data['away']} ({data['outcome']}, msg {message_id})", flush=True)
    _RESULTED_LOCALLY.add(pred_id)
    _RESULTED_LOCALLY_MSG[pred_id] = message_id

    _mark_with_retry(
        f"{api_base}/api/admin/telegram-poster/mark-resulted", admin_secret,
        {"id": pred_id, "message_id": message_id},
    )
    return True


def run_cycle(api_base, admin_secret, bot_token, chat_id, cycle_id):
    started = time.monotonic()
    did_something = False

    _log(f"cycle={cycle_id} pending:start")
    try:
        pending_started = time.monotonic()
        pending_result = _handle_pending(api_base, admin_secret, bot_token, chat_id)
        did_something |= pending_result
        _log(f"cycle={cycle_id} pending:end result={pending_result} "
             f"elapsed={time.monotonic() - pending_started:.3f}s")
    except Exception:
        _log(f"cycle={cycle_id} pending:error\n{traceback.format_exc()}")

    _log(f"cycle={cycle_id} settled:start")
    try:
        settled_started = time.monotonic()
        settled_result = _handle_settled(api_base, admin_secret, bot_token, chat_id)
        did_something |= settled_result
        _log(f"cycle={cycle_id} settled:end result={settled_result} "
             f"elapsed={time.monotonic() - settled_started:.3f}s")
    except Exception:
        _log(f"cycle={cycle_id} settled:error\n{traceback.format_exc()}")

    _log(f"cycle={cycle_id} end did_something={did_something} "
         f"elapsed={time.monotonic() - started:.3f}s")
    return did_something


def main():
    api_base = os.environ.get("TELEGRAM_POSTER_API_BASE", DEFAULT_API_BASE)
    admin_secret = os.environ.get("ADMIN_SECRET")
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    missing = [n for n, v in [
        ("ADMIN_SECRET", admin_secret), ("TELEGRAM_BOT_TOKEN", bot_token),
        ("TELEGRAM_CHAT_ID", chat_id),
    ] if not v]
    if missing:
        print(f"HATA: eksik ortam değişkeni: {', '.join(missing)}", flush=True)
        sys.exit(1)

    faulthandler.enable()
    _log(f"started api_base={api_base}")
    cycle_id = 0
    while True:
        cycle_id += 1
        # Astra'nin onerdigi watchdog: run_cycle beklenenden COK uzun surerse
        # (180sn - iki HTTP el sikismasi + retry'larin normal en kotu durumu
        # olan ~70sn'nin bolca ustunde) GERCEK bir Python stack dump'i
        # loglara yazilir - "nerede takili" sorusuna tahmin degil, KANIT.
        # Sadece bu blok etrafinda kurulu, main() basinda DEGIL - aksi halde
        # saglikli 120sn'lik sleep() bile yanlis alarm uretirdi.
        faulthandler.dump_traceback_later(180, repeat=False, exit=False)
        try:
            did_something = run_cycle(api_base, admin_secret, bot_token, chat_id, cycle_id)
        except BaseException:
            # Teshis icin BaseException'i da (KeyboardInterrupt/SystemExit
            # DAHIL) logluyoruz, sonra ayni sekilde tekrar yukseltiyoruz -
            # davranis DEGISMIYOR, sadece "sessizce cikti mi" sorusuna
            # cevap ekleniyor.
            _log(f"cycle={cycle_id} unexpected-base-exception\n{traceback.format_exc()}")
            raise
        finally:
            faulthandler.cancel_dump_traceback_later()

        sleep_seconds = 5 if did_something else CYCLE_PAUSE_SECONDS
        _log(f"cycle={cycle_id} sleep:start seconds={sleep_seconds}")
        time.sleep(sleep_seconds)
        _log(f"cycle={cycle_id} sleep:end")


if __name__ == "__main__":
    main()
