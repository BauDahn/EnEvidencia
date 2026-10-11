"""Persistencia en SQLite."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
import weakref
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlsplit

from .models import Claim, ClaimRecord, Fact, Source, SourceType, Status

SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY, url TEXT NOT NULL, text TEXT NOT NULL, source_type TEXT NOT NULL,
    outlet TEXT, author TEXT, published TEXT, title TEXT, kind TEXT,
    classification_reason TEXT, retrieved_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'accepted'
);
CREATE TABLE IF NOT EXISTS claims (
    id INTEGER PRIMARY KEY, statement TEXT NOT NULL, speaker TEXT NOT NULL, quote TEXT NOT NULL,
    value TEXT NOT NULL DEFAULT '',
    source_id INTEGER NOT NULL REFERENCES sources(id),
    fact_id INTEGER NOT NULL REFERENCES facts(id),
    status TEXT NOT NULL DEFAULT 'accepted'
);
CREATE TABLE IF NOT EXISTS fact_sources (
    fact_id INTEGER NOT NULL, source_id INTEGER NOT NULL,
    extracted INTEGER NOT NULL DEFAULT 0, discarded INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (fact_id, source_id)
);
-- Auditoría del flujo automático (tablas nuevas; CREATE IF NOT EXISTS no toca bases existentes)
CREATE TABLE IF NOT EXISTS audit_queries (
    id INTEGER PRIMARY KEY, fact_id INTEGER NOT NULL, query TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_sources (
    id INTEGER PRIMARY KEY, fact_id INTEGER NOT NULL, url TEXT NOT NULL, title TEXT NOT NULL DEFAULT '',
    domain TEXT NOT NULL DEFAULT '', decision TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', source_id INTEGER
);
CREATE TABLE IF NOT EXISTS audit_claims (
    id INTEGER PRIMARY KEY, fact_id INTEGER NOT NULL, source_id INTEGER, url TEXT NOT NULL DEFAULT '',
    statement TEXT NOT NULL DEFAULT '', speaker TEXT NOT NULL DEFAULT '', quote TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT ''
);
-- Informe con redacción IA: se genera una vez (gasta cuota) y se muestra desde aquí
CREATE TABLE IF NOT EXISTS fact_reports (
    fact_id INTEGER PRIMARY KEY, text TEXT NOT NULL DEFAULT '', warnings TEXT NOT NULL DEFAULT '[]',
    references_json TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL, model TEXT NOT NULL DEFAULT '',
    accepted_claims_count INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT ''
);
"""


class _SharedCacheConn(sqlite3.Connection):
    """Solo para `:memory:` compartida: la caché compartida devuelve SQLITE_LOCKED (sin busy_timeout);
    se reintenta unos segundos, como haría busy_timeout en una BD en archivo."""

    def _retry(self, fn, *a):
        deadline = time.monotonic() + 5.0
        while True:
            try:
                return fn(*a)
            except sqlite3.OperationalError as e:
                if "locked" not in str(e) or time.monotonic() > deadline:
                    raise
                time.sleep(0.002)

    def execute(self, *a):
        return self._retry(super().execute, *a)

    def commit(self):
        return self._retry(super().commit)


