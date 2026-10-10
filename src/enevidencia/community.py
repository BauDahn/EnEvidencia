"""Comunidad (foro de lectores) y moderación. Mismo SQLite que Storage.

Las aportaciones de la comunidad son OPINIONES de lectores, no evidencia: nunca entran en comparaciones ni informes.
Estado de ocultación (columna `hidden`): 0 visible, 1 retirado por moderación, 2 eliminado por su autor.
Anti-spam: límite de mensajes por hora y usuario, duplicados rechazados, longitudes validadas.
"""
from __future__ import annotations

import re
import sqlite3
import threading
import time
from datetime import datetime, timezone

CATEGORIES = ["General", "Metodología", "Fuentes y datos", "Errores y correcciones", "Sugerencias"]
TITLE_MIN, TITLE_MAX = 8, 140
BODY_MIN, BODY_MAX = 5, 5000
REASON_MIN, REASON_MAX = 3, 500
EDIT_WINDOW = 15 * 60
MAX_PER_HOUR = 10
MAX_REPORTS_PER_HOUR = 20
DUP_WINDOW = 24 * 3600

SCHEMA = """
CREATE TABLE IF NOT EXISTS threads (
    id INTEGER PRIMARY KEY, title TEXT NOT NULL, body TEXT NOT NULL, author_id INTEGER NOT NULL,
    category TEXT NOT NULL, fact_id INTEGER, created_at TEXT NOT NULL, last_activity TEXT NOT NULL,
    locked INTEGER NOT NULL DEFAULT 0, pinned INTEGER NOT NULL DEFAULT 0, hidden INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY, thread_id INTEGER NOT NULL, author_id INTEGER NOT NULL, body TEXT NOT NULL,
    created_at TEXT NOT NULL, edited_at TEXT, hidden INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY, kind TEXT NOT NULL CHECK (kind IN ('thread','post','topic')), target_id INTEGER NOT NULL,
    reporter_id INTEGER NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL, resolved INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS mod_log (
    id INTEGER PRIMARY KEY, admin_id INTEGER NOT NULL, action TEXT NOT NULL, target TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_posts_thread ON posts(thread_id, id);
CREATE INDEX IF NOT EXISTS idx_threads_act ON threads(pinned DESC, last_activity DESC);
CREATE INDEX IF NOT EXISTS idx_threads_fact ON threads(fact_id);
"""


class CommunityError(ValueError):
    """Error de validación apto para mostrar al usuario."""


class RateLimited(CommunityError):
    pass


def _clean(text: str | None) -> str:
    t = (text or "").replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    t = "".join(c for c in t if c in "\n\t" or ord(c) >= 32)
    return re.sub(r"\n{4,}", "\n\n\n", t).strip()


