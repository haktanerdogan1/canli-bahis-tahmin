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
import time

from db_config import DB_PATH, connect

# Ilk yari icin dogrulanmis bir HT status_id yok (bkz. thesports_bot.py basi
# aciklama - sadece LIVE=2 ve FINISHED=8 kanitli). Bu yuzden status'e dayanarak
# "ilk yari bitti mi" sorusu bazen HICBIR ZAMAN cevaplanamiyor, sinyal mac
# TAMAMEN bitene kadar PENDING kalirdi. SAFE_FIRST_HALF_OVER_SECONDS, uzatmalar
# dahil ilk yarinin ve devre arasinin baslangicinin rahatlikla sigdigi, ama
# 2. yarinin (tipik olarak kickoff+60-65dk) henuz baslamadigi guvenli bir
# zaman penceresi - bu noktadan sonra fh_end icin "en son gorulen skor"a
# guvenmek, sinyali saatlerce gereksiz PENDING birakmaktan daha dogru.
SAFE_FIRST_HALF_OVER_SECONDS = 50 * 60

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


PREMATURE_FH_LOSS_MIGRATION = "2026_08_02_premature_fh_losses_to_won"


def backfill_premature_fh_losses(verbose=True):
    """Erken ilk-yari kapanisi yuzunden yanlislikla LOST yazilmis kayitlari
    bir kez WON'a cevirir.

    Bu, outcome degismezligi kuralinin genel bir istisnasi degildir - sadece
    KANITLANMIS, dar bir bug deseninin duzeltilmesidir (bkz. compute_outcome
    icindeki first_half_over aciklamasi): bazi API fiksturlerinde canli
    dakika sayaci gercek devre arasindan (status='HT') ONCE 45'i asiyor,
    eski kod bunu "ilk yari bitti" saniyor ve o anki eksik/gecici skorla
    LOST'a karar veriyordu - oysa API birkac dakika sonra "resmi" (dakika=45'e
    sabitlenmis) devre arasi skorunu daha yuksek golle gonderiyordu.

    Sadece SOMUT KANITI olan kayitlar duzeltiliyor: settled_at'ten SONRA
    gelen, dakika<=45 bir snapshot, o an kullanilan initial_goals esiginden
    DAHA YUKSEK toplam gol gosteriyorsa. Migration anahtari sayesinde ayni
    veritabaninda ikinci kez calismaz.
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
                   (PREMATURE_FH_LOSS_MIGRATION,)).fetchone():
        conn.close()
        return 0

    cur.execute('''
        UPDATE consensus_predictions
        SET outcome='WON'
        WHERE decision='signal' AND outcome='LOST' AND signal_minute <= 45
          AND EXISTS (
              SELECT 1 FROM live_snapshots ls
              WHERE ls.match_id = consensus_predictions.match_id
                AND ls.minute <= 45
                AND ls.captured_at > consensus_predictions.settled_at
                AND (COALESCE(ls.home_score,0) + COALESCE(ls.away_score,0))
                    > consensus_predictions.initial_goals
          )
    ''')
    affected = cur.rowcount
    cur.execute("INSERT INTO data_migrations(migration_key, affected_rows) VALUES(?,?)",
                (PREMATURE_FH_LOSS_MIGRATION, affected))
    conn.commit()
    conn.close()
    if verbose:
        print(f"[settlement] erken IY kapanisi backfill: {affected} kayit WON yapildi", flush=True)
    return affected


FALSE_VOID_STALE_PROGRESS_MIGRATION = "2026_08_05_false_void_stale_progress_to_real_outcome"


def backfill_false_void_stale_progress(verbose=True):
    """_close_stale_progress()'in dakika donmasini (status_id dogrulanamadigi
    icin) mac gercekten durmus sanmasi yuzunden yanlislikla VOID yazilmis
    kayitlari, macin GERCEK nihai verisinden hesaplanan dogru sonuca (WON ya
    da LOST) cevirir.

    Bu, outcome degismezligi kuralinin genel bir istisnasi degildir - GHOST_LOSS
    ve PREMATURE_FH_LOSS migration'lariyla AYNI dar desen: sadece SOMUT KANITI
    olan kayitlar (outcome='VOID' + matches.status GERCEKTEN 'FINISHED' olmus -
    yani mac hicbir zaman gercekten terk edilmemis, thesports_bot/v4_api_bot
    onu daha sonra dogru sekilde bitmis olarak guncellemis) duzeltiliyor, ve
    KOR KOR 'LOST' yazilmiyor - compute_outcome() ile ayni mantik, ayni kod
    yolu kullanilarak GERCEK sonuc (WON ya da LOST) hesaplanip yaziliyor.
    Migration anahtari sayesinde ayni veritabaninda ikinci kez calismaz.
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
                   (FALSE_VOID_STALE_PROGRESS_MIGRATION,)).fetchone():
        conn.close()
        return 0

    cur.execute('''
        SELECT p.id, p.signal_minute, p.initial_goals, m.home_score, m.away_score,
               m.status, m.minute, fh.fh_end_home, fh.fh_end_away
        FROM consensus_predictions p
        JOIN matches m ON m.id = p.match_id
        LEFT JOIN (
            SELECT ls1.match_id, ls1.home_score AS fh_end_home, ls1.away_score AS fh_end_away
            FROM live_snapshots ls1
            WHERE ls1.id = (
                SELECT ls2.id FROM live_snapshots ls2
                WHERE ls2.match_id = ls1.match_id AND ls2.minute <= 45
                ORDER BY ls2.minute DESC, ls2.id DESC
                LIMIT 1
            )
        ) fh ON fh.match_id = m.id
        WHERE p.decision = 'signal' AND p.outcome = 'VOID' AND m.status = 'FINISHED'
    ''')
    rows = cur.fetchall()

    affected = 0
    for (pid, sig_min, init_g, hs, aws, status, minute, fh_h, fh_a) in rows:
        outcome = compute_outcome(sig_min, init_g, hs, aws, status, minute, fh_h, fh_a)
        if outcome in ("WON", "LOST"):
            cur.execute(
                "UPDATE consensus_predictions SET outcome=?, settled_at=CURRENT_TIMESTAMP "
                "WHERE id=? AND outcome='VOID'",
                (outcome, pid),
            )
            affected += cur.rowcount

    cur.execute("INSERT INTO data_migrations(migration_key, affected_rows) VALUES(?,?)",
                (FALSE_VOID_STALE_PROGRESS_MIGRATION, affected))
    conn.commit()
    conn.close()
    if verbose:
        print(f"[settlement] yanlis VOID duzeltmesi: {affected} kayit gercek "
              "sonucuna (WON/LOST) cevrildi", flush=True)
    return affected


