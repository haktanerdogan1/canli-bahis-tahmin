#!/usr/bin/env python3
"""Terminal bot izleme ekrani - canli sistem durumu.

Railway'deki uretim API'sinin admin panel uclarini periyodik olarak
cekip tek ekranda gosterir: sistem nabzi, canli veri kapsamasi, her
botun en son ne zaman calistigi, DB sagligi.

KULLANIM:
    export ADMIN_SECRET=<Railway'deki ADMIN_SECRET degeri>
    python3 bot_izle.py                # 20sn'de bir yeniler
    python3 bot_izle.py --once         # tek sefer bas, cik
    python3 bot_izle.py --aralik 10    # yenileme araligini degistir
    python3 bot_izle.py --api-base http://localhost:8000

Sadece OKUR - hicbir yazma/silme ucuna dokunmaz.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

DEFAULT_API_BASE = "https://web-production-f1dba.up.railway.app"
BAYAT_ESIK_SN = 300  # bir bot 5 dk'dir yazmadiysa "bayat" say

C = {
    "reset": "\033[0m", "bold": "\033[1m", "dim": "\033[2m",
    "red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m",
    "blue": "\033[34m", "cyan": "\033[36m", "gray": "\033[90m",
}
if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
    C = {k: "" for k in C}


def renk(s, *adlar):
    return "".join(C[a] for a in adlar) + str(s) + C["reset"]


def _iso_parse(s):
    if not s:
        return None
    s = s.strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f",
                "%Y-%m-%d %H:%M:%S%z"):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def yas_str(iso, simdi=None):
    """ISO zaman damgasindan 'Xsn once' / 'Xdk once' uret."""
    dt = _iso_parse(iso)
    if dt is None:
        return renk("?", "gray"), None
    simdi = simdi or datetime.now(timezone.utc)
    fark = (simdi - dt).total_seconds()
    if fark < 0:
        fark = 0
    if fark < 90:
        txt = f"{int(fark)}sn"
    elif fark < 5400:
        txt = f"{int(fark / 60)}dk"
    else:
        txt = f"{fark / 3600:.1f}sa"
    return txt, fark


def cek(api_base, yol, secret, timeout=20):
    req = urllib.request.Request(
        api_base.rstrip("/") + yol,
        headers={"x-admin-secret": secret, "User-Agent": "bot_izle/1.0"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def bolum(baslik):
    print()
    print(renk(f"── {baslik} ", "bold", "cyan") + renk("─" * (58 - len(baslik)), "gray"))


def nabiz_yaz(ozet, kapsama):
    bolum("SISTEM NABZI")
    if not ozet:
        print(renk("  (ozet alinamadi)", "red"))
    else:
        won, lost = ozet.get("kazanan", 0), ozet.get("kaybeden", 0)
        io = ozet.get("isabet_orani")
        io_txt = f"%{io * 100:.1f}" if io is not None else "-"
        io_renk = "green" if (io or 0) >= 0.6 else ("yellow" if (io or 0) >= 0.5 else "red")
        print(f"  Sinyal: {renk(ozet.get('toplam_sinyal', 0), 'bold')} toplam  "
              f"{renk(str(won) + 'K', 'green')}/{renk(str(lost) + 'Y', 'red')}  "
              f"isabet {renk(io_txt, io_renk, 'bold')}  "
              f"| bekleyen {renk(ozet.get('bekleyen', 0), 'yellow')}  "
              f"| gecersiz {ozet.get('gecersiz', 0)}  "
              f"| uye {ozet.get('uye_sayisi', 0)}")
    if kapsama:
        n = kapsama.get("canli_mac_sayisi", 0)
        oran = kapsama.get("sut_verisi_var_ve_yeterli_oran")
        oran_txt = f"%{oran * 100:.0f}" if oran is not None else "-"
        fs_t = kapsama.get("kaynak_kirilimi", {}).get("fs_toplam_canli_mac", 0)
        fs_d = kapsama.get("kaynak_kirilimi", {}).get("fs_sut_verisi_dolu", 0)
        print(f"  Canli mac: {renk(n, 'bold')}  "
              f"| sut verisi yeterli: {renk(oran_txt, 'bold')} "
              f"({kapsama.get('sut_verisi_var_ve_yeterli(>=4)', 0)}/{n})  "
              f"| xG: {kapsama.get('xg_verisi_var', 0)}  "
              f"| korner: {kapsama.get('korner_verisi_var', 0)}")
        if fs_t:
            print(renk(f"  flashscore kaynakli: {fs_d}/{fs_t} macta sut verisi dolu", "gray"))


def botlar_yaz(durum):
    bolum("BOT DURUMU  (son calisma / degerlendirme / sinyal / ort. iddia)")
    if not durum or not durum.get("botlar"):
        print(renk("  (bot durumu alinamadi)", "red"))
        return
    simdi = _iso_parse(durum.get("sunucu_saati")) or datetime.now(timezone.utc)
    bayat = 0
    for b in durum["botlar"]:
        yas, fark = yas_str(b.get("son_calisma"), simdi)
        if fark is not None and fark > BAYAT_ESIK_SN:
            bayat += 1
            isim = renk(f"{b['bot']:<24}", "red")
            yas_g = renk(f"{yas:>5}", "red", "bold")
        else:
            isim = f"{b['bot']:<24}"
            yas_g = renk(f"{yas:>5}", "green")
        iddia = b.get("ortalama_iddia")
        iddia_txt = f"%{iddia * 100:.0f}" if iddia is not None else renk("  -", "gray")
        print(f"  {isim} {yas_g}  "
              f"{b.get('toplam_degerlendirme', 0):>6} deg  "
              f"{b.get('toplam_sinyal', 0):>4} sin  "
              f"{iddia_txt:>5}")
    if bayat:
        print(renk(f"  ⚠  {bayat} bot {BAYAT_ESIK_SN // 60}+ dk'dir yazmiyor "
                   f"(uretim durmus olabilir)", "red", "bold"))
    else:
        print(renk("  ✓ tum botlar guncel yaziyor", "green"))


def db_yaz(teshis):
    bolum("DB SAGLIK")
    if not teshis:
        print(renk("  (db teshis alinamadi)", "red"))
        return
    d = teshis.get("dosya_boyutlari") or {}
    db_mb = d.get("db_mb")
    wal_mb = d.get("wal_mb")
    kuyruk = teshis.get("temizlik_kuyrugu")
    sat = teshis.get("satir_sayilari", {})
    mac = teshis.get("mac_durumlari", {})
    parcalar = []
    if db_mb is not None:
        parcalar.append(f"db {db_mb}MB")
    if wal_mb is not None:
        w_renk = "red" if (wal_mb or 0) > 50 else ("yellow" if (wal_mb or 0) > 10 else "green")
        parcalar.append(f"wal {renk(str(wal_mb) + 'MB', w_renk)}")
    if kuyruk is not None:
        k_renk = "red" if kuyruk > 100000 else ("yellow" if kuyruk > 10000 else "green")
        parcalar.append(f"temizlik kuyrugu {renk(kuyruk, k_renk)}")
    if parcalar:
        print("  " + "  |  ".join(parcalar))
    if sat:
        print(renk("  satirlar: " + "  ".join(
            f"{k}={v}" for k, v in sat.items()), "gray"))
    if mac:
        print(renk("  maclar: " + "  ".join(
            f"{k}={v}" for k, v in mac.items()), "gray"))


def bir_tur(api_base, secret, db_goster):
    veri = {"ozet": None, "kapsama": None, "durum": None}
    teshis = None
    hatalar = []
    for anahtar, ad, yol in (
        ("ozet", "ozet", "/api/admin/panel/ozet"),
        ("kapsama", "kapsama", "/api/admin/panel/veri-kapsama"),
        ("durum", "bot-durum", "/api/admin/panel/bot-durum"),
    ):
        try:
            v = cek(api_base, yol, secret)
            if isinstance(v, dict) and v.get("error") == "yetkisiz":
                print(renk("YETKISIZ - ADMIN_SECRET yanlis ya da tanimli degil.", "red", "bold"))
                sys.exit(2)
            veri[anahtar] = v
        except urllib.error.HTTPError as e:
            hatalar.append(f"{ad}: HTTP {e.code}")
        except Exception as e:  # noqa
            hatalar.append(f"{ad}: {e}")
    ozet, kapsama, durum = veri["ozet"], veri["kapsama"], veri["durum"]
    if db_goster:
        try:
            teshis = cek(api_base, "/api/admin/panel/db-teshis", secret, timeout=30)
        except Exception as e:  # noqa
            hatalar.append(f"db-teshis: {e}")

    if C["reset"]:
        print("\033[2J\033[H", end="")
    simdi = datetime.now().strftime("%H:%M:%S")
    srv = (durum or {}).get("sunucu_saati", "?")
    print(renk(f" MATCHRIX BOT IZLEME ", "bold", "blue")
          + renk(f" yerel {simdi}  |  sunucu {srv}  |  {api_base}", "gray"))
    nabiz_yaz(ozet, kapsama)
    botlar_yaz(durum)
    if db_goster:
        db_yaz(teshis)
    if hatalar:
        bolum("HATALAR")
        for h in hatalar:
            print(renk("  " + h, "red"))


def main():
    p = argparse.ArgumentParser(description="Terminal bot izleme ekrani")
    p.add_argument("--api-base", default=DEFAULT_API_BASE)
    p.add_argument("--aralik", type=int, default=20, help="yenileme araligi (sn)")
    p.add_argument("--once", action="store_true", help="tek sefer bas ve cik")
    p.add_argument("--db-her", type=int, default=3,
                   help="DB teshisini kac turda bir goster (0=hic)")
    args = p.parse_args()

    secret = os.environ.get("ADMIN_SECRET")
    if not secret:
        print("HATA: ADMIN_SECRET ortam degiskeni tanimli degil.\n"
              "  export ADMIN_SECRET=<Railway'deki deger>\n"
              "  python3 bot_izle.py", file=sys.stderr)
        sys.exit(1)

    tur = 0
    try:
        while True:
            db_goster = args.db_her > 0 and (tur % args.db_her == 0)
            try:
                bir_tur(args.api_base, secret, db_goster)
            except SystemExit:
                raise
            except Exception as e:  # noqa
                print(renk(f"tur hatasi: {e}", "red"))
            if args.once:
                break
            print(renk(f"\n  {args.aralik}sn sonra yenilenecek "
                       f"(Ctrl+C ile cik)...", "gray"))
            tur += 1
            time.sleep(args.aralik)
    except KeyboardInterrupt:
        print(renk("\nkapatildi.", "gray"))


if __name__ == "__main__":
    main()
