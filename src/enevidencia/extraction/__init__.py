"""Extracción de afirmaciones. El usuario confirma cada una; la cita debe estar en la fuente."""
from __future__ import annotations

import re

from ..models import Claim, Source, normalize_text


class QuoteNotInSource(ValueError):
    """La cita no aparece literalmente en el texto de la fuente."""


def build_claim(source: Source, fact_id: int, statement: str, speaker: str, quote: str, value: str = "") -> Claim:
    """Crea una afirmación verificando que su cita existe en la fuente (trazabilidad)."""
    if source.id is None:
        raise ValueError("La fuente debe estar guardada antes de extraer afirmaciones.")
    if normalize_text(quote) not in normalize_text(source.text):
        raise QuoteNotInSource("La cita no aparece literalmente en el texto de la fuente.")
    return Claim(statement=statement, speaker=speaker, quote=quote.strip(), source_id=source.id,
                 fact_id=fact_id, value=value.strip())


_SENTENCE = re.compile(r"(?<=[.!?])\s+")


def suggest_candidates(text: str) -> list[str]:
    """Frases que podrían contener afirmaciones (con cifras o entrecomillado). Solo sugerencias: no se guardan."""
    out = []
    for s in _SENTENCE.split(text):
        s = s.strip()
        if len(s) > 15 and (re.search(r"\d", s) or re.search(r"[\"“«]", s)):
            out.append(s)
    return out
