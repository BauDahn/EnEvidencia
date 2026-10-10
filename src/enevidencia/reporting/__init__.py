"""Informe con cita a cada fuente. Sin conclusiones sobre qué es verdad."""
from __future__ import annotations

from datetime import date

from ..comparison import Comparison, compare
from ..models import ClaimRecord, Fact
from ..storage import Storage

DISCLAIMER = ("Este informe muestra lo que dice cada fuente. No determina cuál es la verdad: "
              "la clasificación de fuentes es descriptiva, no un juicio de fiabilidad.")


def methodology_lines(m: dict) -> list[str]:
    """Apartado breve de metodología: qué se buscó y cómo se repartieron las fuentes (para ver el equilibrio)."""
    out = ["## Metodología", ""]
    if not m["automatic"]:
        out += ["Las fuentes de este hecho se añadieron manualmente; no hubo búsqueda automática.", ""]
    else:
        out.append("Consultas utilizadas: " + "; ".join(f"«{q}»" for q in m["queries"]) + "." if m["queries"]
                   else "Consultas utilizadas: no registradas.")
        out.append(f"Fuentes consideradas: {m['considered']} · aceptadas: {m['accepted']} · excluidas: {m['excluded']} "
                   "(criterios objetivos en config/pipeline.yaml; motivos en la página de auditoría).")
        if m["by_type"]:
            out.append("Aceptadas por tipo: " + ", ".join(f"{k} {v}" for k, v in sorted(m["by_type"].items())) + ".")
        if m["by_domain"]:
            out.append("Aceptadas por medio/dominio: " + ", ".join(f"{k} {v}" for k, v in sorted(m["by_domain"].items())) + ".")
        out += ["", "El buscador y la IA que propone consultas pueden introducir sesgos que el código no elimina; "
                "por eso se muestran las consultas y todas las exclusiones. El sistema no decide la verdad.", ""]
    return out


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
    lines += methodology_lines(storage.methodology(fact.id))
    lines.append("## Fuentes")
    seen = {}
    for r in records:
        seen.setdefault(r.source.id, r)
    for sid, n in sorted(refs.items(), key=lambda kv: kv[1]):
        lines.append(_ref_line(n, seen[sid]))
    return "\n".join(lines) + "\n"


def render_html(storage: Storage, fact: Fact, controls: dict | None = None) -> str:
    """Informe HTML autocontenido (plantilla templates/report.html). Sin `controls` es la versión descargable."""
    from .render import render_report_html
    return render_report_html(storage, fact, controls=controls)
