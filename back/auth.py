"""Local users and opaque browser sessions. No credentials are shipped with the app."""
import argparse
import getpass
import hashlib
import hmac
import os
import secrets
import sqlite3
import time
from pathlib import Path

DB_PATH = Path(os.environ.get("NFREADER_AUTH_DB", Path(__file__).parent / "data" / "usuarios.sqlite3"))
COOKIE = "nfreader_session"
SESSION_SECONDS = 12 * 60 * 60
ITERATIONS = 600_000


def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=10)
    db.execute("CREATE TABLE IF NOT EXISTS users (username TEXT PRIMARY KEY, password_hash TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, username TEXT NOT NULL, expires INTEGER NOT NULL)")
    return db


def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password, stored):
    try:
        algorithm, count, salt, digest = stored.split("$")
        if algorithm != "pbkdf2_sha256" or not 100_000 <= int(count) <= 1_000_000:
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(count))
        return hmac.compare_digest(actual, bytes.fromhex(digest))
    except (ValueError, TypeError):
        return False


def set_user(username, password):
    if not username or len(username) > 80 or len(password) < 6:
        raise ValueError("Usuário obrigatório e senha de pelo menos 6 caracteres.")
    with connect() as db:
        db.execute("INSERT INTO users VALUES (?, ?) ON CONFLICT(username) DO UPDATE SET password_hash=excluded.password_hash", (username, hash_password(password)))
        db.execute("DELETE FROM sessions WHERE username=?", (username,))


def authenticate(username, password):
    with connect() as db:
        row = db.execute("SELECT password_hash FROM users WHERE username=?", (username,)).fetchone()

    if row is None:
        hashlib.pbkdf2_hmac("sha256", password.encode(), b"missing-user-salt", ITERATIONS)
        return False
    return verify_password(password, row[0])


def create_session(username):
    token = secrets.token_urlsafe(32)
    with connect() as db:
        db.execute("DELETE FROM sessions WHERE expires < ?", (int(time.time()),))
        db.execute("INSERT INTO sessions VALUES (?, ?, ?)", (hashlib.sha256(token.encode()).hexdigest(), username, int(time.time()) + SESSION_SECONDS))
    return token


def current_user(token):
    if not token:
        return None
    with connect() as db:
        row = db.execute("SELECT s.username FROM sessions s JOIN users u ON u.username=s.username WHERE s.token_hash=? AND s.expires>?", (hashlib.sha256(token.encode()).hexdigest(), int(time.time()))).fetchone()
    return row[0] if row else None


def revoke_session(token):
    if token:
        with connect() as db:
            db.execute("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Gerenciar usuários locais do NFreader")
    parser.add_argument("action", choices=["add", "remove", "list"])
    parser.add_argument("username", nargs="?")
    args = parser.parse_args()
    if args.action == "list":
        with connect() as db:
            for (name,) in db.execute("SELECT username FROM users ORDER BY username"):
                print(name)
    elif args.action == "add":
        username = args.username or input("Nome do usuário: ").strip()
        if not username:
            parser.error("Informe o nome do usuário.")
        password = getpass.getpass("Senha (mínimo 6 caracteres): ")
        if password != getpass.getpass("Repita a senha: "):
            parser.error("As senhas não coincidem.")
        try:
            set_user(username, password)
        except ValueError as exc:
            parser.error(str(exc))
        print("Usuário salvo; sessões anteriores encerradas.")
    else:
        if not args.username:
            parser.error("Informe o nome do usuário a remover.")
        with connect() as db:
            db.execute("DELETE FROM sessions WHERE username=?", (args.username,))
            db.execute("DELETE FROM users WHERE username=?", (args.username,))
        print("Usuário removido.")
