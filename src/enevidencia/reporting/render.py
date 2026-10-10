"""Informe HTML autocontenido (estilo dossier impreso). Sin CDNs ni dependencia de /static: se puede guardar y enviar."""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Callable

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup, escape

from ..comparison import compare
from ..models import ClaimRecord, Fact
from ..storage import Storage

DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre",
         "noviembre", "diciembre"]
REF_RE = re.compile(r"\[(\d+)\]")


def fecha_es(d: date | None = None) -> str:
    """'sábado, 10 de octubre de 2026' (sin depender de la configuración regional del sistema)."""
    d = d or date.today()
    return f"{DIAS[d.weekday()]}, {d.day} de {MESES[d.month - 1]} de {d.year}"


def fecha_corta(d: date | None) -> str:
    return f"{d.day} {MESES[d.month - 1][:3]}. {d.year}" if d else "fecha desconocida"


def safe_href(url: str) -> str:
    """Solo enlaces http(s) clicables; cualquier otro esquema (javascript:, data:...) se neutraliza."""
    return url if str(url).lower().startswith(("http://", "https://")) else "#"


def _env() -> Environment:
    env = Environment(loader=FileSystemLoader(str(Path(__file__).parent / "templates")),
                      autoescape=select_autoescape(["html"], default=True))
    env.filters["safe_href"] = safe_href
    env.filters["fecha"] = fecha_corta
    return env


_ENV = _env()


def narrative_blocks(body: str, remap: dict[int, int]) -> list[dict]:
    """Texto del LLM -> bloques (h/p/ul). Se escapa TODO y solo después se enlazan las referencias [n]
    (renumeradas a la numeración del informe)."""
    def link(text: str) -> Markup:
        def sub(m: re.Match) -> str:
            n = remap.get(int(m.group(1)))
            return f'<a class="ref" href="#fn-{n}">[{n}]</a>' if n else m.group(0)
        return Markup(REF_RE.sub(sub, str(escape(text))))

    blocks: list[dict] = []
    para: list[str] = []

    def flush() -> None:
        if para:
            blocks.append({"kind": "p", "html": link(" ".join(para))})
            para.clear()

    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            flush()
        elif line.startswith("#"):
            flush()
            blocks.append({"kind": "h", "html": link(line.lstrip("#").strip())})
        elif line.startswith(("- ", "* ")):
            flush()
            if blocks and blocks[-1]["kind"] == "ul":
                blocks[-1]["items"].append(link(line[2:]))
            else:
                blocks.append({"kind": "ul", "items": [link(line[2:])]})
        else:
            para.append(line)
    flush()
    return blocks


def _entry(n: int, r: ClaimRecord) -> dict:
    c, s = r.claim, r.source
    return {"n": n, "id": c.id, "speaker": c.speaker, "statement": c.statement, "quote": c.quote, "value": c.value,
            "outlet": s.outlet or s.title or "Fuente", "type": s.source_type.label, "published": s.published}


def build_context(storage: Storage, fact: Fact, ai: dict | None = None, controls: dict | None = None) -> dict:
    from . import DISCLAIMER
    records = storage.claims_for_fact(fact.id)
    cmp = compare(fact.id, records)
    nums: dict[int, int] = {}                 # source_id -> n (misma numeración que el informe .md)
    firsts: dict[int, ClaimRecord] = {}

    def num(r: ClaimRecord) -> int:
        if r.source.id not in nums:
            nums[r.source.id] = len(nums) + 1
            firsts[r.source.id] = r
        return nums[r.source.id]

    groups = [{"value": g.value, "entries": [_entry(num(r), r) for r in g.records]} for g in cmp.groups]
    without = [_entry(num(r), r) for r in cmp.without_value]
    sources = []
    for sid, n in sorted(nums.items(), key=lambda kv: kv[1]):
        s = firsts[sid].source
        sources.append({"n": n, "title": s.title, "outlet": s.outlet, "author": s.author, "published": s.published,
                        "type": s.source_type.label, "url": s.url})
    ctx = {"fact": fact, "today": fecha_es(), "status": cmp.status, "total": cmp.total, "groups": groups,
           "without": without, "m": storage.methodology(fact.id), "sources": sources, "disclaimer": DISCLAIMER,
           "controls": controls, "ai": ai or {"state": "none"}, "nsources": len(sources)}
    if ctx["ai"].get("state") == "ok":
        remap = {n: nums[sid] for n, sid in ctx["ai"]["refs"].items() if sid in nums}
        ctx["ai"] = {**ctx["ai"], "blocks": narrative_blocks(ctx["ai"]["body"], remap)}
    return ctx


def render_report_html(storage: Storage, fact: Fact, ai: dict | None = None, controls: dict | None = None) -> str:
    return _ENV.get_template("report.html").render(**build_context(storage, fact, ai, controls))
