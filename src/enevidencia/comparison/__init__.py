"""Comparación de afirmaciones por hecho. Muestra coincidencias y contradicciones; nunca un veredicto."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..models import ClaimRecord

AGREE, DIFFER, NO_VALUE = "coinciden", "se contradicen", "sin valor comparable"


def normalize_value(value: str) -> str:
    """Normaliza '2.650 €' y '2650€' al mismo valor comparable."""
    v = value.strip().lower()
    v = re.sub(r"(?<=\d)[.\s](?=\d{3}\b)", "", v)  # separador de miles
    return re.sub(r"\s+", "", v)


@dataclass
class ValueGroup:
    value: str                       # valor tal como lo escribió la primera afirmación
    records: list[ClaimRecord] = field(default_factory=list)


@dataclass
class Comparison:
    fact_id: int
    groups: list[ValueGroup]
    without_value: list[ClaimRecord]
    status: str                      # descriptivo: coinciden / se contradicen / sin valor comparable

    @property
    def total(self) -> int:
        return sum(len(g.records) for g in self.groups) + len(self.without_value)


def compare(fact_id: int, records: list[ClaimRecord]) -> Comparison:
    groups: dict[str, ValueGroup] = {}
    without = []
    for r in records:
        if not r.claim.value.strip():
            without.append(r)
            continue
        groups.setdefault(normalize_value(r.claim.value), ValueGroup(r.claim.value)).records.append(r)
    if not groups:
        status = NO_VALUE
    elif len(groups) == 1:
        status = AGREE
    else:
        status = DIFFER
    return Comparison(fact_id, list(groups.values()), without, status)
