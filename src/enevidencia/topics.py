"""Temas propuestos por los lectores: sugerir, votar y denunciar. Misma SQLite que Storage.

Los votos miden interés, no verdad: solo ayudan al administrador a decidir en qué gastar la cuota de IA.
"""
from __future__ import annotations

from .storage import ConnSource
import re
import sqlite3
import threading
import time
import unicodedata
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS topics (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    author_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','investigating','done','rejected')),
    fact_id INTEGER,
    created_at TEXT NOT NULL,
    hidden INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS topic_votes (
    topic_id INTEGER NOT NULL, user_id INTEGER NOT NULL, PRIMARY KEY (topic_id, user_id)
);
CREATE TABLE IF NOT EXISTS topic_reports (
    topic_id INTEGER NOT NULL, user_id INTEGER NOT NULL, reason TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
    PRIMARY KEY (topic_id, user_id)
);
"""

MIN_TITLE, MAX_TITLE, MAX_DESC, MAX_REASON = 8, 140, 1000, 300
MAX_PER_DAY = 5
STATUSES = ("open", "investigating", "done", "rejected")
STATUS_LABELS = {"open": "abierto", "investigating": "en investigación", "done": "investigado", "rejected": "descartado"}

_SELECT = """SELECT t.*, u.username AS author,
    (SELECT COUNT(*) FROM topic_votes v WHERE v.topic_id=t.id) AS votes,
    (SELECT COUNT(*) FROM topic_reports r WHERE r.topic_id=t.id) AS reports
    FROM topics t LEFT JOIN users u ON u.id=t.author_id"""


class TopicError(ValueError):
    """Error de validación apto para mostrar al usuario."""

    def __init__(self, msg: str, existing_id: int | None = None) -> None:
        super().__init__(msg)
        self.existing_id = existing_id


def normalize_title(title: str) -> str:
    t = unicodedata.normalize("NFKD", title.lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", t).split())


def _clean(text: str) -> str:
    """Sin caracteres de control; espacios colapsados en una línea."""
    return " ".join("".join(c for c in (text or "") if c >= " " or c in "\t\n").split())


class RateLimiter:
    """Ventana deslizante en memoria por usuario."""

    def __init__(self, limit: int, seconds: float) -> None:
        self.limit, self.seconds = limit, seconds
        self._hits: dict[int, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: int) -> bool:
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] > self.seconds:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(now)
            return True


class Topics(ConnSource):
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._src = conn
        self.lock = threading.Lock()
        self.conn.executescript(SCHEMA)
        self.vote_limiter = RateLimiter(30, 60)

    # ---- lectura
    def get(self, topic_id: int, include_hidden: bool = False):
        row = self.conn.execute(_SELECT + " WHERE t.id=?" + ("" if include_hidden else " AND t.hidden=0"),
                                (topic_id,)).fetchone()
        return dict(row) if row else None

    def list(self, status: str = "", q: str = "", include_hidden: bool = False, limit: int = 200) -> list[dict]:
        sql, args = _SELECT + " WHERE 1=1", []
        if not include_hidden:
            sql += " AND t.hidden=0"
        if status == "rejected":
            sql += " AND t.status='rejected'"
        elif status in STATUSES:
            sql += " AND t.status=?"
            args.append(status)
        else:
            sql += " AND t.status!='rejected'"
        sql += " ORDER BY votes DESC, t.created_at DESC, t.id DESC"
        rows = [dict(r) for r in self.conn.execute(sql, args)]
        needle = normalize_title(q)
        if needle:
            rows = [r for r in rows if needle in normalize_title(r["title"] + " " + r["description"])]
        return rows[:limit]

    def top(self, n: int = 3) -> list[dict]:
        return [t for t in self.list(status="open") if t["votes"] > 0][:n] or self.list(status="open")[:n]

    def voted_ids(self, user_id: int) -> set[int]:
        return {r[0] for r in self.conn.execute("SELECT topic_id FROM topic_votes WHERE user_id=?", (user_id,))}

    def reports(self, topic_id: int) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT r.*, u.username FROM topic_reports r LEFT JOIN users u ON u.id=r.user_id WHERE topic_id=? "
            "ORDER BY created_at", (topic_id,))]

    # ---- escritura de usuarios
    def suggest(self, user_id: int, title: str, description: str = "") -> int:
        title, description = _clean(title), (description or "").replace("\r", "").strip()
        description = "".join(c for c in description if c >= " " or c == "\n")
        if not MIN_TITLE <= len(title) <= MAX_TITLE:
            raise TopicError(f"El titular debe tener entre {MIN_TITLE} y {MAX_TITLE} caracteres.")
        if len(description) > MAX_DESC:
            raise TopicError(f"La descripción no puede superar los {MAX_DESC} caracteres.")
        norm = normalize_title(title)
        with self.lock:
            for r in self.conn.execute("SELECT id, title FROM topics WHERE hidden=0"):
                if normalize_title(r["title"]) == norm:
                    raise TopicError("Ya existe un tema con ese titular: apóyalo en lugar de repetirlo.", r["id"])
            since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
            n = self.conn.execute("SELECT COUNT(*) FROM topics WHERE author_id=? AND created_at>=?",
                                  (user_id, since)).fetchone()[0]
            if n >= MAX_PER_DAY:
                raise TopicError(f"Has alcanzado el límite de {MAX_PER_DAY} sugerencias en 24 horas. Vuelve mañana.")
            cur = self.conn.execute(
                "INSERT INTO topics(title, description, author_id, created_at) VALUES (?,?,?,?)",
                (title, description, user_id, datetime.now(timezone.utc).isoformat(timespec="seconds")))
            self.conn.commit()
            return cur.lastrowid

    def toggle_vote(self, topic_id: int, user_id: int) -> bool:
        """Apoya o retira el apoyo. Devuelve True si tras la llamada el usuario apoya el tema."""
        with self.lock:
            cur = self.conn.execute("DELETE FROM topic_votes WHERE topic_id=? AND user_id=?", (topic_id, user_id))
            voted = cur.rowcount == 0
            if voted:
                self.conn.execute("INSERT INTO topic_votes(topic_id, user_id) VALUES (?,?)", (topic_id, user_id))
            self.conn.commit()
            return voted

    def report(self, topic_id: int, user_id: int, reason: str = "") -> None:
        with self.lock:
            self.conn.execute("INSERT OR REPLACE INTO topic_reports(topic_id, user_id, reason, created_at) VALUES (?,?,?,?)",
                              (topic_id, user_id, _clean(reason)[:MAX_REASON],
                               datetime.now(timezone.utc).isoformat(timespec="seconds")))
            self.conn.commit()

    # ---- moderación
    def claim_investigation(self, topic_id: int) -> bool:
        """Reserva atómicamente el tema para investigarlo: solo UNA llamada gana (UPDATE condicional)."""
        with self.lock:
            cur = self.conn.execute("UPDATE topics SET status='investigating' "
                                    "WHERE id=? AND fact_id IS NULL AND status='open'", (topic_id,))
            self.conn.commit()
            return cur.rowcount == 1

    def release_investigation(self, topic_id: int) -> None:
        with self.lock:
            self.conn.execute("UPDATE topics SET status='open' WHERE id=? AND fact_id IS NULL AND status='investigating'",
                              (topic_id,))
            self.conn.commit()

    def set_status(self, topic_id: int, status: str, fact_id: int | None = None) -> None:
        if status not in STATUSES:
            raise TopicError("Estado no válido.")
        with self.lock:
            self.conn.execute("UPDATE topics SET status=?, fact_id=COALESCE(?, fact_id) WHERE id=?",
                              (status, fact_id, topic_id))
            self.conn.commit()

    def set_hidden(self, topic_id: int, hidden: bool) -> None:
        with self.lock:
            self.conn.execute("UPDATE topics SET hidden=? WHERE id=?", (int(hidden), topic_id))
            self.conn.commit()
