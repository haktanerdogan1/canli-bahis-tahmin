"""Sinyalleri iki asamada otomatik X'e (Twitter) paylasan istemci.

Railway'de supervisor.py uzerinden calisir (Playwright/Chromium GEREKTIRMEZ,
sadece `requests` - sevenm_client.py/iddaa_odds_client.py ile ayni hafif
desen). Kullanici talebi (2026-08-29): once sinyal acildiginda bir "canli
sinyal" tweeti atilir, sinyal sonuclandiginda (WON/LOST) o tweeti ALINTILAYAN
ikinci bir sonuc tweeti atilir.

Akis (her dongude iki ayri kontrol):
  1) /api/admin/x-poster/next-pending -> henuz anons edilmemis en eski ACIK
     sinyal varsa "canli sinyal" tweeti atilir, tweet id'si mark-announced
     ile kaydedilir.
  2) /api/admin/x-poster/next-settled -> anonsu atilmis ama sonucu henuz
     paylasilmamis, ARTIK sonuclanmis bir sinyal varsa, orijinal tweeti
     alintilayan bir SONUC tweeti atilir, mark-resulted ile kaydedilir.

OAuth 1.0a imzalama stdlib ile elle yapiliyor (hmac/hashlib/base64/urllib) -
`requests_oauthlib` gibi ekstra bir bagimlilik eklemeye gerek yok.

KULLANIM:
    export X_CONSUMER_KEY=... X_CONSUMER_SECRET=... X_ACCESS_TOKEN=... X_ACCESS_TOKEN_SECRET=...
    python3 x_poster.py
"""
import base64
import hashlib
import hmac
import os
import secrets
import sys
import time
import urllib.parse

import requests

DEFAULT_API_BASE = "https://web-production-f1dba.up.railway.app"
CYCLE_PAUSE_SECONDS = 120
X_TWEET_URL = "https://api.x.com/2/tweets"


def _oauth1_header(method, url, consumer_key, consumer_secret, token, token_secret):
    """RFC 5849 HMAC-SHA1 imzali Authorization header'i uretir. Bu istekte
    gonderilecek gorunur (query/form) parametre olmadigi icin (JSON govde
    imzaya dahil DEGIL, OAuth1 sadece query+form parametrelerini imzalar)
    imza tabani sadece oauth_* alanlarindan olusuyor."""
    oauth_params = {
        "oauth_consumer_key": consumer_key,
        "oauth_nonce": secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(time.time())),
        "oauth_token": token,
        "oauth_version": "1.0",
    }
    quote = lambda s: urllib.parse.quote(str(s), safe="")
    param_str = "&".join(f"{quote(k)}={quote(v)}" for k, v in sorted(oauth_params.items()))
    base_str = "&".join([method.upper(), quote(url), quote(param_str)])
    signing_key = f"{quote(consumer_secret)}&{quote(token_secret)}"
    signature = base64.b64encode(
        hmac.new(signing_key.encode(), base_str.encode(), hashlib.sha1).digest()
    ).decode()
    oauth_params["oauth_signature"] = signature
    header = "OAuth " + ", ".join(f'{quote(k)}="{quote(v)}"' for k, v in sorted(oauth_params.items()))
    return header


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


def _post_tweet(text, consumer_key, consumer_secret, token, token_secret, quote_tweet_id=None):
    header = _oauth1_header("POST", X_TWEET_URL, consumer_key, consumer_secret, token, token_secret)
    body = {"text": text}
    if quote_tweet_id:
        body["quote_tweet_id"] = quote_tweet_id
    r = requests.post(
        X_TWEET_URL,
        headers={"Authorization": header, "Content-Type": "application/json"},
        json=body, timeout=20,
    )
    r.raise_for_status()
    return r.json()["data"]["id"]


def _handle_pending(api_base, admin_secret, keys):
    r = requests.get(
        f"{api_base}/api/admin/x-poster/next-pending",
        headers={"x-admin-secret": admin_secret}, timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    if not data.get("found"):
        return False

    tweet_id = _post_tweet(_format_announce(data), *keys)
    print(f"📢 Anons edildi: {data['home']} - {data['away']} (tweet {tweet_id})", flush=True)

    requests.post(
        f"{api_base}/api/admin/x-poster/mark-announced",
        headers={"x-admin-secret": admin_secret},
        params={"id": data["id"], "tweet_id": tweet_id}, timeout=15,
    ).raise_for_status()
    return True


def _handle_settled(api_base, admin_secret, keys):
    r = requests.get(
        f"{api_base}/api/admin/x-poster/next-settled",
        headers={"x-admin-secret": admin_secret}, timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    if not data.get("found"):
        return False

    tweet_id = _post_tweet(
        _format_result(data), *keys, quote_tweet_id=data["announce_tweet_id"]
    )
    print(f"🏁 Sonuç paylaşıldı: {data['home']} - {data['away']} ({data['outcome']}, tweet {tweet_id})", flush=True)

    requests.post(
        f"{api_base}/api/admin/x-poster/mark-resulted",
        headers={"x-admin-secret": admin_secret},
        params={"id": data["id"], "tweet_id": tweet_id}, timeout=15,
    ).raise_for_status()
    return True


def run_cycle(api_base, admin_secret, keys):
    did_something = False
    try:
        did_something |= _handle_pending(api_base, admin_secret, keys)
    except Exception as e:
        print(f"⚠️  Anons döngüsü hatası: {e}", flush=True)
    try:
        did_something |= _handle_settled(api_base, admin_secret, keys)
    except Exception as e:
        print(f"⚠️  Sonuç döngüsü hatası: {e}", flush=True)
    return did_something


def main():
    api_base = os.environ.get("X_POSTER_API_BASE", DEFAULT_API_BASE)
    admin_secret = os.environ.get("ADMIN_SECRET")
    consumer_key = os.environ.get("X_CONSUMER_KEY")
    consumer_secret = os.environ.get("X_CONSUMER_SECRET")
    token = os.environ.get("X_ACCESS_TOKEN")
    token_secret = os.environ.get("X_ACCESS_TOKEN_SECRET")

    missing = [n for n, v in [
        ("ADMIN_SECRET", admin_secret), ("X_CONSUMER_KEY", consumer_key),
        ("X_CONSUMER_SECRET", consumer_secret), ("X_ACCESS_TOKEN", token),
        ("X_ACCESS_TOKEN_SECRET", token_secret),
    ] if not v]
    if missing:
        print(f"HATA: eksik ortam değişkeni: {', '.join(missing)}", flush=True)
        sys.exit(1)

    keys = (consumer_key, consumer_secret, token, token_secret)
    print(f"🚀 X paylaşım botu başlatılıyor -> {api_base}", flush=True)
    while True:
        try:
            did_something = run_cycle(api_base, admin_secret, keys)
        except Exception as e:
            print(f"⚠️  Döngü hatası: {e}", flush=True)
            did_something = False
        # Bir seyler paylasildiysa hemen ardindan baska bekleyen var mi diye
        # hizli bakiyoruz; yoksa normal aralik kadar bekliyoruz.
        time.sleep(5 if did_something else CYCLE_PAUSE_SECONDS)


if __name__ == "__main__":
    main()
