"""
Sinyal sonuclandirma (settlement) katmani.

NEDEN VAR:
  Onceden bir sinyalin KAZANDI/KAYBETTI durumu her API isteginde, macin O ANKI
  skorundan yeniden hesaplaniyordu. Bunun iki buyuk sakincasi vardi:
    1. Mac verisi degistiginde/eskidiginde gecmis sonuc DEGISIYOR veya KAYBOLUYORDU.
    2. Hicbir egitim verisi birikmiyordu - hangi botun ne kadar isabetli oldugu
       olculemiyordu.

  Artik sonuc bir kez hesaplanip consensus_predictions.outcome sutununa KALICI
  yaziliyor. Sonuclanmis bir kayit bir daha asla degismez.

SONUCLANDIRMA KURALI (mevcut api.py mantiginin birebir tasinmis hali):
  * Sinyal dakikasi <= 45 ise "Ilk Yari" marketidir; SADECE ilk yari sonundaki
    skora bakilir, 2. yari gollerinin etkisi yoktur.
  * Aksi halde "Mac Sonu" marketidir; macin toplam golune bakilir.
  * Hedef: sinyal anindaki toplam golun uzerine EN AZ 1 gol daha gelmesi.
"""
import sqlite3

from db_config import DB_PATH, connect


def _connect():
    return connect()


def ensure_schema():
    """Sonuclandirma icin gereken sutunlari garanti eder (idempotent)."""
    conn = _connect()
    cur = conn.cursor()
    for ddl in (
        "ALTER TABLE consensus_predictions ADD COLUMN signal_minute INTEGER",
        "ALTER TABLE consensus_predictions ADD COLUMN market TEXT",
        "ALTER TABLE consensus_predictions ADD COLUMN initial_goals INTEGER",
    ):
        try:
            cur.execute(ddl)
        except sqlite3.OperationalError:
            pass  # sutun zaten var
    conn.commit()
    conn.close()


def compute_outcome(signal_minute, initial_goals, current_home, current_away,
                    match_status, current_minute, fh_end_home, fh_end_away):
    """Tek bir sinyalin sonucunu hesaplar: 'WON', 'LOST' veya None (henuz belirsiz).

    Saf fonksiyon - veritabanina dokunmaz, bu sayede test edilebilir.
    """
    # Feed takibi mac bitmeden kaybolduysa sonucu bilmiyoruz. Bu bir kayip degil,
    # gozlem disidir; referans snapshot eksik olsa bile VOID karari verilebilir.
    if match_status == "ABANDONED":
        return "VOID"

    if initial_goals is None:
        return None

    is_first_half_market = (signal_minute is not None and signal_minute <= 45)

    if is_first_half_market:
        first_half_over = match_status in ("HT", "FINISHED") or (current_minute or 0) > 45

        if not first_half_over:
            # Ilk yari HALA DEVAM EDIYOR: bu asamada current_home/current_away
            # zaten ilk yari skorunun ta kendisi (2. yari hic baslamadi), bu
            # yuzden hedef gol gelir gelmez WON'a karar vermek GUVENLI - ilk
            # yarinin bitmesini (HT) beklemeye gerek yok.
            total_now = (current_home or 0) + (current_away or 0)
            if total_now > initial_goals:
                return "WON"
            return None  # henuz hedef gol gelmedi, ilk yari surdukce belirsiz

        # Ilk yari KAPANDI: artik SADECE ilk yari sonu skoruna (fh_end) bakiyoruz,
        # current_home/current_away'e degil - aksi halde 2. yaride gelen bir gol
        # bu ilk yari sinyalini yanlislikla WON yapardi (bkz. 2.3 nolu duzeltme).
        ref_home = fh_end_home if fh_end_home is not None else current_home
        ref_away = fh_end_away if fh_end_away is not None else current_away
        total_fh = (ref_home or 0) + (ref_away or 0)
        return "WON" if total_fh > initial_goals else "LOST"

    # Mac sonu marketi
    total_now = (current_home or 0) + (current_away or 0)
    if total_now > initial_goals:
        return "WON"
    if match_status == "FINISHED":
        return "LOST"
    return None  # mac devam ediyor, henuz belli degil


def settle_pending(verbose=True):
    """Sonuclanmamis tum sinyalleri kontrol eder, kesinlesenleri KALICI yazar.

    Zaten outcome'u dolu olan kayitlara DOKUNMAZ.
    Doner: (sonuclanan_sayisi, kazanan, kaybeden)
    """
    ensure_schema()
    conn = _connect()
    cur = conn.cursor()

    cur.execute('''
        SELECT p.id, p.signal_minute, s.home_score, s.away_score,
               m.home_score, m.away_score, m.status, m.minute,
               fh.fh_end_home, fh.fh_end_away, p.initial_goals
        FROM consensus_predictions p
        JOIN matches m ON m.id = p.match_id
        LEFT JOIN live_snapshots s ON s.id = p.snapshot_id
        LEFT JOIN (
            SELECT ls1.match_id, ls1.home_score AS fh_end_home, ls1.away_score AS fh_end_away
            FROM live_snapshots ls1
            INNER JOIN (
                SELECT match_id, MAX(minute) AS max_min
                FROM live_snapshots WHERE minute <= 45 GROUP BY match_id
            ) ls2 ON ls1.match_id = ls2.match_id AND ls1.minute = ls2.max_min
        ) fh ON fh.match_id = m.id
        WHERE p.decision = 'signal' AND p.outcome IS NULL
    ''')
    rows = cur.fetchall()

    settled = won = lost = void = 0
    for (pid, sig_min, snap_h, snap_a, cur_h, cur_a, status, minute,
         fh_h, fh_a, stored_initial) in rows:

        # Sinyal anindaki toplam gol: once kalici sutun, yoksa snapshot'tan
        if stored_initial is not None:
            initial = stored_initial
        elif snap_h is not None and snap_a is not None:
            initial = snap_h + snap_a
        else:
            continue  # referans skor yok, sonuclandiramayiz

        outcome = compute_outcome(sig_min, initial, cur_h, cur_a, status, minute, fh_h, fh_a)
        if outcome is None:
            continue

        cur.execute(
            "UPDATE consensus_predictions "
            "SET outcome = ?, settled_at = CURRENT_TIMESTAMP, initial_goals = ? "
            "WHERE id = ? AND outcome IS NULL",
            (outcome, initial, pid),
        )
        settled += 1
        if outcome == "WON":
            won += 1
        elif outcome == "LOST":
            lost += 1
        else:
            void += 1

    conn.commit()
    conn.close()

    if verbose and settled:
        print(f"[settlement] {settled} sinyal sonuclandi "
              f"(kazanan={won} kaybeden={lost} gecersiz={void})", flush=True)
    return settled, won, lost, void