def like_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def paginate(total: int, page: int, per_page: int) -> tuple[int, int, int]:
    """(pagina_segura, total_paginas, offset). Nunca fuera de rango."""
    pages = max(1, -(-total // per_page))
    page = min(max(1, int(page or 1)), pages)
    return page, pages, (page - 1) * per_page


class Community:
    def __init__(self, conn: sqlite3.Connection, clock=time.time, edit_window: int = EDIT_WINDOW,
                 max_per_hour: int = MAX_PER_HOUR) -> None:
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._lock = threading.RLock()
        self._clock = clock
        self.edit_window = edit_window
        self.max_per_hour = max_per_hour

    # ---- utilidades
    def _iso(self, ts: float | None = None) -> str:
        return datetime.fromtimestamp(self._clock() if ts is None else ts, timezone.utc).isoformat(timespec="seconds")

    def _since(self, seconds: int) -> str:
        return self._iso(self._clock() - seconds)

    def _age(self, iso: str) -> float:
        try:
            return self._clock() - datetime.fromisoformat(iso).timestamp()
        except ValueError:
            return float("inf")

    def can_edit(self, author_id: int, created_at: str, user) -> bool:
        return bool(user) and not user.banned and user.id == author_id and self._age(created_at) <= self.edit_window

    # ---- validación y antispam
    @staticmethod
    def _check_body(body: str) -> str:
        body = _clean(body)
        if len(body) < BODY_MIN:
            raise CommunityError(f"El mensaje debe tener al menos {BODY_MIN} caracteres.")
        if len(body) > BODY_MAX:
            raise CommunityError(f"El mensaje no puede superar {BODY_MAX} caracteres.")
        return body

    def _antispam(self, user, body: str) -> None:
        if user.banned:
            raise CommunityError("Tu cuenta está bloqueada y no puede participar.")
        since = self._since(3600)
        n = self.conn.execute(
            "SELECT (SELECT COUNT(*) FROM threads WHERE author_id=? AND created_at>=?)"
            " + (SELECT COUNT(*) FROM posts WHERE author_id=? AND created_at>=?)",
            (user.id, since, user.id, since)).fetchone()[0]
        if n >= self.max_per_hour and not user.is_admin:
            raise RateLimited(f"Has alcanzado el límite de {self.max_per_hour} mensajes por hora. Vuelve a intentarlo más tarde.")
        d = self._since(DUP_WINDOW)
        dup = self.conn.execute(
            "SELECT 1 FROM posts WHERE author_id=? AND body=? AND created_at>=? AND hidden<>2 UNION ALL "
            "SELECT 1 FROM threads WHERE author_id=? AND body=? AND created_at>=? AND hidden<>2",
            (user.id, body, d, user.id, body, d)).fetchone()
        if dup:
            raise CommunityError("Ese mensaje ya lo has enviado. No se admiten mensajes duplicados.")

    # ---- creación
    def create_thread(self, user, title: str, body: str, category: str, fact_id: int | None = None) -> int:
        title = re.sub(r"\s+", " ", _clean(title))
        if not TITLE_MIN <= len(title) <= TITLE_MAX:
            raise CommunityError(f"El título debe tener entre {TITLE_MIN} y {TITLE_MAX} caracteres.")
        if category not in CATEGORIES:
            raise CommunityError("Elige una categoría de la lista.")
        body = self._check_body(body)
        if fact_id is not None and not self.conn.execute("SELECT 1 FROM facts WHERE id=?", (fact_id,)).fetchone():
            raise CommunityError("El hecho indicado no existe.")
        with self._lock:
            self._antispam(user, body)
            now = self._iso()
            cur = self.conn.execute(
                "INSERT INTO threads(title,body,author_id,category,fact_id,created_at,last_activity) VALUES (?,?,?,?,?,?,?)",
                (title, body, user.id, category, fact_id, now, now))
            self.conn.commit()
            return cur.lastrowid  # type: ignore[return-value]

    def add_post(self, user, thread_id: int, body: str) -> int:
        body = self._check_body(body)
        t = self.get_thread(thread_id)
        if t is None:
            raise CommunityError("El hilo no existe.")
        if t["hidden"] and not user.is_admin:
            raise CommunityError("Este hilo ha sido retirado y no admite respuestas.")
        if t["locked"] and not user.is_admin:
            raise CommunityError("Este hilo está cerrado: ya no admite respuestas.")
        with self._lock:
            self._antispam(user, body)
            now = self._iso()
            cur = self.conn.execute("INSERT INTO posts(thread_id,author_id,body,created_at) VALUES (?,?,?,?)",
                                    (thread_id, user.id, body, now))
            self.conn.execute("UPDATE threads SET last_activity=? WHERE id=?", (now, thread_id))
            self.conn.commit()
            return cur.lastrowid  # type: ignore[return-value]

    # ---- lectura
    _T = ("SELECT t.*, u.username AS author, u.role AS author_role, "
          "(SELECT COUNT(*) FROM posts p WHERE p.thread_id=t.id AND p.hidden<>2) AS replies "
          "FROM threads t JOIN users u ON u.id=t.author_id")

    def get_thread(self, thread_id: int):
        return self.conn.execute(self._T + " WHERE t.id=?", (thread_id,)).fetchone()

    def get_post(self, post_id: int):
        return self.conn.execute("SELECT p.*, u.username AS author, u.role AS author_role FROM posts p "
                                 "JOIN users u ON u.id=p.author_id WHERE p.id=?", (post_id,)).fetchone()

    def list_threads(self, category: str = "", q: str = "", fact_id: int | None = None, page: int = 1,
                     per_page: int = 15):
        where, args = ["t.hidden<>2"], []
        if category in CATEGORIES:
            where.append("t.category=?"); args.append(category)
        if fact_id:
            where.append("t.fact_id=?"); args.append(fact_id)
        q = (q or "").strip()[:80]
        if q:
            where.append("(t.title LIKE ? ESCAPE '\\' OR t.body LIKE ? ESCAPE '\\')")
            args += [f"%{like_escape(q)}%"] * 2
        w = " WHERE " + " AND ".join(where)
        total = self.conn.execute("SELECT COUNT(*) FROM threads t" + w, args).fetchone()[0]
        page, pages, off = paginate(total, page, per_page)
        rows = self.conn.execute(self._T + w + " ORDER BY t.pinned DESC, t.last_activity DESC, t.id DESC LIMIT ? OFFSET ?",
                                 args + [per_page, off]).fetchall()
        return rows, total, page, pages

    def list_posts(self, thread_id: int, page: int = 1, per_page: int = 20):
        total = self.conn.execute("SELECT COUNT(*) FROM posts WHERE thread_id=?", (thread_id,)).fetchone()[0]
        page, pages, off = paginate(total, page, per_page)
        rows = self.conn.execute(
            "SELECT p.*, u.username AS author, u.role AS author_role FROM posts p JOIN users u ON u.id=p.author_id "
            "WHERE p.thread_id=? ORDER BY p.id LIMIT ? OFFSET ?", (thread_id, per_page, off)).fetchall()
        return rows, total, page, pages

    def threads_for_fact(self, fact_id: int, limit: int = 5):
        rows = self.conn.execute(self._T + " WHERE t.fact_id=? AND t.hidden=0 ORDER BY t.last_activity DESC LIMIT ?",
                                 (fact_id, limit)).fetchall()
        total = self.conn.execute("SELECT COUNT(*) FROM threads WHERE fact_id=? AND hidden=0", (fact_id,)).fetchone()[0]
        return rows, total

    # ---- edición propia
    def _own(self, row, user, what: str) -> None:
        if row is None:
            raise CommunityError(f"{what} no existe.")
        if user.banned or row["author_id"] != user.id:
            raise PermissionError("Solo el autor puede modificar su mensaje.")

    def edit_post(self, user, post_id: int, body: str) -> None:
        p = self.get_post(post_id)
        self._own(p, user, "El mensaje")
        if p["hidden"]:
            raise CommunityError("Este mensaje ya no se puede editar.")
        if not self.can_edit(p["author_id"], p["created_at"], user):
            raise CommunityError(f"Solo se puede editar durante los primeros {self.edit_window // 60} minutos.")
        body = self._check_body(body)
        self.conn.execute("UPDATE posts SET body=?, edited_at=? WHERE id=?", (body, self._iso(), post_id))
        self.conn.commit()

    def edit_thread(self, user, thread_id: int, body: str) -> None:
        t = self.get_thread(thread_id)
        self._own(t, user, "El hilo")
        if t["hidden"]:
            raise CommunityError("Este hilo ya no se puede editar.")
        if not self.can_edit(t["author_id"], t["created_at"], user):
            raise CommunityError(f"Solo se puede editar durante los primeros {self.edit_window // 60} minutos.")
        body = self._check_body(body)
        self.conn.execute("UPDATE threads SET body=? WHERE id=?", (body, thread_id))
        self.conn.commit()

    def delete_post(self, user, post_id: int) -> None:
        self._own(self.get_post(post_id), user, "El mensaje")
        self.conn.execute("UPDATE posts SET hidden=2 WHERE id=? AND hidden<>1", (post_id,))
        self.conn.commit()

    def delete_thread(self, user, thread_id: int) -> None:
        self._own(self.get_thread(thread_id), user, "El hilo")
        self.conn.execute("UPDATE threads SET hidden=2 WHERE id=? AND hidden<>1", (thread_id,))
        self.conn.commit()

    # ---- denuncias
    def report(self, user, kind: str, target_id: int, reason: str) -> int:
        if user.banned:
            raise CommunityError("Tu cuenta está bloqueada y no puede participar.")
        if kind not in ("thread", "post"):
            raise CommunityError("Tipo de denuncia no válido.")
        reason = re.sub(r"\s+", " ", _clean(reason))
        if len(reason) < REASON_MIN:
            raise CommunityError("Explica brevemente el motivo de la denuncia.")
        if len(reason) > REASON_MAX:
            raise CommunityError(f"El motivo no puede superar {REASON_MAX} caracteres.")
        row = self.get_thread(target_id) if kind == "thread" else self.get_post(target_id)
        if row is None:
            raise CommunityError("El mensaje denunciado no existe.")
        with self._lock:
            if self.conn.execute("SELECT 1 FROM reports WHERE kind=? AND target_id=? AND reporter_id=? AND resolved=0",
                                 (kind, target_id, user.id)).fetchone():
                raise CommunityError("Ya has denunciado este mensaje; la moderación lo revisará.")
            n = self.conn.execute("SELECT COUNT(*) FROM reports WHERE reporter_id=? AND created_at>=?",
                                  (user.id, self._since(3600))).fetchone()[0]
            if n >= MAX_REPORTS_PER_HOUR:
                raise RateLimited("Has enviado demasiadas denuncias en poco tiempo.")
            cur = self.conn.execute("INSERT INTO reports(kind,target_id,reporter_id,reason,created_at) VALUES (?,?,?,?,?)",
                                    (kind, target_id, user.id, reason, self._iso()))
            self.conn.commit()
            return cur.lastrowid  # type: ignore[return-value]

    # ---- moderación
    def log(self, admin_id: int, action: str, target: str, reason: str = "") -> None:
        self.conn.execute("INSERT INTO mod_log(admin_id,action,target,reason,created_at) VALUES (?,?,?,?,?)",
                          (admin_id, action, target, (reason or "")[:REASON_MAX], self._iso()))
        self.conn.commit()

    def mod_log(self, limit: int = 50):
        return self.conn.execute("SELECT m.*, u.username AS admin FROM mod_log m LEFT JOIN users u ON u.id=m.admin_id "
                                 "ORDER BY m.id DESC LIMIT ?", (limit,)).fetchall()

    def set_hidden(self, admin_id: int, kind: str, target_id: int, hidden: bool, reason: str = "") -> None:
        table = {"thread": "threads", "post": "posts"}.get(kind)
        if not table:
            raise CommunityError("Tipo no válido.")
        if not self.conn.execute(f"SELECT 1 FROM {table} WHERE id=?", (target_id,)).fetchone():
            raise CommunityError("El elemento no existe.")
        self.conn.execute(f"UPDATE {table} SET hidden=? WHERE id=?", (1 if hidden else 0, target_id))
        self.conn.commit()
        self.log(admin_id, ("ocultar_" if hidden else "restaurar_") + kind, f"{kind}:{target_id}", reason)

    def set_flag(self, admin_id: int, thread_id: int, flag: str, value: bool, reason: str = "") -> None:
        if flag not in ("locked", "pinned"):
            raise CommunityError("Acción no válida.")
        if self.get_thread(thread_id) is None:
            raise CommunityError("El hilo no existe.")
        self.conn.execute(f"UPDATE threads SET {flag}=? WHERE id=?", (int(value), thread_id))
        self.conn.commit()
        name = {("locked", True): "bloquear_hilo", ("locked", False): "desbloquear_hilo",
                ("pinned", True): "fijar_hilo", ("pinned", False): "desfijar_hilo"}[(flag, value)]
        self.log(admin_id, name, f"thread:{thread_id}", reason)

    def resolve_report(self, admin_id: int, report_id: int, reason: str = "") -> None:
        cur = self.conn.execute("UPDATE reports SET resolved=1 WHERE id=?", (report_id,))
        self.conn.commit()
        if cur.rowcount:
            self.log(admin_id, "cerrar_denuncia", f"report:{report_id}", reason)

    def open_reports(self, limit: int = 100) -> list[dict]:
        rows = self.conn.execute(
            "SELECT r.*, u.username AS reporter FROM reports r LEFT JOIN users u ON u.id=r.reporter_id "
            "WHERE r.resolved=0 ORDER BY r.id LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["thread_id"], d["excerpt"], d["hidden"], d["target_author"] = None, "", 0, ""
            if r["kind"] == "thread":
                t = self.get_thread(r["target_id"])
                if t:
                    d.update(thread_id=t["id"], excerpt=t["title"], hidden=t["hidden"], target_author=t["author"])
            elif r["kind"] == "post":
                p = self.get_post(r["target_id"])
                if p:
                    d.update(thread_id=p["thread_id"], excerpt=p["body"][:200], hidden=p["hidden"], target_author=p["author"])
            out.append(d)
        return out

    def topic_reports(self, limit: int = 100) -> list[dict]:
        """Temas denunciados (tabla `topic_reports` del módulo de temas, si existe), agrupados por tema."""
        has = {r["name"] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        out: list[dict] = []
        try:
            if "topic_reports" in has and "topics" in has:
                for r in self.conn.execute(
                        "SELECT tr.topic_id, t.title, t.hidden, COUNT(*) AS n, GROUP_CONCAT(tr.reason, ' | ') AS reasons "
                        "FROM topic_reports tr JOIN topics t ON t.id=tr.topic_id GROUP BY tr.topic_id "
                        "ORDER BY MAX(tr.created_at) DESC LIMIT ?", (limit,)):
                    out.append({**dict(r), "kind": "topic_reports"})
            out += [{**d, "kind": "reports_topic"} for d in self.open_reports(limit) if d["kind"] == "topic"]
        except sqlite3.Error:
            pass
        return out

    def set_topic_hidden(self, admin_id: int, topic_id: int, hidden: bool, reason: str = "") -> None:
        try:
            cur = self.conn.execute("UPDATE topics SET hidden=? WHERE id=?", (int(hidden), topic_id))
        except sqlite3.Error:
            raise CommunityError("Los temas no están disponibles.") from None
        if not cur.rowcount:
            raise CommunityError("El tema no existe.")
        self.conn.commit()
        self.log(admin_id, ("ocultar_" if hidden else "restaurar_") + "topic", f"topic:{topic_id}", reason)

    def clear_topic_reports(self, admin_id: int, topic_id: int, reason: str = "") -> None:
        try:
            self.conn.execute("DELETE FROM topic_reports WHERE topic_id=?", (topic_id,))
        except sqlite3.Error:
            return
        self.conn.commit()
        self.log(admin_id, "cerrar_denuncias_tema", f"topic:{topic_id}", reason)

    def list_users(self, limit: int = 200):
        return self.conn.execute("SELECT id, username, role, created_at, banned FROM users ORDER BY id LIMIT ?",
                                 (limit,)).fetchall()
