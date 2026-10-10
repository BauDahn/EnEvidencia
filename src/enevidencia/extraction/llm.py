"""Extracción de afirmaciones con LLM. El modelo PROPONE; el código verifica cada cita contra la fuente."""
from __future__ import annotations

from dataclasses import dataclass, field

from ..llm import LLMClient, LLMError
from ..models import Claim, Source, Status, normalize_text
from . import QuoteNotInSource, build_claim

CHUNK_SIZE = 6000
CHUNK_OVERLAP = 400


@dataclass
class ExtractionResult:
    claims: list[Claim] = field(default_factory=list)
    discarded: list[dict] = field(default_factory=list)
    error: str | None = None


PROMPT = """Eres un asistente que localiza afirmaciones verificables en un texto. No eres juez: no valores, no concluyas quién tiene razón.

Del TEXTO siguiente, extrae afirmaciones CONCRETAS y verificables (cifras, fechas, actos, decisiones){topic_line}.
Devuelve SOLO un array JSON (máximo {max_claims} elementos) con objetos de esta forma:
[{{"statement": "...", "speaker": "...", "quote": "...", "value": "..."}}]

Reglas:
- "quote" debe ser una copia LITERAL de un fragmento del texto, carácter por carácter, sin parafrasear ni resumir ni corregir.
- "speaker" es quien hizo la afirmación según el texto (persona, institución, juzgado, o el propio medio si es una afirmación del medio).
- "statement" es la afirmación en una frase neutra y concreta.
- "value" es el dato comparable (cifra, fecha, sí/no) o cadena vacía si no hay.
- No inventes nada que no esté en el texto. No valores ni concluyas.
- Si no hay afirmaciones concretas, devuelve [].

TEXTO:
\"\"\"
{text}
\"\"\"
"""


def split_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Trocea por tramos de ~size caracteres con solape, cortando en espacio si es posible."""
    if len(text) <= size:
        return [text]
    chunks, start = [], 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            cut = text.rfind(" ", start + size // 2, end)
            if cut > start:
                end = cut
        chunks.append(text[start:end])
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return chunks


def _items(response) -> list:
    if isinstance(response, dict):
        for key in ("claims", "afirmaciones", "items"):
            if isinstance(response.get(key), list):
                return response[key]
        raise ValueError("El JSON devuelto no es una lista de afirmaciones.")
    if isinstance(response, list):
        return response
    raise ValueError("El JSON devuelto no es una lista de afirmaciones.")


def extract_claims(source: Source, fact_id: int, llm: LLMClient, topic: str = "",
                   max_claims: int = 15) -> ExtractionResult:
    """Propone afirmaciones (status PROPOSED) cuya cita literal ha sido verificada en el texto."""
    if source.id is None:
        raise ValueError("La fuente debe estar guardada antes de extraer afirmaciones.")
    result = ExtractionResult()
    if not source.text.strip():
        return result
    topic_line = f", relacionadas con: {topic}" if topic.strip() else ""
    seen: set[tuple[str, str, str]] = set()
    errors: list[str] = []

    for chunk in split_text(source.text):
        prompt = PROMPT.format(topic_line=topic_line, max_claims=max_claims, text=chunk)
        try:
            items = _items(llm.generate_json(prompt))
        except LLMError as e:
            errors.append(f"Fallo del LLM: {e}")
            continue
        except ValueError as e:
            errors.append(str(e))
            continue
        for item in items:
            if not isinstance(item, dict):
                result.discarded.append({"item": item, "reason": "Elemento con forma inválida (no es un objeto)."})
                continue
            fields = {k: ("" if item.get(k) is None else str(item.get(k))) for k in ("statement", "speaker", "quote", "value")}
            try:
                claim = build_claim(source, fact_id, fields["statement"], fields["speaker"],
                                    fields["quote"], fields["value"])
            except QuoteNotInSource:
                result.discarded.append({**fields, "reason": "La cita no aparece literalmente en la fuente."})
                continue
            except ValueError as e:
                result.discarded.append({**fields, "reason": str(e)})
                continue
            key = (normalize_text(claim.statement), normalize_text(claim.speaker), normalize_text(claim.quote))
            if key in seen:
                continue
            if len(result.claims) >= max_claims:
                result.discarded.append({**fields, "reason": f"Se superó el máximo de {max_claims} afirmaciones."})
                continue
            seen.add(key)
            claim.status = Status.PROPOSED
            result.claims.append(claim)

    if errors:
        result.error = "; ".join(dict.fromkeys(errors))
    return result
