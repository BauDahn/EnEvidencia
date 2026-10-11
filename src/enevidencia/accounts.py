"""Cuentas y sesiones locales, sin servicios externos (solo biblioteca estándar).

- Contraseñas: scrypt con sal aleatoria por usuario; comparación en tiempo constante.
- Sesiones: token aleatorio de 32 bytes; en la base solo se guarda su sha256 (un volcado de la base no sirve
  para suplantar a nadie). Caducan a los 14 días y se invalidan al salir.
- El primer usuario registrado es administrador (alta atómica en una sola sentencia SQL), salvo que se defina
  ENEVIDENCIA_ADMIN_USER: entonces solo ese usuario es admin al registrarse. `make_admin` promueve/recupera.
- Limitador en memoria de intentos fallidos por IP + usuario, con bloqueo temporal.
"""
from __future__ import annotations

from .storage import ConnSource
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('user','admin')),
    created_at TEXT NOT NULL,
    banned INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS register_log (
    ip TEXT NOT NULL, at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_register_log ON register_log(ip, at);
"""

SESSION_DAYS = 14
MIN_PASSWORD = 10
MAX_PASSWORD = 200
MAX_FAILS = 5
MAX_IP_FAILS = 20          # fallos de login por IP (cualquier usuario) en LOCK_SECONDS
REG_PER_HOUR = 3
REG_PER_DAY = 10
LOCK_SECONDS = 15 * 60
USERNAME_RE = re.compile(r"^[a-z0-9_]{3,24}$")
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")
GENERIC_LOGIN_ERROR = "Usuario o contraseña incorrectos."
_SCRYPT = (2 ** 14, 8, 1)


class AccountError(ValueError):
    """Error de validación apto para mostrar al usuario."""


@dataclass(frozen=True)
class User:
    id: int
    username: str
    email: str
    role: str
    created_at: str
    banned: bool = False

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    n, r, p = _SCRYPT
    dk = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=32)
    return f"scrypt${n}${r}${p}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, dk = stored.split("$")
        calc = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=len(dk) // 2)
        return hmac.compare_digest(calc.hex(), dk)
    except (ValueError, TypeError):
        return False


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Accounts(ConnSource):
    def __init__(self, conn: sqlite3.Connection, clock=time.monotonic) -> None:
        self._src = conn
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._lock = threading.RLock()
        self._clock = clock
        self._fails: dict[tuple[str, str], list[float]] = {}
        self._ip_fails: dict[str, list[float]] = {}
        self.reg_per_hour, self.reg_per_day = REG_PER_HOUR, REG_PER_DAY
        self._dummy = hash_password("x" * 12)  # para igualar tiempos cuando el usuario no existe

    # ---- registro
    def register(self, username: str, email: str, password: str) -> User:
        username, email = (username or "").strip().lower(), (email or "").strip()
        if not USERNAME_RE.match(username):
            raise AccountError("El usuario debe tener de 3 a 24 caracteres: letras minúsculas, números o guion bajo.")
        if len(email) > 120 or not EMAIL_RE.match(email):
            raise AccountError("Escribe un correo electrónico válido.")
        if len(password or "") < MIN_PASSWORD:
            raise AccountError(f"La contraseña debe tener al menos {MIN_PASSWORD} caracteres.")
        if len(password) > MAX_PASSWORD:
            raise AccountError(f"La contraseña no puede superar {MAX_PASSWORD} caracteres.")
        if password.lower() in (username, email.lower()):
            raise AccountError("La contraseña no puede ser igual al usuario o al correo.")
        pw = hash_password(password)
        with self._lock:
            designated = (os.environ.get("ENEVIDENCIA_ADMIN_USER", "").strip().lower())
            try:
                # Rol decidido DENTRO de la sentencia INSERT (atómica en SQLite): dos procesos no pueden crear dos "primeros".
                cur = self.conn.execute(
                    "INSERT INTO users(username,email,password_hash,role,created_at) "
                    "SELECT ?,?,?, CASE WHEN ?=1 THEN 'admin' WHEN ?=1 AND NOT EXISTS (SELECT 1 FROM users) "
                    "THEN 'admin' ELSE 'user' END, ?",
                    (username, email, pw, int(bool(designated) and username == designated), int(not designated),
                     _now().isoformat(timespec="seconds")))
                self.conn.commit()
            except sqlite3.IntegrityError:
                raise AccountError("Ese usuario o ese correo ya están en uso.") from None
            return self.get_user(cur.lastrowid)  # type: ignore[return-value]

    def make_admin(self, username: str) -> bool:
        """Promueve a administrador (recuperación). False si el usuario no existe."""
        with self._lock:
            cur = self.conn.execute("UPDATE users SET role='admin', banned=0 WHERE username=?",
                                    ((username or "").strip().lower(),))
            self.conn.commit()
            return cur.rowcount > 0

    # ---- límite de registros por IP (persistido: sobrevive a reinicios)
    def registration_allowed(self, ip: str) -> bool:
        now = time.time()
        with self._lock:
            hour, day = (self.conn.execute("SELECT COUNT(*) FROM register_log WHERE ip=? AND at>?",
                                           (ip, now - span)).fetchone()[0] for span in (3600, 86400))
        return hour < self.reg_per_hour and day < self.reg_per_day

    def record_registration(self, ip: str) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM register_log WHERE at<?", (time.time() - 86400,))
            self.conn.execute("INSERT INTO register_log(ip, at) VALUES (?,?)", (ip, time.time()))
            self.conn.commit()

    def get_user(self, user_id: int) -> User | None:
        row = self.conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return self._user(row) if row else None

    @staticmethod
    def _user(row) -> User:
        return User(row["id"], row["username"], row["email"], row["role"], row["created_at"], bool(row["banned"]))

    # ---- limitador
    def _recent_fails(self, key: tuple[str, str]) -> list[float]:
        now = self._clock()
        fails = [t for t in self._fails.get(key, []) if now - t < LOCK_SECONDS]
        self._fails[key] = fails
        return fails

    def _recent_ip_fails(self, ip: str) -> list[float]:
        now = self._clock()
        fails = [t for t in self._ip_fails.get(ip, []) if now - t < LOCK_SECONDS]
        self._ip_fails[ip] = fails
        return fails

    def is_locked(self, ip: str, username: str) -> bool:
        with self._lock:
            return len(self._recent_fails((ip, (username or "").strip().lower()))) >= MAX_FAILS

    # ---- autenticación
    def authenticate(self, username: str, password: str, ip: str = "") -> User:
        """Devuelve el usuario o lanza AccountError con un mensaje genérico (no revela si el usuario existe)."""
        uname = (username or "").strip().lower()
        key = (ip, uname)
        with self._lock:
            if len(self._recent_fails(key)) >= MAX_FAILS or (ip and len(self._recent_ip_fails(ip)) >= MAX_IP_FAILS):
                raise AccountError("Demasiados intentos fallidos. Espera unos minutos antes de volver a probar.")
        row = self.conn.execute("SELECT * FROM users WHERE username=?", (uname,)).fetchone() if uname else None
        ok = verify_password((password or "")[:MAX_PASSWORD], row["password_hash"] if row else self._dummy)
        if row is None or not ok or row["banned"]:
            with self._lock:
                self._fails.setdefault(key, []).append(self._clock())
                self._ip_fails.setdefault(ip, []).append(self._clock())
            raise AccountError(GENERIC_LOGIN_ERROR)
        with self._lock:
            self._fails.pop(key, None)
        return self._user(row)

    # ---- sesiones
    def create_session(self, user: User) -> str:
        token = secrets.token_urlsafe(32)
        now = _now()
        with self._lock:
            self.conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now.isoformat(timespec="seconds"),))
            self.conn.execute("INSERT INTO sessions(token_hash,user_id,created_at,expires_at) VALUES (?,?,?,?)",
                              (_token_hash(token), user.id, now.isoformat(timespec="seconds"),
                               (now + timedelta(days=SESSION_DAYS)).isoformat(timespec="seconds")))
            self.conn.commit()
        return token

    def user_from_token(self, token: str | None) -> User | None:
        if not token or len(token) > 128:
            return None
        row = self.conn.execute(
            "SELECT u.*, s.expires_at AS exp FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=?",
            (_token_hash(token),)).fetchone()
        if row is None or row["banned"] or row["exp"] < _now().isoformat(timespec="seconds"):
            return None
        return self._user(row)

    def logout(self, token: str | None) -> None:
        if token:
            with self._lock:
                self.conn.execute("DELETE FROM sessions WHERE token_hash=?", (_token_hash(token),))
                self.conn.commit()

    def ban(self, user_id: int, banned: bool = True) -> None:
        """Bloquea (o desbloquea) una cuenta y cierra sus sesiones. Para moderación."""
        with self._lock:
            self.conn.execute("UPDATE users SET banned=? WHERE id=?", (int(banned), user_id))
            if banned:
                self.conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            self.conn.commit()

    def unban(self, user_id: int) -> None:
        """Restablece una cuenta bloqueada (podrá volver a entrar)."""
        self.ban(user_id, False)
