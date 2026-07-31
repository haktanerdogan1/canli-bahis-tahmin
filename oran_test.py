#!/usr/bin/env python3
"""
Oran ucu kesif scripti.

Ne yapar:
  1. RapidAPI'den o an CANLI olan maclari ceker
  2. Ilk birkac macin ID'sini alir
  3. Her mac icin farkli ulke kodlariyla oran ucunu dener
  4. Sonuclari oran_ciktilari/ klasorune kaydeder ve ozet basar

Kullanim:
    python3 oran_test.py ANAHTARIN
"""
import json
import os
import sys
import urllib.request

HOST = "free-api-live-football-data.p.rapidapi.com"
ULKELER = ["BR", "GB", "DE", "ES", "IT", "NG", "IN", "US"]
CIKTI = "oran_ciktilari"


def cek(yol, anahtar):
    req = urllib.request.Request(
        f"https://{HOST}{yol}",
        headers={"x-rapidapi-host": HOST, "x-rapidapi-key": anahtar},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def main():
    if len(sys.argv) >= 2:
        anahtar = sys.argv[1].strip()
    else:
        anahtar = input("RapidAPI anahtarini yapistir ve Enter'a bas: ").strip()
    if not anahtar:
        print("Anahtar bos - cikiliyor.")
        sys.exit(1)
    os.makedirs(CIKTI, exist_ok=True)

    print("Canli maclar cekiliyor...")
    d = cek("/football-current-live", anahtar)
    r = d.get("response", {})
    maclar = r.get("live") or r.get("matches") or []
    if not maclar:
        print(">>> Su an CANLI MAC YOK (sabahin bu saatinde normal).")
        print(">>> Gunduz/aksam tekrar calistir.")
        return

    secili = maclar[:3]
    print(f"{len(maclar)} canli mac bulundu, ilk {len(secili)} tanesi denenecek:\n")
    for m in secili:
        h = m.get("home", {}).get("name", "?")
        a = m.get("away", {}).get("name", "?")
        print(f"   id={m['id']}  {h} - {a}")

    print("\nOran ucu deneniyor...\n")
    bulunan = []
    for m in secili:
        mid = m["id"]
        for cc in ULKELER:
            try:
                o = cek(f"/football-event-odds?eventid={mid}&countrycode={cc}", anahtar)
            except Exception as e:
                print(f"   {mid} {cc}: HATA {e}")
                continue

            with open(f"{CIKTI}/oran_{mid}_{cc}.json", "w", encoding="utf-8") as f:
                json.dump(o, f, ensure_ascii=False, indent=2)

            odds = (o.get("response") or {}).get("odds")
            if not odds:
                print(f"   {mid} {cc}: bos")
                continue

            saglayici = odds.get("persistentKey", "?")
            ic = (odds.get("odds") or {})
            marketler = list(ic.keys())
            secimler = (ic.get("resolvedOddsMarket") or {}).get("selections", [])
            isimler = [s.get("name") for s in secimler]
            print(f"   {mid} {cc}: VAR -> {saglayici} | marketler={marketler} | secimler={isimler}")
            bulunan.append((mid, cc, saglayici, marketler, isimler))

    print("\n" + "=" * 60)
    if bulunan:
        print(f"Oran donen kombinasyon: {len(bulunan)}")
        tum_secimler = set()
        for *_, isimler in bulunan:
            tum_secimler.update(i for i in isimler if i)
        print(f"Gorulen tum secim adlari: {sorted(tum_secimler)}")
        print("\n>>> Bunlarda '1','X','2' disinda bir sey varsa (Over/Under gibi) HABER VER.")
    else:
        print("Hicbir ulke kodunda oran donmedi.")
    print(f"\nTum ciktilar: {CIKTI}/ klasorunde")


if __name__ == "__main__":
    main()
