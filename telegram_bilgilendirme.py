"""Telegram grubuna 3 saatte bir donen iki bilgilendirme mesajini
otomatik atan istemci - kullanici talebi (2026-09-08): "telegramda ...
bu ve bu mesaji otomatik atalim". Kadans 2026-09-10'da yarim saatten
3 saate cikarildi (kullanici: "30 dakika olmasin, 3 saatte 1" - grup
her yarim saatte tekrarlanan sabit mesajlardan bunaliyordu).

Sinyal anonsu/sonucu (telegram_poster.py) ile TAMAMEN AYRI bir akis - bu
betik SADECE iki sabit bilgilendirme mesajini donusumlu paylasir, hicbir
sinyal/sonuc verisine dokunmaz.

IKI MESAJ:
  A) Kisa "bahis uygulamasi degiliz" aciklamasi (sabit metin).
  B) Kasa yonetimi bilgilendirmesi - icindeki "son 30 gunde haftada
     ortalama X sinyal, isabet orani %Y" rakamlari HER GONDERIMDE
     /api/admin/panel/kasa-bilgi-istatistik'ten TAZE cekilir, sabit
     yazilmaz (CLAUDE.md kural 2: olcmeden iddia yok - zaman gectikce
     eski/yanlis bir rakami tekrar tekrar atmamak icin).

KULLANIM:
    export ADMIN_SECRET=... TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=...
    python3 telegram_bilgilendirme.py

Kullanici bu betigi devreye almadan hemen once mesajlardan birini ELLE
attigi icin ILK otomatik gonderim, surec baslar baslamaz DEGIL, tam
CYCLE_PAUSE_SECONDS sonra yapilir (asagida ilk dongude sleep ONCE
calisiyor, bkz. main())."""
import os
import sys
import time

import requests

DEFAULT_API_BASE = "https://web-production-f1dba.up.railway.app"
CYCLE_PAUSE_SECONDS = 3 * 60 * 60  # 3 saat (2026-09-10, onceden yarim saatti)

_MESSAGE_A = (
    "Matchrix bir bahis uygulaması değildir; bahis kabul etmez, "
    "bahis parası tutmaz, kazanç garantisi vermez."
)


def _format_message_b(istatistik):
    haftalik = istatistik.get("haftalik_ortalama_sinyal")
    isabet = istatistik.get("isabet_orani")
    haftalik_txt = f"{haftalik:.1f}" if haftalik is not None else "-"
    isabet_txt = f"%{round(isabet * 100)}" if isabet is not None else "-"
    return (
        "Kasanı Nasıl Yönetmelisin?\n"
        f"Son 30 günde haftada ortalama {haftalik_txt} sinyal paylaştık, "
        f"isabet oranımız {isabet_txt}. Ama bu, her sinyalin kazanacağı "
        "anlamına gelmez — kayıp seriler normaldir ve olacaktır. Haftanı "
        "karlı kapatmanın yolu tek bir büyük bahis değil, disiplinli kasa "
        "yönetimidir: tek sinyale kasanın %10-20'sinden fazlasını ayırma, "
        "kayıp gelse bile bahis miktarını artırma (martingale yapma), ve "
        "asla kaybetmeyi göze alamayacağın parayla oynama.\n"
        "Bu genel bir kasa-yönetimi bilgilendirmesidir, kişisel finansal "
        "tavsiye değildir. Geçmiş performans gelecekteki sonuçları garanti "
        "etmez."
    )


def _fetch_istatistik(api_base, admin_secret):
    r = requests.get(
        f"{api_base}/api/admin/panel/kasa-bilgi-istatistik",
        headers={"x-admin-secret": admin_secret}, timeout=15,
    )
    r.raise_for_status()
    return r.json()


def _send_message(bot_token, chat_id, text):
    r = requests.post(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        json={"chat_id": chat_id, "text": text}, timeout=20,
    )
    r.raise_for_status()
    return str(r.json()["result"]["message_id"])


def run_once(api_base, admin_secret, bot_token, chat_id, sira):
    """sira: 0 -> mesaj A, 1 -> mesaj B (donusumlu)."""
    if sira % 2 == 0:
        text = _MESSAGE_A
        etiket = "A (bahis-degiliz)"
    else:
        istatistik = _fetch_istatistik(api_base, admin_secret)
        text = _format_message_b(istatistik)
        etiket = f"B (kasa-yonetimi, {istatistik.get('sonuclanan')} sinyal/30gun)"

    message_id = _send_message(bot_token, chat_id, text)
    print(f"📨 Bilgilendirme mesajı {etiket} gönderildi (msg {message_id})", flush=True)


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

    print(f"🚀 Telegram bilgilendirme botu başlatılıyor -> {api_base} "
          f"({CYCLE_PAUSE_SECONDS / 3600:g} saatte bir, A/B dönüşümlü)", flush=True)

    sira = 0
    while True:
        # Kullanici bu betik devreye alinmadan hemen once mesajlardan birini
        # ELLE atmisti - ilk otomatik gonderim hemen degil, bir tam dongu
        # sonra olsun diye sleep BASTA (dongunun sonunda degil).
        time.sleep(CYCLE_PAUSE_SECONDS)
        try:
            run_once(api_base, admin_secret, bot_token, chat_id, sira)
            sira += 1
        except Exception as e:
            print(f"⚠️  Bilgilendirme döngüsü hatası: {e}", flush=True)
            # Basarisiz denemede sira ILERLETILMEZ - bir sonraki dongude AYNI
            # mesaji tekrar denesin (mesaj gonderilmediyse zaten sorun yok,
            # Telegram'a atilmamis bir sey icin "atildi" yanlislikla
            # isaretlenmiyor - bu betikte DB yazimi yok, sadece Telegram POST'u).


if __name__ == "__main__":
    main()
