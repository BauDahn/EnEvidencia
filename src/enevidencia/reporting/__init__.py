"""Informe en página: numeración única de fuentes y texto de descargo. Sin conclusiones sobre qué es verdad."""
from __future__ import annotations

from ..models import ClaimRecord

DISCLAIMER = ("Este informe muestra lo que dice cada fuente. No determina cuál es la verdad: "
              "la clasificación de fuentes es descriptiva, no un juicio de fiabilidad.")


def numbering(records: list[ClaimRecord]) -> tuple[dict[int, int], dict[int, ClaimRecord]]:
    """ÚNICA fuente de numeración [n]: source_id -> n (orden de primera aparición) y n -> primer registro.
    La usan tanto el prompt de la narrativa como la lista de fuentes mostrada."""
    nums: dict[int, int] = {}
    first: dict[int, ClaimRecord] = {}
    for r in records:
        if r.source.id not in nums:
            nums[r.source.id] = len(nums) + 1
            first[nums[r.source.id]] = r
    return nums, first


def reference_entry(n: int, r: ClaimRecord) -> dict:
    s = r.source
    return {"n": n, "source_id": s.id, "title": s.title, "outlet": s.outlet, "author": s.author,
            "published": s.published.isoformat() if s.published else None, "type": s.source_type.label, "url": s.url}


def _ref_line(n: int, r: ClaimRecord) -> str:
    s = r.source
    parts = [p for p in [s.author, s.outlet, s.title] if p]
    when = s.published.isoformat() if s.published else "fecha desconocida"
    return f"[{n}] {', '.join(parts) or 'Fuente'} ({when}) · {s.source_type.label} · {s.url}"
