"""Agrupación de afirmaciones por punto concreto con ayuda del LLM.

El LLM solo AGRUPA y PROPONE valores normalizados. Las contradicciones las decide el código
(`comparison.compare`); el estado es descriptivo, nunca un veredicto de verdad.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, replace

from ..llm import LLMClient, LLMError
from ..models import ClaimRecord
from . import Comparison, compare

UNGROUPED = "Sin agrupar"

PROMPT = """Agrupa estas afirmaciones según el PUNTO CONCRETO del que tratan (por ejemplo, \
"importe de la renta exigida"). Afirmaciones que tratan del mismo punto van en el mismo grupo; \
cada afirmación debe estar en un solo grupo. No juzgues cuál es verdadera ni si se contradicen.
Para cada afirmación con valor, propón ese valor normalizado (mismo formato para el mismo dato, \
p. ej. "2650 EUR"); si no hay valor, omítela de "normalized".

Responde solo con JSON:
{{"groups": [{{"topic": "...", "claim_ids": [1, 2]}}], "normalized": {{"1": "2650 EUR"}}}}

Afirmaciones:
{claims}
"""


@dataclass
class ClaimGroup:
    topic: str
    claim_ids: list[int]
    normalized: dict[int, str] = field(default_factory=dict)  # claim_id -> valor normalizado propuesto


def group_claims(records: list[ClaimRecord], llm: LLMClient) -> list[ClaimGroup]:
    """Propone grupos; valida ids y garantiza que cada afirmación quede en exactamente un grupo."""
    by_id = {r.claim.id: r for r in records if r.claim.id is not None}
    if not by_id:
        return []
    listing = json.dumps(
        [{"claim_id": i, "statement": r.claim.statement, "speaker": r.claim.speaker, "value": r.claim.value}
         for i, r in by_id.items()], ensure_ascii=False)
    try:
        data = llm.generate_json(PROMPT.format(claims=listing))
        raw_groups = data["groups"]
        raw_norm = data.get("normalized") or {}
        if not isinstance(raw_groups, list) or not isinstance(raw_norm, dict):
            raise TypeError
    except (LLMError, KeyError, TypeError, AttributeError):
        raw_groups, raw_norm = [], {}

    norm: dict[int, str] = {}
    for k, v in raw_norm.items():
        try:
            cid = int(k)
        except (ValueError, TypeError):
            continue
        if cid in by_id and isinstance(v, (str, int, float)) and str(v).strip():
            norm[cid] = str(v).strip()

    seen: set[int] = set()
    groups: list[ClaimGroup] = []
    for g in raw_groups:
        if not isinstance(g, dict) or not isinstance(g.get("claim_ids"), list):
            continue
        ids: list[int] = []
        for c in g["claim_ids"]:
            if isinstance(c, bool) or not isinstance(c, int):
                continue
            if c in by_id and c not in seen:  # ids inventados o repetidos se descartan
                seen.add(c)
                ids.append(c)
        if ids:
            topic = str(g.get("topic", "")).strip() or "Sin título"
            groups.append(ClaimGroup(topic, ids, {i: norm[i] for i in ids if i in norm}))
    rest = [i for i in by_id if i not in seen]
    if rest:
        groups.append(ClaimGroup(UNGROUPED, rest, {i: norm[i] for i in rest if i in norm}))
    return groups


def compare_grouped(fact_id: int, records: list[ClaimRecord],
                    groups: list[ClaimGroup]) -> list[tuple[ClaimGroup, Comparison]]:
    """Aplica `compare` a cada grupo. Si hay valor normalizado propuesto, se usa en lugar del original."""
    by_id = {r.claim.id: r for r in records}
    out = []
    for g in groups:
        recs = []
        for cid in g.claim_ids:
            r = by_id.get(cid)
            if r is None:
                continue
            nv = g.normalized.get(cid)
            if nv and r.claim.value.strip():
                r = ClaimRecord(replace(r.claim, value=nv), r.source)
            recs.append(r)
        out.append((g, compare(fact_id, recs)))
    return out
