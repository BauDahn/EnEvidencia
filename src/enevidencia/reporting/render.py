"""Ayudas de presentación del informe en página (se escapa TODO el texto externo)."""
from __future__ import annotations

import re
from datetime import date

from markupsafe import Markup, escape

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


def narrative_blocks(body: str, valid: set[int]) -> list[dict]:
    """Texto del LLM -> bloques (h/p/ul). Se escapa TODO y solo después se enlazan las referencias [n]
    existentes en la lista de fuentes (ancla #fn-n)."""
    def link(text: str) -> Markup:
        def sub(m: re.Match) -> str:
            n = int(m.group(1))
            return f'<a class="ref" href="#fn-{n}">[{n}]</a>' if n in valid else m.group(0)
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
