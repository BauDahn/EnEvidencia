"""Modelo de datos. Toda afirmación lleva fuente y cita textual."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum


class SourceType(str, Enum):
    """Clasificación descriptiva (no es un juicio de fiabilidad)."""

    PRIMARY = "primary"
    SECONDARY = "secondary"
    INSTITUTIONAL = "institutional"

    @property
    def label(self) -> str:
        return {"primary": "Primaria", "secondary": "Secundaria", "institutional": "Institucional"}[self.value]


class Status(str, Enum):
    """Estado de revisión. Lo propuesto automáticamente no es definitivo hasta que el usuario lo acepta."""

    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


def normalize_text(text: str) -> str:
    """Colapsa espacios y unifica comillas para comparar citas con el original."""
    text = text.translate(str.maketrans({"“": '"', "”": '"', "«": '"', "»": '"', "‘": "'", "’": "'"}))
    return re.sub(r"\s+", " ", text).strip().lower()


@dataclass
class Fact:
    """Un hecho concreto sobre el que se recopilan afirmaciones."""

    title: str
    description: str = ""
    id: int | None = None


@dataclass
class Source:
    """Fuente completa con el texto original."""

    url: str
    text: str
    source_type: SourceType
    outlet: str = ""          # medio o institución
    author: str = ""          # autor de la fuente
    published: date | None = None
    title: str = ""
    kind: str = ""            # entrevista, sentencia, noticia...
    classification_reason: str = ""
    retrieved_at: datetime = field(default_factory=datetime.now)
    status: Status = Status.ACCEPTED
    id: int | None = None


@dataclass
class Claim:
    """Afirmación concreta, atribuida a quien la dijo y anclada a una fuente."""

    statement: str            # "La renta exigida era de 2.650 €"
    speaker: str              # quién lo dijo
    quote: str                # cita textual literal de la fuente
    source_id: int
    fact_id: int
    value: str = ""           # valor comparable opcional ("2650 €")
    status: Status = Status.ACCEPTED
    id: int | None = None

    def __post_init__(self) -> None:
        if not self.statement.strip():
            raise ValueError("La afirmación no puede estar vacía.")
        if not self.speaker.strip():
            raise ValueError("Hay que indicar quién hizo la afirmación.")
        if not self.quote.strip():
            raise ValueError("Cada afirmación necesita una cita textual de la fuente.")


@dataclass
class ClaimRecord:
    """Afirmación junto a los datos de su fuente (vista trazable completa)."""

    claim: Claim
    source: Source
