"""Accounts and login sessions. Standard library only (hashlib, hmac, secrets)."""
import hashlib
import hmac
import os
import re
import secrets
import sqlite3

from db import NOW, get_conn

SESSION_DAYS = 7
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# scrypt parameters (memory-hard password hashing). Stored inside each hash so they can change later.
_N, _R, _P = 2**14, 8, 1


# ------------------------------------------------------------------ passwords
def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt_hex, digest_hex = stored.split("$")
        digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex),
                                n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(digest.hex(), digest_hex)  # constant-time compare
    except Exception:
        return False


_DUMMY_HASH = hash_password("not-a-real-password")  # used so unknown emails cost the same time


# ------------------------------------------------------------------ users
def validate_credentials(email: str, password: str) -> str | None:
    """Return an error message, or None if both look fine."""
    if len(email) > 254 or not EMAIL_RE.match(email):
        return "Please enter a valid email address."
    if not 8 <= len(password) <= 128:
        return "Password must be 8 to 128 characters."
    return None


def clean_name(name: str, email: str) -> str:
    """Display name: trimmed, one-spaced, max 60 characters. Falls back to the part before the @."""
    name = " ".join((name or "").split())[:60]
    return name or email.split("@")[0]


def create_user(email: str, password: str, name: str = "") -> int | None:
    """Returns the new user id, or None if the email is already registered."""
    try:
        with get_conn() as conn:
            cur = conn.execute(
                "INSERT INTO users (email, name, password_hash) VALUES (?, ?, ?)",
                (email, clean_name(name, email), hash_password(password)),
            )
            return cur.lastrowid
    except sqlite3.IntegrityError:
        return None


def authenticate(email: str, password: str):
    """Returns the user row (id, email, name) if the email/password match, else None."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, email, name, password_hash FROM users WHERE email = ?", (email,)
        ).fetchone()
    if row is None:
        verify_password(password, _DUMMY_HASH)  # same cost as a real check
        return None
    return row if verify_password(password, row["password_hash"]) else None


# ------------------------------------------------------------------ sessions
def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(user_id: int) -> str:
    """Make a random token for the cookie. Only its hash is stored, so a DB leak is not a login leak."""
    token = secrets.token_urlsafe(32)
    with get_conn() as conn:
        conn.execute(f"DELETE FROM sessions WHERE expires_at <= {NOW}")  # tidy up old ones
        conn.execute(
            "INSERT INTO sessions (user_id, token_hash, expires_at) "
            f"VALUES (?, ?, strftime('%Y-%m-%d %H:%M:%f', 'now', '+{SESSION_DAYS} days'))",
            (user_id, _hash_token(token)),
        )
    return token


def user_for_token(token: str | None):
    """The logged-in user (id, email, name) for this cookie token, or None."""
    if not token:
        return None
    with get_conn() as conn:
        return conn.execute(
            "SELECT u.id, u.email, u.name FROM sessions s JOIN users u ON u.id = s.user_id "
            f"WHERE s.token_hash = ? AND s.expires_at > {NOW}", (_hash_token(token),),
        ).fetchone()


def delete_session(token: str | None) -> None:
    if token:
        with get_conn() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_hash_token(token),))