VOID_RECONSIDER_MIGRATION = "2026_08_05_void_reconsidered_after_abandoned_shortcircuit_fix"
# _close_stale_missing() ile ayni esik (thesports_bot.py'den bagimsiz kalmak
# icin burada ayrica tanimli - import etmek THESPORTS_USER/SECRET zorunlulugunu
# settlement.py'ye bulastirirdi).
RECENTLY_SEEN_MINUTES = 10


def backfill_void_reconsidered(verbose=True):
    """Iki AYRI kanitlanmis hatanin etkiledigi VOID kayitlarini duzeltir:

      1. compute_outcome() ABANDONED kontrolunu koşulsuz en basta yapiyordu -
         hedef gol SIGNAL ANINDAN SONRA zaten gerceklesmis (WON olmasi
         gereken) sinyaller bile VOID yaziliyordu (bkz. Cultural
         Leonesa-Real Aviles, Ponferradina-Unionistas). Bu fonksiyon DUZELTILMIS
         compute_outcome() ile GERCEK mac verisinden yeniden hesaplar; WON/LOST
         cikarsa yazar.
      2. _close_stale_progress() dakika/skor donuk kaldiginda maci ABANDONED
         yapiyordu, mac GERCEKTEN hala TheSports feed'inde gorunuyor olsa bile
         (bkz. Cesena-Vis Pesaro). Recompute hala belirsiz (None) donuyorsa VE
         mac hala TAZE gorulmusse (last_seen_at RECENTLY_SEEN_MINUTES icinde),
         bu VOID'in de YANLIS oldugu kanitlanmis demektir - ama gercek sonuc
         henuz belli olmadigi icin WON/LOST yazilamaz; outcome NULL'a
         (PENDING) geri aciliyor ki normal settle_pending() akisi macin
         gercek sonucuna ulastiginda onu doğru sekilde sonuclandirsin.

    Migration anahtari sayesinde ayni veritabaninda ikinci kez calismaz.
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
                   (VOID_RECONSIDER_MIGRATION,)).fetchone():
        conn.close()
        return 0, 0

    cur.execute('''
        SELECT p.id, p.signal_minute, p.initial_goals, m.id, m.home_score, m.away_score,
               m.status, m.minute, m.kickoff_ts,
               (julianday('now') - julianday(m.last_seen_at)) * 1440 AS dk_once_gorulmus,
               fh.fh_end_home, fh.fh_end_away, fh.fh_end_minute
        FROM consensus_predictions p
        JOIN matches m ON m.id = p.match_id
        LEFT JOIN (
            SELECT ls1.match_id, ls1.home_score AS fh_end_home, ls1.away_score AS fh_end_away,
                   ls1.minute AS fh_end_minute
            FROM live_snapshots ls1
            WHERE ls1.id = (
                SELECT ls2.id FROM live_snapshots ls2
                WHERE ls2.match_id = ls1.match_id AND ls2.minute <= 45
                ORDER BY ls2.minute DESC, ls2.id DESC
                LIMIT 1
            )
        ) fh ON fh.match_id = m.id
        WHERE p.decision = 'signal' AND p.outcome = 'VOID'
    ''')
    rows = cur.fetchall()

    fixed = reopened = 0
    now_ts = time.time()
    for (pid, sig_min, init_g, match_id_db, hs, aws, status, minute, kickoff_ts,
         dk_once_gorulmus, fh_h, fh_a, fh_minute) in rows:

        if (not fh_minute) and kickoff_ts and sig_min is not None and sig_min <= 45:
            t_h, t_a = _time_based_fh_end(cur, match_id_db, kickoff_ts)
            if t_h is not None:
                fh_h, fh_a = t_h, t_a

        outcome = compute_outcome(sig_min, init_g, hs, aws, status, minute, fh_h, fh_a,
                                  kickoff_ts=kickoff_ts, now=now_ts)

        if outcome in ("WON", "LOST"):
            cur.execute(
                "UPDATE consensus_predictions SET outcome=?, settled_at=CURRENT_TIMESTAMP "
                "WHERE id=? AND outcome='VOID'",
                (outcome, pid),
            )
            fixed += cur.rowcount
        elif (outcome in (None, "VOID") and status == "ABANDONED"
              and dk_once_gorulmus is not None and dk_once_gorulmus <= RECENTLY_SEEN_MINUTES):
            cur.execute(
                "UPDATE consensus_predictions SET outcome=NULL, settled_at=NULL "
                "WHERE id=? AND outcome='VOID'",
                (pid,),
            )
            reopened += cur.rowcount

    cur.execute("INSERT INTO data_migrations(migration_key, affected_rows) VALUES(?,?)",
                (VOID_RECONSIDER_MIGRATION, fixed + reopened))
    conn.commit()
    conn.close()
    if verbose:
        print(f"[settlement] VOID yeniden degerlendirme: {fixed} kayit gercek "
              f"sonuca (WON/LOST) cevrildi, {reopened} kayit hala canli oldugu "
              "kanitlandigi icin PENDING'e geri acildi", flush=True)
    return fixed, reopened


def finalize_fully_settled_matches(verbose=True):
    """ARTIK ORCHESTRATOR'DAN CAGRILMIYOR (2026-08-24, bkz. app/core/orchestrator.py
    yorum satirlari) - referans/rollback icin kod tabaninda duruyor, v4_api_bot.py
    ile ayni ilke. NEDEN KAPATILDI: bu fonksiyonun matches.status='FINISHED'
    yazmasi, frontend'in "MS" etiketini DOGRUDAN bu alandan okumasi yuzunden
    hala GERCEKTEN canli olan maclari erken bitmis gosteriyordu (kullanici
    raporu: "sonuçlar ekranında kazanan/kaybeden mac devam ederken bile MS
    yaziyor"). Asagidaki orijinal gerekce artik GECERSIZ - hedef aldigi
    v4_api_bot.py (RapidAPI) zaten calistirilmiyor (bkz. supervisor.py),
    bugunku kaynaklarin (fs_/7m_) tracked_ids tarzi bir restart-bypass'i yok.
    Eger ileride yeniden acilirsa, matches.status yerine AYRI bir kolona
    (ör. sinyaller_kilitli TIMESTAMP) yazmali - boylece frontend'in gercek
    mac durumunu gosteren alan bundan etkilenmez.

    Butun sinyalleri sonuclanmis (hic PENDING kalmayan) maclarin durumunu
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


def void_stuck_signals(verbose=True):
    """GUVENLIK AGI 2: last_progress_at'in KOSULLU CASE guncellemesine VE
    consensus_predictions.signal_minute'un dolu olmasina hicbir sekilde
    BAGIMLI OLMADAN calisan kontrol.

    NEDEN GEREKLI: _close_stale_progress() (v4_api_bot.py) matches.last_progress_at
    sutununa dayaniyor - bu sutun sadece "dakika gercekten degisti" algilandiginda
    guncelleniyor, koşullu bir CASE ifadesiyle. Bu zincirde bir sorun cikarsa
    mac saatlerce ayni dakikada "canli" gorunmeye devam edebiliyor. Ilk
    versiyonum (signal_minute'e gore ilerleme kontrolu) bu 5 mac icin dahi
    calismadi cunku bu satirlar signal_minute kolonu eklenmeden ONCE atilmis
    olabilir (ALTER TABLE ADD COLUMN mevcut satirlarda NULL birakir) - o
    yuzden signal_minute IS NOT NULL sartina takilip hic tetiklenmediler.

    Bu versiyon hicbir saklanan/kosullu sutuna guvenmiyor: live_snapshots
    tablosundaki HAM captured_at zaman damgalarina bakarak "son 30 gercek
    dakikada bu macin dakikasi GERCEKTEN degisti mi" sorusunu HER SEFERINDE
    yeniden hesapliyor. matches.minute (kosullu guncellenen bir sutun) ile
    degil, o anki matches.minute degerine kiyasla farkli bir minute tasiyan
    bir snapshot son 30 dakikada var mi diye bakiyor - yoksa donuk demektir.
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
              AND NOT EXISTS (
                  SELECT 1 FROM live_snapshots ls
                  WHERE ls.match_id = m.id
                    AND ls.captured_at >= datetime('now', '-{STUCK_SIGNAL_MINUTES} minutes')
                    AND COALESCE(ls.minute, -999) <> COALESCE(m.minute, -999)
              )
        )
    ''')
    affected = cur.rowcount
    conn.commit()
    conn.close()
    if verbose and affected:
        print(f"[settlement] guvenlik agi 2: {affected} sinyal {STUCK_SIGNAL_MINUTES}dk+ once "
              "olusturuldu ama son snapshot gecmisine gore macin dakikasi hic "
              "degismedigi icin VOID yapildi", flush=True)
    return affected


def compute_outcome(signal_minute, initial_goals, current_home, current_away,
                    match_status, current_minute, fh_end_home, fh_end_away,
                    kickoff_ts=None, now=None):
    """Tek bir sinyalin sonucunu hesaplar: 'WON', 'LOST' veya None (henuz belirsiz).

    Saf fonksiyon - veritabanina dokunmaz, bu sayede test edilebilir. kickoff_ts/now
    sadece HT status'u hicbir zaman dogrulanamayan kaynaklar (bkz.
    SAFE_FIRST_HALF_OVER_SECONDS) icin saat-bazli yedek karara girdi olur; caller
    fh_end_home/away'i de bu duruma uygun (saat-bazli secilmis) gecirmelidir.
    """
    # NOT (onceki hata): ABANDONED kontrolu BURADA, en basta ve kosulsuz
    # yapiliyordu - bu, hedef gol SIGNAL ANINDAN SONRA zaten gerceklesmis
    # (dolayisiyla WON olmasi gereken) bir sinyali bile VOID'e ceviriyordu
    # (bkz. Cultural Leonesa-Real Aviles: mac 1-1 bitti, hedef "1.5 ust"
    # zaten gecilmisti, ama takip ABANDONED'a dustugu icin VOID yazildi).
    # ABANDONED artik sadece asagida, WON zaten belirlenemediyse VOID
    # dondurmek icin kullaniliyor - "kayip takip" gercekten BELIRSIZ
    # durumlar icindir, zaten kazanilmis bir sinyali silmek icin degil.

    if initial_goals is None:
        return None

    is_first_half_market = (signal_minute is not None and signal_minute <= 45)

    if is_first_half_market:
        # NOT: eskiden "(current_minute or 0) > 45" da ilk yarinin bittigine
        # kanit sayiliyordu. Bazi fiksturlerde API canli dakika sayacini 45'in
        # UZERINE cikariyor (46, 47, 48...) - gercek devre arasi (status='HT')
        # onaylanmadan ONCE - ve gecikmeli olarak "resmi" devre arasi skorunu
        # (dakika=45'e geri sabitlenmis) birkac dakika sonra gonderiyor. Erken
        # "minute>45" varsayimi, o resmi skor gelmeden fh_end'i erken/eksik
        # bir anlik goruntuyle kilitleyip yanlislikla LOST'a karar veriyordu
        # (bkz. Ludogorets-Botev Vratsa: gercek IY sonu 2-0 iken sistem
        # dakika 46'da hala 1-0 olan eski veriyle LOST yazmisti). Artik SADECE
        # acik status='HT'/'FINISHED' sinyaline guveniliyor; boyle bir sinyal
        # hic gelmezse zaten void_stuck_signals() 30dk sonra devreye girer.
        first_half_over = match_status in ("HT", "FINISHED")

        if not first_half_over and kickoff_ts:
            _now = now if now is not None else time.time()
            if (_now - kickoff_ts) >= SAFE_FIRST_HALF_OVER_SECONDS:
                first_half_over = True

        if not first_half_over:
            # Ilk yari HALA DEVAM EDIYOR: bu asamada current_home/current_away
            # zaten ilk yari skorunun ta kendisi (2. yari hic baslamadi), bu
            # yuzden hedef gol gelir gelmez WON'a karar vermek GUVENLI - ilk
            # yarinin bitmesini (HT) beklemeye gerek yok.
            total_now = (current_home or 0) + (current_away or 0)
            if total_now > initial_goals:
                return "WON"
            if match_status == "ABANDONED":
                return "VOID"
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
    if match_status == "ABANDONED":
        return "VOID"
    return None  # mac devam ediyor, henuz belli degil


def _time_based_fh_end(cur, match_id_db, kickoff_ts):
    """Dakika etiketine guvenilmedigi durumlar icin (bkz. SAFE_FIRST_HALF_OVER_SECONDS)
    kickoff+50dk civarinda gercekte YAKALANMIS son snapshot'un skorunu doner -
    hicbir snapshot o zamandan once yoksa (None, None)."""
    hedef = time.strftime(
        "%Y-%m-%d %H:%M:%S", time.gmtime(kickoff_ts + SAFE_FIRST_HALF_OVER_SECONDS)
    )
    cur.execute('''
        SELECT home_score, away_score FROM live_snapshots
        WHERE match_id = ? AND captured_at <= ?
        ORDER BY captured_at DESC LIMIT 1
    ''', (match_id_db, hedef))
    row = cur.fetchone()
    return (row[0], row[1]) if row else (None, None)


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
               fh.fh_end_home, fh.fh_end_away, fh.fh_end_minute, p.initial_goals,
               m.id, m.kickoff_ts
        FROM consensus_predictions p
        LEFT JOIN matches m ON m.id = p.match_id
        LEFT JOIN live_snapshots s ON s.id = p.snapshot_id
        LEFT JOIN (
            -- Ayni dakikada (orn. API'nin gecikmeli "resmi" HT skorunu
            -- gonderdigi durumda) birden fazla satir varsa EN SON eklenen
            -- (en yuksek id) tercih edilir - aksi halde match_id basina
            -- birden fazla satir donup disaridaki sorguda satir cogalmasina
            -- (fan-out) yol acardi.
            SELECT ls1.match_id, ls1.home_score AS fh_end_home, ls1.away_score AS fh_end_away,
                   ls1.minute AS fh_end_minute
            FROM live_snapshots ls1
            WHERE ls1.id = (
                SELECT ls2.id FROM live_snapshots ls2
                WHERE ls2.match_id = ls1.match_id AND ls2.minute <= 45
                ORDER BY ls2.minute DESC, ls2.id DESC
                LIMIT 1
            )
        ) fh ON fh.match_id = m.id
        WHERE p.decision = 'signal' AND p.outcome IS NULL
    ''')
    rows = cur.fetchall()

    settled = won = lost = void = 0
    now_ts = time.time()
    for (pid, sig_min, snap_h, snap_a, cur_h, cur_a, status, minute,
         fh_h, fh_a, fh_minute, stored_initial, match_id_db, kickoff_ts) in rows:

        # Sinyal anindaki toplam gol: once kalici sutun, yoksa snapshot'tan
        if stored_initial is not None:
            initial = stored_initial
        elif snap_h is not None and snap_a is not None:
            initial = snap_h + snap_a
        else:
            continue  # referans skor yok, sonuclandiramayiz

        # fh_end dakika-etiketi guvenilmezse (0/None - HT icin status_id hic
        # dogrulanamiyor, bkz. SAFE_FIRST_HALF_OVER_SECONDS aciklamasi) VE
        # kickoff biliniyorsa, saat-bazli yedek referansi dene. Dakika-bazli
        # veri GERCEKTEN varsa (fh_minute > 0) buna hic dokunulmuyor.
        if (not fh_minute) and kickoff_ts and sig_min is not None and sig_min <= 45:
            t_h, t_a = _time_based_fh_end(cur, match_id_db, kickoff_ts)
            if t_h is not None:
                fh_h, fh_a = t_h, t_a

        outcome = compute_outcome(sig_min, initial, cur_h, cur_a, status, minute, fh_h, fh_a,
                                  kickoff_ts=kickoff_ts, now=now_ts)
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


def reconcile_void_signals(verbose=True):
    """VOID yazilmis sinyalleri HER DONGUDE yeniden degerlendirir - kullanici
    talebi geregi ("gozlem disi olanlarin kazananlarini kazandi, kaybedenlerini
    kayip olarak isaretle"). settle_pending()'in tek farki: outcome='VOID'
    olanlara bakar (IS NULL degil).

    NEDEN TEK SEFERLIK BIR MIGRATION DEGIL, HER DONGUDE CALISAN BIR FONKSIYON:
    VOID artik SADECE eski buglardan degil, kasitli bir tasarimdan da geliyor
    (bkz. orchestrator.py _kapasite_kontrolu - kapasite dolunca en zayif acik
    sinyal VOID yapilip yerine daha guclu bir aday aciliyor). Kapasite yuzunden
    VOID olan bir mac COGU ZAMAN hala CANLI - saatler sonra gercek sonucuna
    ulasabilir. Tek seferlik bir migration bunu YAKALAYAMAZ, bu yuzden
    settle_pending() ile AYNI CADANSTA (her orchestrator dongusunde) calisir.

    SADECE kesin WON/LOST cikan kayitlar guncellenir - hala belirsiz (None)
    ya da gercekten VOID cikan kayitlara DOKUNULMAZ, PENDING'e de GERI
    ACILMAZ (kullanicinin bu seferki istegi sadece kesin sonucu yazmak,
    kapasite kontrolunu bozmadan).
    """
    conn = _connect()
    cur = conn.cursor()

    cur.execute('''
        SELECT p.id, p.signal_minute, m.home_score, m.away_score, m.status, m.minute,
               fh.fh_end_home, fh.fh_end_away, fh.fh_end_minute, p.initial_goals,
               m.id, m.kickoff_ts
        FROM consensus_predictions p
        LEFT JOIN matches m ON m.id = p.match_id
        LEFT JOIN (
            SELECT ls1.match_id, ls1.home_score AS fh_end_home, ls1.away_score AS fh_end_away,
                   ls1.minute AS fh_end_minute
            FROM live_snapshots ls1
            WHERE ls1.id = (
                SELECT ls2.id FROM live_snapshots ls2
                WHERE ls2.match_id = ls1.match_id AND ls2.minute <= 45
                ORDER BY ls2.minute DESC, ls2.id DESC
                LIMIT 1
            )
        ) fh ON fh.match_id = m.id
        WHERE p.decision = 'signal' AND p.outcome = 'VOID' AND p.initial_goals IS NOT NULL
    ''')
    rows = cur.fetchall()

    fixed = won = lost = 0
    now_ts = time.time()
    for (pid, sig_min, cur_h, cur_a, status, minute, fh_h, fh_a, fh_minute,
         initial, match_id_db, kickoff_ts) in rows:

        if (not fh_minute) and kickoff_ts and sig_min is not None and sig_min <= 45:
            t_h, t_a = _time_based_fh_end(cur, match_id_db, kickoff_ts)
            if t_h is not None:
                fh_h, fh_a = t_h, t_a

        outcome = compute_outcome(sig_min, initial, cur_h, cur_a, status, minute, fh_h, fh_a,
                                  kickoff_ts=kickoff_ts, now=now_ts)
        if outcome not in ("WON", "LOST"):
            continue

        cur.execute(
            "UPDATE consensus_predictions SET outcome=?, settled_at=CURRENT_TIMESTAMP "
            "WHERE id=? AND outcome='VOID'",
            (outcome, pid),
        )
        fixed += cur.rowcount
        if outcome == "WON":
            won += 1
        else:
            lost += 1

    conn.commit()
    conn.close()
    if verbose and fixed:
        print(f"[settlement] VOID yeniden kontrol: {fixed} kayit kesin sonuca "
              f"cevrildi (kazanan={won} kaybeden={lost})", flush=True)
    return fixed, won, lost


def delete_unresolvable_void(verbose=True):
    """Kalici olarak ASLA cozulemeyecek VOID sinyalleri siler - kullanici
    talebi ("gercekten belirsiz olanlari sil"). reconcile_void_signals()'in
    tam tersi: o kesin sonuca ulasanlari WON/LOST yapar, bu da bir daha HICBIR
    ZAMAN kesin sonuca ulasamayacaklari temizler.

    Sadece IKI DAR durumda siler (yanlislikla hala cozulebilecek bir kaydi
    silmemek icin):
      1. initial_goals NULL - sinyal anindaki referans gol sayisi hic
         kaydedilmemis, hangi esigi takip ettigimizi ASLA bilemeyiz.
      2. Bagli mac status='ABANDONED' - tracking bu maca bir daha DONMEYECEK
         (thesports_bot durumu boyle birakti), gercek sonuc asla gelmeyecek.
    Hala CANLI (LIVE/HT/FINISHED-henuz-reconcile-edilmemis) bir maca bagli
    VOID kayitlara DOKUNULMAZ - onlar reconcile_void_signals() ile mac
    (gercekten) bitince kesin sonuca ulasir, erken silinmemeli.
    """
    conn = _connect()
    cur = conn.cursor()
    cur.execute('''
        DELETE FROM consensus_predictions
        WHERE decision='signal' AND outcome='VOID'
          AND (
              initial_goals IS NULL
              OR match_id IN (SELECT id FROM matches WHERE status='ABANDONED')
          )
    ''')
    silinen = cur.rowcount
    conn.commit()
    conn.close()
    if verbose and silinen:
        print(f"[settlement] {silinen} kalici cozulemeyen VOID sinyal silindi", flush=True)
    return silinen
