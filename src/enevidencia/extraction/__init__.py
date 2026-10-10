"""Extracción de afirmaciones. El usuario confirma cada una; la cita debe estar en la fuente."""
from __future__ import annotations

import re
import unicodedata

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


# ---- verificación de ATRIBUCIÓN (la cita real no basta: debe ser de quien se dice)
ATTRIBUTION_WINDOW = 400
_GENERIC = {"senor", "senora", "doctor", "doctora", "presidente", "presidenta", "ministro", "ministra", "portavoz",
            "fuentes", "segun", "para", "como", "esta", "este", "desde", "entre", "sobre"}


def _plain(text: str) -> str:
    t = unicodedata.normalize("NFKD", normalize_text(text))
    t = "".join(c for c in t if not unicodedata.combining(c))
    return " " + " ".join(re.sub(r"[^\w]+", " ", t).split()) + " "


def _aliases(speaker: str) -> list[str]:
    full = _plain(speaker).strip()
    tokens = [w for w in full.split() if len(w) >= 4 and w not in _GENERIC]
    return ([full] if full else []) + tokens


def attribution_ok(source: Source, speaker: str, quote: str) -> bool:
    """True si el hablante (o un alias: apellido, institución sin tildes ni mayúsculas) aparece en la cita o en una
    ventana de ~400 caracteres alrededor de ella en la fuente, o es el medio/autor de la propia fuente."""
    aliases = _aliases(speaker)
    if not aliases:
        return False
    for meta in (source.outlet, source.author):
        m = _plain(meta or "")
        if m.strip() and any(f" {a.strip()} " in m or m.strip() in f" {a.strip()} " for a in aliases):
            return True
    text, q = _plain(source.text), _plain(quote).strip()
    if not q:
        return False
    start = 0
    for _ in range(100):
        pos = text.find(q, start)
        if pos < 0:
            break
        window = text[max(0, pos - ATTRIBUTION_WINDOW): pos + len(q) + ATTRIBUTION_WINDOW]
        if any(f" {a.strip()} " in window for a in aliases):
            return True
        start = pos + 1
    return False
