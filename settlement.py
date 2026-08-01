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

GHOST_LOSS_MIGRATION = "2026_07_31_early_finished_losses_to_void"
# Bir sinyal hicbir zaman sonsuza kadar PENDING kalamaz - KOK NEDENDEN BAGIMSIZ
# mutlak bir zaman siniri. Normal akiste her sinyal match tracking (last_seen_at/
# last_progress_at/grace-period) uzerinden cok daha erken WON/LOST/VOID'e ulasir;
# bu sadece o katmanda HENUZ BULUNAMAMIS bir bug (restart zamanlamasi, orphan
# kayit, RapidAPI kesintisi vb.) sinyali askida biraktiginda devreye giren bir
# son care. 3 saat, en uzun mac (uzatmalar+penaltilar) suresinin bile kat kat
# uzerinde - gercek bir mac hala oynaniyor olamayacak kadar cok.
SIGNAL_TIMEOUT_HOURS = 3


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


def backfill_ghost_losses(verbose=True):
    """Erken takip kaybi nedeniyle LOST yazilmis kayitlari bir kez VOID yapar.

    Bu, outcome degismezligi kuralinin genel bir istisnasi degildir. Yalnizca
    kullanici tarafindan dogrulanan eski bugun dar kosuluna uygulanir ve migration
    anahtari sayesinde ayni veritabaninda ikinci kez calismaz.
    """
    conn = _connect()
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS data_migrations (
            migration_key TEXT PRIMARY KEY,
            applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            affected_rows INTEGER NOT NULL DEFAULT 0
        )
    ''')
    if cur.execute("SELECT 1 FROM data_migrations WHERE migration_key=?",
                   (GHOST_LOSS_MIGRATION,)).fetchone():
        conn.close()
        return 0

    # Once maclari isaretle; ardindan yalnizca bu maclarin hatali LOST
    # sinyallerini VOID'e cevir. WON/PENDING ve 85+ dakika kayitlari korunur.
    cur.execute('''
        UPDATE matches
        SET status='ABANDONED'
        WHERE status='FINISHED' AND COALESCE(minute, 0) < 85
          AND EXISTS (
              SELECT 1 FROM consensus_predictions p
              WHERE p.match_id=matches.id AND p.decision='signal' AND p.outcome='LOST'
          )
    ''')
    cur.execute('''
        UPDATE consensus_predictions
        SET outcome='VOID'
        WHERE decision='signal' AND outcome='LOST'
          AND match_id IN (
              SELECT id FROM matches
              WHERE status='ABANDONED' AND COALESCE(minute, 0) < 85
          )
    ''')
    affected = cur.rowcount
    cur.execute("INSERT INTO data_migrations(migration_key, affected_rows) VALUES(?,?)",
                (GHOST_LOSS_MIGRATION, affected))
    conn.commit()
    conn.close()
    if verbose:
        print(f"[settlement] hayalet kayip backfill: {affected} kayit VOID yapildi", flush=True)
    return affected


def finalize_fully_settled_matches(verbose=True):
    """Butun sinyalleri sonuclanmis (hic PENDING kalmayan) maclarin durumunu
    FINISHED yapar - v4_api_bot'un o an ne rapor ettiginden BAGIMSIZ.

    NEDEN GEREKLI: bir sinyalin outcome'u KALICI yazildiktan sonra bile,
    o sinyalin bagli oldugu matches satirinin 'status' alani LIVE/HT'de
    TAKILI kalabiliyordu (v4_api_bot RapidAPI'nin o an ne dondugune gore
    surekli yeniden yaziyor). RapidAPI ara sira BIR ONCEKI GUNDEN kalma
    maclari feed'e tasiyor (bkz. _plausibly_current_live_match docstring);
    boyle bir mac hala status='LIVE' oldugu icin restart-guvenlik bypass'i
    (tracked_ids) onu her deploy'da "zaten canli, onaya gerek yok" diye
    ANINDA guvenip taze tutuyordu - dunku bir Konferans Ligi maci boylece
    her restart'ta ekrana geri donuyordu.

    Butun sinyalleri sonuclanmis bir macin GERCEKTE hala oynanip oynanmadigi
    onemsiz - hicbir PENDING sinyal onun 'live' durumuna bagli degil. Statusu
    FINISHED yaparak onu tracked_ids bypass'inin disina cikariyoruz.
    """
    conn = _connect()
    cur = conn.cursor()
    cur.execute("""
        UPDATE matches SET status='FINISHED'
        WHERE status NOT IN ('FINISHED','ABANDONED','Ended','FT','Canceled')
          AND id IN (SELECT DISTINCT match_id FROM consensus_predictions WHERE decision='signal')
          AND NOT EXISTS (
              SELECT 1 FROM consensus_predictions p2
              WHERE p2.match_id = matches.id AND p2.decision='signal' AND p2.outcome IS NULL
          )
    """)
    affected = cur.rowcount
    conn.commit()
    conn.close()
    if verbose and affected:
        print(f"[settlement] {affected} mac tum sinyalleri sonuclandigi icin FINISHED yapildi "
              "(restart-bypass'ten cikarildi)", flush=True)
    return affected


def void_timed_out_signals(verbose=True):
    """GUVENLIK AGI: SIGNAL_TIMEOUT_HOURS'tan uzun sure PENDING kalan her sinyali
    VOID yapar - matches tablosuna, JOIN'e veya baska hicbir seye BAGIMLI DEGIL.

    NEDEN GEREKLI: settle_pending() consensus_predictions'i matches ile JOIN
    ederek calisir; o katmanda (restart zamanlamasi, feed kesintisi, henuz
    bulunamamis bir baska bug) sinyalin sonsuza kadar PENDING kalmasina yol
    acan bir sorun cikarsa, kullanici o sinyali ASLA bir sonuca ulasmis
    gormez - ekrandan sessizce kaybolur. Bu fonksiyon KOK NEDENDEN BAGIMSIZ
    calisir: sadece consensus_predictions.created_at'e bakar, hicbir JOIN
    yapmaz - o yuzden matches tablosunda ne olursa olsun calismaya devam eder.
    """
    conn = _connect()
    cur = conn.cursor()
    cur.execute(f'''
        UPDATE consensus_predictions
        SET outcome='VOID', settled_at=CURRENT_TIMESTAMP
        WHERE decision='signal' AND outcome IS NULL
          AND created_at <= datetime('now', '-{SIGNAL_TIMEOUT_HOURS} hours')
    ''')
    affected = cur.rowcount
    conn.commit()
    conn.close()
    if verbose and affected:
        print(f"[settlement] guvenlik agi: {affected} sinyal {SIGNAL_TIMEOUT_HOURS} saatten "
              "uzun PENDING kaldigi icin VOID yapildi", flush=True)
    return affected


STUCK_SIGNAL_MINUTES = 30
STUCK_PROGRESS_BUFFER = 5


def void_stuck_signals(verbose=True):
    """GUVENLIK AGI 2: last_progress_at'in KOSULLU CASE guncellemesine hicbir
    sekilde BAGIMLI OLMADAN calisan, en basit mumkun kontrol.

    NEDEN GEREKLI: _close_stale_progress() (v4_api_bot.py) matches.last_progress_at
    sutununa dayaniyor - bu sutun sadece "dakika gercekten degisti" algilandiginda
    guncelleniyor, koşullu bir CASE ifadesiyle. Bu zincirde herhangi bir yerde
    (henuz tespit edilememis) bir sorun olursa, mac saatlerce ayni dakikada
    "canli" gorunmeye devam edebiliyor ve sinyal hic sonuclanmiyor. Bu fonksiyon
    o zincire hic girmiyor: sadece SIGNAL'IN NE ZAMAN OLUSTURULDUGUNU (created_at)
    ve MACIN O ANKI dakika DEGERINI (matches.minute, dogrudan okuma - hicbir
    kosullu guncelleme mantigina tabi degil) karsilastiriyor.

    ONEMLI: sabit bir dakika esigi (orn. "hep <20 ise") KULLANMIYORUZ - cunku
    donuk kalinan dakika 0 da olabilir, 22 de, 52 de (bkz. Koper dk=7,
    Katowice dk=22, Ludogorets dk=52 - hepsi ayni bug'in farkli anlik
    goruntuleri). Bunun yerine sinyalin OLUSTUGU ANDAKI dakikayi (signal_minute)
    referans aliyoruz: 30+ gercek dakika gecmis olmasina ragmen macin guncel
    dakikasi hala o referansin (+5 dakikalik tolerans) UZERINE CIKMAMISSA,
    feed bu fikstur icin donmus demektir - mutlak degeri onemli degil.
    """
    conn = _connect()
    cur = conn.cursor()
    cur.execute(f'''
        UPDATE consensus_predictions
        SET outcome='VOID', settled_at=CURRENT_TIMESTAMP
        WHERE id IN (
            SELECT p.id FROM consensus_predictions p
            JOIN matches m ON m.id = p.match_id
            WHERE p.decision='signal' AND p.outcome IS NULL
              AND p.created_at <= datetime('now', '-{STUCK_SIGNAL_MINUTES} minutes')
              AND p.signal_minute IS NOT NULL
              AND COALESCE(m.minute, 0) <= p.signal_minute + {STUCK_PROGRESS_BUFFER}
        )
    ''')
    affected = cur.rowcount
    conn.commit()
    conn.close()
    if verbose and affected:
        print(f"[settlement] guvenlik agi 2: {affected} sinyal {STUCK_SIGNAL_MINUTES}dk+ once "
              "olusturuldu ama macin dakikasi o zamandan beri ilerlemedigi icin "
              "VOID yapildi", flush=True)
    return affected


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
        LEFT JOIN matches m ON m.id = p.match_id
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
