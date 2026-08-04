"""
JCODE Analytics - kimlik dogrulama katmani.

Tasarim notlari:
  * Sifreler ASLA duz metin saklanmaz. hashlib.scrypt (Python standart kutuphanesi)
    ile, her kullanici icin ayri rastgele salt kullanilarak hash'lenir.
  * Oturum, HMAC ile IMZALANMIS bir cookie ile tutulur. Cookie kurcalanirsa imza
    tutmaz ve oturum gecersiz sayilir.
  * Hicbir ek pip bagimliligi yok (bcrypt/jwt vb. gerekmez).
"""
import hashlib
import hmac
import os
import secrets
import sqlite3
import time

from db_config import DB_PATH, connect  # Railway kalici disk destegi (bkz. db_config.py)

SESSION_TTL_SECONDS = 30 * 24 * 60 * 60  # 30 gun

_SCRYPT_N = 2 ** 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32


def _connect():
    return connect()


def init_auth_schema():
    """users tablosunu ve ayar tablosunu hazirlar (idempotent)."""
    conn = _connect()
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS app_config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    ''')
    conn.commit()
    conn.close()


def _get_secret_key() -> bytes:
    """Oturum imzalama anahtari.

    Once SECRET_KEY ortam degiskenine bakilir. Tanimli degilse veritabaninda
    kalici rastgele bir anahtar uretilip saklanir - boylece sunucu her yeniden
    basladiginda kullanicilarin oturumu dusmez.
    """
    env_key = os.environ.get("SECRET_KEY")
    if env_key:
        return env_key.encode("utf-8")

    conn = _connect()
    cur = conn.cursor()
    cur.execute("SELECT value FROM app_config WHERE key = 'session_secret'")
    row = cur.fetchone()
    if row:
        conn.close()
        return row[0].encode("utf-8")

    generated = secrets.token_hex(32)
    cur.execute(
        "INSERT OR REPLACE INTO app_config (key, value) VALUES ('session_secret', ?)",
        (generated,),
    )
    conn.commit()
    conn.close()
    return generated.encode("utf-8")


# --- Sifre islemleri -------------------------------------------------------

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(
        password.encode("utf-8"), salt=salt,
        n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_SCRYPT_DKLEN,
    )
    return f"scrypt${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, salt_hex, hash_hex = stored.split("$")
        if algo != "scrypt":
            return False
        dk = hashlib.scrypt(
            password.encode("utf-8"), salt=bytes.fromhex(salt_hex),
            n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_SCRYPT_DKLEN,
        )
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


# --- Oturum (imzali cookie) ------------------------------------------------

def create_session_token(user_id: int) -> str:
    expires = int(time.time()) + SESSION_TTL_SECONDS
    payload = f"{user_id}.{expires}"
    sig = hmac.new(_get_secret_key(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def verify_session_token(token: str):
    """Gecerliyse user_id doner, degilse None."""
    if not token:
        return None
    try:
        user_id_str, expires_str, sig = token.rsplit(".", 2)
        payload = f"{user_id_str}.{expires_str}"
        expected = hmac.new(_get_secret_key(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, sig):
            return None
        if int(expires_str) < int(time.time()):
            return None
        return int(user_id_str)
    except Exception:
        return None


# --- Kullanici islemleri ---------------------------------------------------

def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def create_user(email: str, password: str):
    """(user_id, None) veya (None, hata_mesaji) doner."""
    email = normalize_email(email)

    if not email or "@" not in email or "." not in email.split("@")[-1]:
        return None, "Geçerli bir e-posta adresi girin."
    if not password or len(password) < 6:
        return None, "Şifre en az 6 karakter olmalı."

    conn = _connect()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO users (email, password_hash) VALUES (?, ?)",
            (email, hash_password(password)),
        )
        conn.commit()
        return cur.lastrowid, None
    except sqlite3.IntegrityError:
        return None, "Bu e-posta zaten kayıtlı."
    finally:
        conn.close()


def authenticate(email: str, password: str):
    """(user_id, None) veya (None, hata_mesaji) doner."""
    email = normalize_email(email)
    conn = _connect()
    cur = conn.cursor()
    cur.execute("SELECT id, password_hash FROM users WHERE email = ?", (email,))
    row = cur.fetchone()
    conn.close()

    # E-posta bulunamadi ile sifre yanlis AYNI mesaji doner: boylece disaridan
    # hangi e-postalarin kayitli oldugu ogrenilemez (user enumeration korumasi).
    if not row or not verify_password(password, row[1]):
        return None, "E-posta veya şifre hatalı."
    return row[0], None


def get_or_create_oauth_user(email: str) -> int:
    """Google gibi bir OAuth saglayicisindan dogrulanmis e-posta icin kullanici
    id'si doner. Kayit yoksa, sema degismesin diye (password_hash NOT NULL),
    asla kimseye soylenmeyen/kullanilamayan rastgele bir sifre hash'iyle yeni
    bir kullanici olusturulur - bu kullanici sadece OAuth ile giris yapabilir.
    """
    email = normalize_email(email)
    conn = _connect()
    cur = conn.cursor()
    cur.execute("SELECT id FROM users WHERE email = ?", (email,))
    row = cur.fetchone()
    if row:
        conn.close()
        return row[0]

    cur.execute(
        "INSERT INTO users (email, password_hash) VALUES (?, ?)",
        (email, hash_password(secrets.token_hex(32))),
    )
    conn.commit()
    user_id = cur.lastrowid
    conn.close()
    return user_id


def get_user_email(user_id: int):
    conn = _connect()
    cur = conn.cursor()
    cur.execute("SELECT email FROM users WHERE id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None
