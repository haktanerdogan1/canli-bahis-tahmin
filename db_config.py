"""
Veritabani yolunu tek yerden yoneten yardimci modul.

NEDEN GEREKLI:
  Railway'in dosya sistemi GECICIDIR. Her deploy'da konteyner sifirdan kurulur ve
  git deposundaki dosyalar geri yazilir. Veritabani dosyasi (database/fh_goal_predictor.db)
  git'te takipli oldugu icin, HER DEPLOY'DA canli veritabani depodaki eski surumle
  DEGISTIRILIYORDU. Bu, uyelik sistemiyle birlikte ciddi bir soruna donusur:
  siteye kaydolan kullanicilarin hesaplari bir sonraki deploy'da SILINIR.

COZUM:
  DATABASE_PATH ortam degiskeni tanimliysa (Railway'de kalici bir Volume'a isaret eder)
  veritabani orada tutulur ve deploy'lardan etkilenmez. Volume ilk kez bostaysa,
  depodaki veritabani bir kereye mahsus "tohum" olarak oraya kopyalanir.
"""
import os
import shutil
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
SEED_DB = os.path.join(PROJECT_DIR, 'database', 'fh_goal_predictor.db')


def get_db_path() -> str:
    target = os.environ.get("DATABASE_PATH")

    if not target:
        # Yerel gelistirme: depodaki dosyayi kullan
        target = SEED_DB

    os.makedirs(os.path.dirname(target), exist_ok=True)

    # Kalici disk bos ise depodaki veritabanini bir kereligine kopyala
    if not os.path.exists(target) and os.path.exists(SEED_DB) and target != SEED_DB:
        try:
            shutil.copy2(SEED_DB, target)
            print(f"[db_config] Kalici diske ilk kurulum: {SEED_DB} -> {target}", flush=True)
        except Exception as e:
            print(f"[db_config] Tohum kopyalama basarisiz: {e}", flush=True)

    # Eger veritabani yoksa (ilk kurulumda veya depodan silindiyse) tablolari olustur
    if not os.path.exists(target) or os.path.getsize(target) == 0:
        import init_db
        init_db.DB_PATH = target
        init_db.init_db()
        print(f"[db_config] Yeni veritabani basariyla olusturuldu: {target}", flush=True)

    return target


DB_PATH = get_db_path()


def connect() -> sqlite3.Connection:
    """Ortak baglanti yardimcisi.

    NEDEN GEREKLI:
      api, v4_api_bot ve orchestrator ayri surecler olarak AYNI SQLite
      dosyasina yaziyor. SQLite'in varsayilan modu (rollback journal) bir
      yazici aktifken diger tum baglantilari kilitler; kisa sureli cakismalar
      "database is locked" hatasina donusuyordu (uyelik kaydinda 500).

      WAL (Write-Ahead Log) modu okuyucularin yazicidan etkilenmemesini
      saglar. busy_timeout ise geriye kalan yazici-yazici cakismalarinda
      aninda hata vermek yerine 30 saniye bekleyip tekrar dener.
    """
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def measured_write(job: str, source: str = "-", batch_size: int = 0):
    """Bir yazma transaction'inin ACQUISITION (writer-slot bekleme), BODY
    (gövde) ve COMMIT surelerini AYRI AYRI olcup loglar (2026-09-08, GPT-6
    Astra ikinci-gorus incelemesi + kullanici onayi).

    NEDEN GEREKLI: bugun yasanan kronik "database is locked" sorununda,
    indeks eklemek gibi somut duzeltmeler bile sorunu SADECE KISMEN
    cozdu - "hangi yazici writer slotunu ne kadar tutuyor" bilgisi
    olmadan kalan darbogazi TAHMIN etmek yerine OLCMEK gerekiyordu
    (CLAUDE.md kural 2: olcmeden iddia yok). Bu fonksiyon TUM yazma
    noktalarina (live_sync, live-stats-update, VOID silme, zaman asimi/
    zombi kapatma, settlement, snapshot temizligi, kalibrasyon) EKLENEREK
    kullanilmasi amaclaniyor - sadece "hata var" degil, "hata ONCESINDE
    writer slotunu edinmek ne kadar surdu, govde ne kadar surdu, commit
    ne kadar surdu" bilgisini verir.

    BEGIN IMMEDIATE kasitli: normal (deferred) transaction ilk yazma
    komutuna kadar writer kilidini ALMAZ - "ne zaman gercekten kilit
    alindi" belirsiz kalir. BEGIN IMMEDIATE writer slotunu EN BASTA
    ister, boylece "acquired" ani NET bir olcum noktasi olur.

    KULLANIM UYARISI (Astra'nin ozetledigi sinirlar): (1) yield edilen
    `conn` uzerinde calisan yardimci fonksiyonlar KENDI baglantisini
    ACMAMALI/commit ETMEMELI - ayni transaction'i paylasmali, (2) bu
    fonksiyon YAZMA GEREKTIRMEYEN uzun okuma/hesaplama isini SARMAMALI -
    once hazirlik disarida yapilip, sadece KISA yazma islemi bu blokla
    sarilmali (aksi halde "acquired" ile "commit" arasindaki sure yanlis
    yorumlanir - govde suresi degil, hazirlik suresi olculmus olur)."""
    tx_id = uuid.uuid4().hex[:12]
    conn = None
    started = time.monotonic()
    wait_started = acquired = commit_started = None
    phase = "connect"

    def _event(name, **fields):
        parcalar = " ".join(f"{k}={v}" for k, v in fields.items())
        print(f"[db_tx] {name} tx_id={tx_id} job={job} source={source} "
              f"batch={batch_size} pid={os.getpid()} tid={threading.get_ident()} "
              f"{parcalar}", flush=True)

    try:
        conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
        conn.execute("PRAGMA synchronous=NORMAL")
        # DIKKAT: busy_timeout PER-CONNECTION bir ayar, connect()'teki
        # gibi burada da ACIKCA ayarlanmali - Astra'nin verdigi ornek
        # iskelette bu satir yoktu, eklemezsek bu yeni baglanti 0ms
        # (SQLite varsayilani) ile acilir ve BEGIN IMMEDIATE kilit varsa
        # HEMEN (beklemeden) hata verir - mevcut connect() davranisiyla
        # TUTARSIZ olurdu.
        conn.execute("PRAGMA busy_timeout=30000")

        phase = "begin"
        _event("begin_attempt", connect_ms=round((time.monotonic() - started) * 1000, 1))
        wait_started = time.monotonic()
        conn.execute("BEGIN IMMEDIATE")
        acquired = time.monotonic()
        phase = "body"
        _event("acquired", wait_ms=round((acquired - wait_started) * 1000, 1))

        yield conn

        phase = "commit"
        commit_started = time.monotonic()
        conn.commit()
        ended = time.monotonic()

        _event("done",
               wait_ms=round((acquired - wait_started) * 1000, 1),
               body_ms=round((commit_started - acquired) * 1000, 1),
               commit_ms=round((ended - commit_started) * 1000, 1))
    except BaseException as exc:
        _event("failed",
               phase=phase,
               elapsed_ms=round((time.monotonic() - started) * 1000, 1),
               sqlite_code=getattr(exc, "sqlite_errorcode", None),
               sqlite_name=getattr(exc, "sqlite_errorname", None))
        if conn is not None and conn.in_transaction:
            try:
                conn.rollback()
            except Exception:
                pass
        raise
    finally:
        if conn is not None:
            conn.close()