class Storage:
    """SQLite con UNA conexión por hilo (`conn` es una propiedad).

    Los hilos de peticiones y los de los jobs en segundo plano nunca comparten conexión (cursores,
    transacciones y `lastrowid` quedan aislados). Modo WAL + busy_timeout para convivir en archivo.
    `:memory:` usa una BD en memoria compartida (URI única por instancia) con una conexión ancla.
    """

    def __init__(self, path: str | Path = "data/enevidencia.db") -> None:
        self._local = threading.local()
        self._all: list[tuple[weakref.ref, sqlite3.Connection]] = []  # (hilo, conexión)
        self._lock_all = threading.Lock()
        self._closed = False
        self._anchor: sqlite3.Connection | None = None
        if str(path) == ":memory:":
            self._memory = True
            self._target = f"file:enevidencia_mem_{uuid.uuid4().hex}?mode=memory&cache=shared"
            self._anchor = self._connect()  # mantiene viva la BD en memoria mientras exista el Storage
        else:
            self._memory = False
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            self._target = str(path)
        self.conn.executescript(SCHEMA)  # esquema y migraciones, en la conexión del hilo creador
        self._migrate()

    def _connect(self) -> sqlite3.Connection:
        c = sqlite3.connect(self._target, check_same_thread=False, timeout=5.0, uri=self._memory,
                            **({"factory": _SharedCacheConn} if self._memory else {}))
        c.row_factory = sqlite3.Row
        if self._memory:
            c.execute("PRAGMA read_uncommitted=1")  # cache compartida: evita "table is locked" entre lectores/escritor
        else:
            c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=5000")
        with self._lock_all:
            # libera las conexiones de hilos que ya terminaron
            alive = []
            for t, old in self._all:
                th = t()
                if th is not None and th.is_alive():
                    alive.append((t, old))
                else:
                    try:
                        old.close()
                    except sqlite3.Error:
                        pass
            alive.append((weakref.ref(threading.current_thread()), c))
            self._all = alive
        return c

    @property
    def conn(self) -> sqlite3.Connection:
        """Conexión propia del hilo que llama (se abre al primer uso)."""
        c = getattr(self._local, "conn", None)
        if c is None:
            if self._closed:
                raise sqlite3.ProgrammingError("Storage cerrado")
            c = self._local.conn = self._connect()
        return c

    def close(self) -> None:
        """Cierra todas las conexiones abiertas (de cualquier hilo) y la ancla en memoria."""
        self._closed = True
        with self._lock_all:
            conns = [c for _, c in self._all]
            self._all = []
        for c in conns:
            try:
                c.close()
            except sqlite3.Error:
                pass
        self._local = threading.local()
        self._anchor = None

    def _migrate(self) -> None:
        """Añade la columna status a bases de datos de la v0.1."""
        for table in ("sources", "claims"):
            cols = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if "status" not in cols:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN status TEXT NOT NULL DEFAULT 'accepted'")
        self.conn.commit()

    # --- hechos
    def add_fact(self, fact: Fact) -> Fact:
        cur = self.conn.execute("INSERT INTO facts(title, description) VALUES (?, ?)", (fact.title, fact.description))
        self.conn.commit()
        fact.id = cur.lastrowid
        return fact

    def get_fact(self, fact_id: int) -> Fact | None:
        r = self.conn.execute("SELECT * FROM facts WHERE id=?", (fact_id,)).fetchone()
        return Fact(r["title"], r["description"], r["id"]) if r else None

    def list_facts(self) -> list[Fact]:
        return [Fact(r["title"], r["description"], r["id"]) for r in self.conn.execute("SELECT * FROM facts ORDER BY id")]

    # --- fuentes
    def add_source(self, s: Source) -> Source:
        cur = self.conn.execute(
            "INSERT INTO sources(url,text,source_type,outlet,author,published,title,kind,classification_reason,retrieved_at,status)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (s.url, s.text, s.source_type.value, s.outlet, s.author,
             s.published.isoformat() if s.published else None, s.title, s.kind,
             s.classification_reason, s.retrieved_at.isoformat(), s.status.value),
        )
        self.conn.commit()
        s.id = cur.lastrowid
        return s

    @staticmethod
    def _source(r: sqlite3.Row) -> Source:
        return Source(
            url=r["url"], text=r["text"], source_type=SourceType(r["source_type"]), outlet=r["outlet"] or "",
            author=r["author"] or "", published=date.fromisoformat(r["published"]) if r["published"] else None,
            title=r["title"] or "", kind=r["kind"] or "", classification_reason=r["classification_reason"] or "",
            retrieved_at=datetime.fromisoformat(r["retrieved_at"]), status=Status(r["status"]), id=r["id"],
        )

    def get_source(self, source_id: int) -> Source | None:
        r = self.conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        return self._source(r) if r else None

    def list_sources(self, status: Status | None = None) -> list[Source]:
        """Todas las fuentes, o solo las de un estado de revisión."""
        q, args = ("SELECT * FROM sources WHERE status=? ORDER BY id", (status.value,)) if status \
            else ("SELECT * FROM sources ORDER BY id", ())
        return [self._source(r) for r in self.conn.execute(q, args)]

    def set_source_status(self, source_id: int, status: Status) -> None:
        self.conn.execute("UPDATE sources SET status=? WHERE id=?", (status.value, source_id))
        self.conn.commit()

    def set_source_type(self, source_id: int, source_type: SourceType, reason: str = "Corregido manualmente") -> None:
        self.conn.execute("UPDATE sources SET source_type=?, classification_reason=? WHERE id=?",
                          (source_type.value, reason, source_id))
        self.conn.commit()

    # --- afirmaciones
    def add_claim(self, c: Claim) -> Claim:
        cur = self.conn.execute(
            "INSERT INTO claims(statement,speaker,quote,value,source_id,fact_id,status) VALUES (?,?,?,?,?,?,?)",
            (c.statement, c.speaker, c.quote, c.value, c.source_id, c.fact_id, c.status.value),
        )
        self.conn.commit()
        c.id = cur.lastrowid
        return c

    def set_claim_status(self, claim_id: int, status: Status) -> None:
        self.conn.execute("UPDATE claims SET status=? WHERE id=?", (status.value, claim_id))
        self.conn.commit()

    def claims_for_fact(self, fact_id: int, status: Status | None = Status.ACCEPTED) -> list[ClaimRecord]:
        """Por defecto solo afirmaciones aceptadas (las que entran en comparación e informe). status=None → todas."""
        q, args = ("SELECT * FROM claims WHERE fact_id=? AND status=? ORDER BY id", (fact_id, status.value)) if status \
            else ("SELECT * FROM claims WHERE fact_id=? ORDER BY id", (fact_id,))
        out = []
        for r in self.conn.execute(q, args):
            claim = Claim(r["statement"], r["speaker"], r["quote"], r["source_id"], r["fact_id"], r["value"],
                          Status(r["status"]), r["id"])
            out.append(ClaimRecord(claim, self.get_source(r["source_id"])))
        return out

    # --- auditoría del flujo automático
    def add_audit_query(self, fact_id: int, query: str) -> None:
        self.conn.execute("INSERT INTO audit_queries(fact_id, query) VALUES (?,?)", (fact_id, query))
        self.conn.commit()

    def add_audit_source(self, fact_id: int, url: str, title: str, domain: str, decision: str, reason: str,
                         source_id: int | None = None) -> None:
        """decision: 'accepted' | 'excluded'."""
        self.conn.execute("INSERT INTO audit_sources(fact_id,url,title,domain,decision,reason,source_id) VALUES (?,?,?,?,?,?,?)",
                          (fact_id, url, title, domain, decision, reason, source_id))
        self.conn.commit()

    def add_audit_claim(self, fact_id: int, source_id: int | None, url: str, d: dict) -> None:
        self.conn.execute("INSERT INTO audit_claims(fact_id,source_id,url,statement,speaker,quote,reason) VALUES (?,?,?,?,?,?,?)",
                          (fact_id, source_id, url, str(d.get("statement", "")), str(d.get("speaker", "")),
                           str(d.get("quote", "")), str(d.get("reason", ""))))
        self.conn.commit()

    def audit(self, fact_id: int) -> dict:
        """Datos de auditoría: consultas, fuentes encontradas (con estado actual), afirmaciones descartadas."""
        q = [r["query"] for r in self.conn.execute("SELECT query FROM audit_queries WHERE fact_id=? ORDER BY id", (fact_id,))]
        srcs = [dict(r) for r in self.conn.execute(
            "SELECT a.*, s.status AS current_status, s.source_type AS current_type FROM audit_sources a "
            "LEFT JOIN sources s ON s.id=a.source_id WHERE a.fact_id=? ORDER BY a.id", (fact_id,))]
        claims = [dict(r) for r in self.conn.execute("SELECT * FROM audit_claims WHERE fact_id=? ORDER BY id", (fact_id,))]
        return {"queries": q, "sources": srcs, "discarded_claims": claims}

    def methodology(self, fact_id: int) -> dict:
        """Resumen para el informe y la auditoría. Los recuentos de aceptadas reflejan el estado ACTUAL
        (si el usuario corrigió algo después, se ve aquí)."""
        a = self.audit(fact_id)
        rows = self.conn.execute(
            "SELECT s.* FROM fact_sources f JOIN sources s ON s.id=f.source_id WHERE f.fact_id=? AND s.status=?",
            (fact_id, Status.ACCEPTED.value)).fetchall()
        by_type: dict[str, int] = {}
        by_domain: dict[str, int] = {}
        for r in rows:
            by_type[SourceType(r["source_type"]).label] = by_type.get(SourceType(r["source_type"]).label, 0) + 1
            host = urlsplit(r["url"]).netloc.lower()
            host = host[4:] if host.startswith("www.") else host
            by_domain[host] = by_domain.get(host, 0) + 1
        considered = len(a["sources"])
        accepted = len(rows)
        return {"queries": a["queries"], "considered": considered, "accepted": accepted,
                "excluded": max(considered - accepted, 0) if considered else 0,
                "by_type": by_type, "by_domain": by_domain, "automatic": bool(a["queries"] or considered)}

    # --- informe con redacción IA
    def save_report(self, fact_id: int, text: str, warnings: list[str], references: list[dict], model: str,
                    accepted_claims_count: int, error: str = "") -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO fact_reports(fact_id,text,warnings,references_json,created_at,model,"
            "accepted_claims_count,error) VALUES (?,?,?,?,?,?,?,?)",
            (fact_id, text, json.dumps(warnings, ensure_ascii=False), json.dumps(references, ensure_ascii=False),
             datetime.now().isoformat(timespec="seconds"), model, accepted_claims_count, error))
        self.conn.commit()

    def get_report(self, fact_id: int) -> dict | None:
        r = self.conn.execute("SELECT * FROM fact_reports WHERE fact_id=?", (fact_id,)).fetchone()
        if not r:
            return None
        return {"fact_id": r["fact_id"], "text": r["text"], "warnings": json.loads(r["warnings"]),
                "references": json.loads(r["references_json"]), "created_at": datetime.fromisoformat(r["created_at"]),
                "model": r["model"], "accepted_claims_count": r["accepted_claims_count"], "error": r["error"]}


class ConnSource:
    """Mixin: `conn` de la conexión del hilo actual si se le pasó un `Storage`, o la conexión dada tal cual."""

    _src: "Storage | sqlite3.Connection"

    @property
    def conn(self) -> sqlite3.Connection:
        return self._src.conn if isinstance(self._src, Storage) else self._src
