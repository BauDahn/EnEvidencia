"""Persistencia en SQLite."""
from __future__ import annotations

import sqlite3
from datetime import date, datetime
from pathlib import Path

from .models import Claim, ClaimRecord, Fact, Source, SourceType

SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY, url TEXT NOT NULL, text TEXT NOT NULL, source_type TEXT NOT NULL,
    outlet TEXT, author TEXT, published TEXT, title TEXT, kind TEXT,
    classification_reason TEXT, retrieved_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS claims (
    id INTEGER PRIMARY KEY, statement TEXT NOT NULL, speaker TEXT NOT NULL, quote TEXT NOT NULL,
    value TEXT NOT NULL DEFAULT '',
    source_id INTEGER NOT NULL REFERENCES sources(id),
    fact_id INTEGER NOT NULL REFERENCES facts(id)
);
"""


class Storage:
    def __init__(self, path: str | Path = "data/enevidencia.db") -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

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
            "INSERT INTO sources(url,text,source_type,outlet,author,published,title,kind,classification_reason,retrieved_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (s.url, s.text, s.source_type.value, s.outlet, s.author,
             s.published.isoformat() if s.published else None, s.title, s.kind,
             s.classification_reason, s.retrieved_at.isoformat()),
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
            retrieved_at=datetime.fromisoformat(r["retrieved_at"]), id=r["id"],
        )

    def get_source(self, source_id: int) -> Source | None:
        r = self.conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        return self._source(r) if r else None

    def list_sources(self) -> list[Source]:
        return [self._source(r) for r in self.conn.execute("SELECT * FROM sources ORDER BY id")]

    def set_source_type(self, source_id: int, source_type: SourceType, reason: str = "Corregido manualmente") -> None:
        self.conn.execute("UPDATE sources SET source_type=?, classification_reason=? WHERE id=?",
                          (source_type.value, reason, source_id))
        self.conn.commit()

    # --- afirmaciones
    def add_claim(self, c: Claim) -> Claim:
        cur = self.conn.execute(
            "INSERT INTO claims(statement,speaker,quote,value,source_id,fact_id) VALUES (?,?,?,?,?,?)",
            (c.statement, c.speaker, c.quote, c.value, c.source_id, c.fact_id),
        )
        self.conn.commit()
        c.id = cur.lastrowid
        return c

    def claims_for_fact(self, fact_id: int) -> list[ClaimRecord]:
        out = []
        for r in self.conn.execute("SELECT * FROM claims WHERE fact_id=? ORDER BY id", (fact_id,)):
            claim = Claim(r["statement"], r["speaker"], r["quote"], r["source_id"], r["fact_id"], r["value"], r["id"])
            out.append(ClaimRecord(claim, self.get_source(r["source_id"])))
        return out
