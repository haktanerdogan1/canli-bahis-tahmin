"""Kazanan sinyalleri otomatik olarak X'e (Twitter) paylasan istemci.

Railway'de supervisor.py uzerinden calisir (Playwright/Chromium GEREKTIRMEZ,
sadece `requests` - sevenm_client.py/iddaa_odds_client.py ile ayni hafif
desen). /api/admin/x-poster/next-win'den paylasilmamis en eski WON sinyalini
cekip X API v2'ye (POST /2/tweets) OAuth 1.0a imzasiyla gonderir, basarili
olursa /api/admin/x-poster/mark-posted ile isaretler.

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
CYCLE_PAUSE_SECONDS = 180
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


def _format_tweet(sinyal):
    home = sinyal["home"]
    away = sinyal["away"]
    market = sinyal["market"]
    prob = sinyal.get("probability")
    guven = f"%{round(prob * 100)}" if prob is not None else "-"
    return (
        f"✅ TUTTU! 🎯\n\n"
        f"🏟️ {home} - {away}\n"
        f"🎯 Tahmin: {market} | AI Güven: {guven}\n\n"
        f"⚡ Sitede açık olan diğer tüm canlı tahminlere ücretsiz ulaş:\n"
        f"👉 matchrixapp.com\n\n"
        f"#bankokupon #GününKuponu #canlibahis #iddaatahminleri"
    )


def _post_tweet(text, consumer_key, consumer_secret, token, token_secret):
    header = _oauth1_header("POST", X_TWEET_URL, consumer_key, consumer_secret, token, token_secret)
    r = requests.post(
        X_TWEET_URL,
        headers={"Authorization": header, "Content-Type": "application/json"},
        json={"text": text}, timeout=20,
    )
    r.raise_for_status()
    return r.json()


def run_cycle(api_base, admin_secret, consumer_key, consumer_secret, token, token_secret):
    r = requests.get(
        f"{api_base}/api/admin/x-poster/next-win",
        headers={"x-admin-secret": admin_secret}, timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    if not data.get("found"):
        return False

    text = _format_tweet(data)
    _post_tweet(text, consumer_key, consumer_secret, token, token_secret)
    print(f"✅ Paylaşıldı: {data['home']} - {data['away']} ({data['market']})", flush=True)

    requests.post(
        f"{api_base}/api/admin/x-poster/mark-posted",
        headers={"x-admin-secret": admin_secret},
        params={"id": data["id"]}, timeout=15,
    ).raise_for_status()
    return True


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

    print(f"🚀 X paylaşım botu başlatılıyor -> {api_base}", flush=True)
    while True:
        try:
            posted = run_cycle(api_base, admin_secret, consumer_key, consumer_secret, token, token_secret)
        except Exception as e:
            print(f"⚠️  Döngü hatası: {e}", flush=True)
            posted = False
        # Bir sinyal paylasildiysa hemen ardindan bekleyen baska biri olup
        # olmadigina hizli bakiyoruz (kisa bekleme); yoksa normal aralik.
        time.sleep(5 if posted else CYCLE_PAUSE_SECONDS)


if __name__ == "__main__":
    main()
