"""Informe con cita a cada fuente. Sin conclusiones sobre qué es verdad."""
from __future__ import annotations

import html
from datetime import date

from ..comparison import Comparison, compare
from ..models import ClaimRecord, Fact
from ..storage import Storage

DISCLAIMER = ("Este informe muestra lo que dice cada fuente. No determina cuál es la verdad: "
              "la clasificación de fuentes es descriptiva, no un juicio de fiabilidad.")


def _ref_line(n: int, r: ClaimRecord) -> str:
    s = r.source
    parts = [p for p in [s.author, s.outlet, s.title] if p]
    when = s.published.isoformat() if s.published else "fecha desconocida"
    return f"[{n}] {', '.join(parts) or 'Fuente'} ({when}) · {s.source_type.label} · {s.url}"


def render_markdown(storage: Storage, fact: Fact) -> str:
    records = storage.claims_for_fact(fact.id)
    cmp: Comparison = compare(fact.id, records)
    refs: dict[int, int] = {}                 # source_id -> número de nota

    def ref(r: ClaimRecord) -> int:
        return refs.setdefault(r.source.id, len(refs) + 1)

    lines = [f"# {fact.title}", ""]
    if fact.description:
        lines += [fact.description, ""]
    lines += [f"*Generado el {date.today().isoformat()}* · Estado entre fuentes: **{cmp.status}**", "",
              f"> {DISCLAIMER}", ""]

    def block(title: str, rs: list[ClaimRecord]) -> None:
        lines.append(f"## {title}")
        for r in rs:
            c = r.claim
            lines.extend([f"- **{c.speaker}**: {c.statement} [{ref(r)}]", f"  > «{c.quote}»"])
        lines.append("")

    for g in cmp.groups:
        block(f"Valor: {g.value}", g.records)
    if cmp.without_value:
        block("Afirmaciones sin valor comparable", cmp.without_value)
    lines.append("## Fuentes")
    seen = {}
    for r in records:
        seen.setdefault(r.source.id, r)
    for sid, n in sorted(refs.items(), key=lambda kv: kv[1]):
        lines.append(_ref_line(n, seen[sid]))
    return "\n".join(lines) + "\n"


def render_html(storage: Storage, fact: Fact) -> str:
    body = html.escape(render_markdown(storage, fact))
    return (f"<!doctype html><meta charset='utf-8'><title>{html.escape(fact.title)}</title>"
            f"<pre style='white-space:pre-wrap;font-family:sans-serif;max-width:50rem;margin:2rem auto'>{body}</pre>")
