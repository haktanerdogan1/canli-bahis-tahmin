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

import requests

DEFAULT_API_BASE = "https://web-production-f1dba.up.railway.app"
CYCLE_PAUSE_SECONDS = 120

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


def _handle_pending(api_base, admin_secret, bot_token, chat_id):
    r = requests.get(
        f"{api_base}/api/admin/telegram-poster/next-pending",
        headers={"x-admin-secret": admin_secret}, timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    if not data.get("found"):
        return False

    message_id = _send_message(bot_token, chat_id, _format_announce(data))
    print(f"📢 Anons edildi: {data['home']} - {data['away']} (msg {message_id})", flush=True)

    requests.post(
        f"{api_base}/api/admin/telegram-poster/mark-announced",
        headers={"x-admin-secret": admin_secret},
        params={"id": data["id"], "message_id": message_id}, timeout=15,
    ).raise_for_status()
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

    message_id = _send_message(
        bot_token, chat_id, _format_result(data),
        reply_to_message_id=data["announce_message_id"],
    )
    print(f"🏁 Sonuç paylaşıldı: {data['home']} - {data['away']} ({data['outcome']}, msg {message_id})", flush=True)

    requests.post(
        f"{api_base}/api/admin/telegram-poster/mark-resulted",
        headers={"x-admin-secret": admin_secret},
        params={"id": data["id"], "message_id": message_id}, timeout=15,
    ).raise_for_status()
    return True


def run_cycle(api_base, admin_secret, bot_token, chat_id):
    did_something = False
    try:
        did_something |= _handle_pending(api_base, admin_secret, bot_token, chat_id)
    except Exception as e:
        print(f"⚠️  Anons döngüsü hatası: {e}", flush=True)
    try:
        did_something |= _handle_settled(api_base, admin_secret, bot_token, chat_id)
    except Exception as e:
        print(f"⚠️  Sonuç döngüsü hatası: {e}", flush=True)
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

    print(f"🚀 Telegram paylaşım botu başlatılıyor -> {api_base}", flush=True)
    while True:
        try:
            did_something = run_cycle(api_base, admin_secret, bot_token, chat_id)
        except Exception as e:
            print(f"⚠️  Döngü hatası: {e}", flush=True)
            did_something = False
        time.sleep(5 if did_something else CYCLE_PAUSE_SECONDS)


if __name__ == "__main__":
    main()
