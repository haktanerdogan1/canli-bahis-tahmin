"""football-data.co.uk formatindaki (Div,Date,Time,HomeTeam,AwayTeam,FTHG,FTAG,
HTHG,HTAG,B365H,B365D,B365A,B365>2.5,B365<2.5,...) hazir mac CSV'lerini okuyup
Railway'deki KALICI arsive (/api/admin/archive-csv-import) gonderir.

NEDEN AYRI SCRIPT: bu makinedeki database/fh_goal_predictor.db sadece YEREL
tohum dosyasi - Railway kendi Volume'unda ayri bir veritabani tutuyor (bkz.
db_config.py). O yuzden dogrudan sqlite3.connect() ile yazmak degil, HTTP
uzerinden production'a POST etmek gerekiyor (iddaa_odds_client.py ile ayni
ilke).

KULLANIM:
    export BACKUP_SECRET=<Railway'deki BACKUP_SECRET degeri>
    python3 import_csv_archive.py /Users/sebnem/Downloads/T1.csv --league "Türkiye Süper Lig"
"""
import argparse
import csv
import os
import sys

import requests

DEFAULT_API_BASE = "https://web-production-f1dba.up.railway.app"
BATCH_SIZE = 200


def parse_row(row):
    try:
        fthg = int(row["FTHG"])
        ftag = int(row["FTAG"])
    except (KeyError, ValueError, TypeError):
        return None

    def flt(key):
        v = row.get(key)
        try:
            return float(v) if v not in (None, "") else None
        except ValueError:
            return None

    return {
        "home": row.get("HomeTeam", "").strip(),
        "away": row.get("AwayTeam", "").strip(),
        "date": row.get("Date", "").strip(),
        "time": row.get("Time", "").strip(),
        "fthg": fthg,
        "ftag": ftag,
        "hthg": row.get("HTHG"),
        "htag": row.get("HTAG"),
        "odds_h": flt("B365H"),
        "odds_d": flt("B365D"),
        "odds_a": flt("B365A"),
        "odds_o25": flt("B365>2.5"),
        "odds_u25": flt("B365<2.5"),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv_path")
    ap.add_argument("--league", default="", help="matches.league_name kolonuna yazilacak deger")
    ap.add_argument("--api-base", default=DEFAULT_API_BASE)
    args = ap.parse_args()

    secret = os.environ.get("BACKUP_SECRET")
    if not secret:
        print("BACKUP_SECRET ortam degiskeni gerekli.", file=sys.stderr)
        sys.exit(1)

    with open(args.csv_path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = [parse_row(r) for r in reader]
        rows = [r for r in rows if r]

    print(f"[import_csv_archive] {len(rows)} gecerli mac satiri okundu: {args.csv_path}")

    toplam_yeni = 0
    toplam_var = 0
    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i:i + BATCH_SIZE]
        resp = requests.post(
            f"{args.api_base}/api/admin/archive-csv-import",
            headers={"x-backup-secret": secret},
            json={"league_name": args.league, "matches": batch},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        toplam_yeni += data.get("yeni", 0)
        toplam_var += data.get("zaten_vardi", 0)
        print(f"  batch {i // BATCH_SIZE + 1}: yeni={data.get('yeni')} zaten_vardi={data.get('zaten_vardi')}")

    print(f"[import_csv_archive] Bitti. Toplam yeni={toplam_yeni}, zaten arsivde olan={toplam_var}")


if __name__ == "__main__":
    main()
