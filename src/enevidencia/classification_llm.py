"""Clasificación asistida por IA: solo se consulta al LLM si ninguna regla YAML aplicó.

La clasificación es DESCRIPTIVA (tipo de documento), nunca un juicio de fiabilidad.
El resultado es una propuesta: el usuario puede corregirla a mano.
"""
from __future__ import annotations

from .classification import classify
from .llm import LLMClient, LLMError
from .models import Source, SourceType

DEFAULT_REASON = "Sin regla aplicable: valor por defecto"
AI_PREFIX = "Propuesto por IA: "
MAX_CHARS = 1500

PROMPT = """Clasifica el TIPO DE DOCUMENTO de una fuente. La clasificación es DESCRIPTIVA: indica qué \
clase de documento es, NO si es fiable, veraz o de calidad. No emitas juicios de fiabilidad.

Tipos:
- primary: el documento es la declaración en sí (entrevista, sentencia, comunicado).
- secondary: pieza que cita o resume a terceros (noticia, artículo, reportaje).
- institutional: publicada por una administración u organismo público.

Responde solo con JSON: {{"type": "primary" | "secondary" | "institutional", "reason": "motivo breve en español"}}

URL: {url}
Medio: {outlet}
Título: {title}
Texto (inicio):
{text}
"""


def classify_with_fallback(source: Source, llm: LLMClient, rules: dict | None = None) -> tuple[SourceType, str]:
    """Reglas YAML primero; el LLM solo si ninguna aplicó. Ante fallo, devuelve el valor por defecto."""
    stype, reason = classify(source.url, source.kind, rules)
    if reason != DEFAULT_REASON:  # alguna regla YAML aplicó
        return stype, reason
    prompt = PROMPT.format(url=source.url, outlet=source.outlet, title=source.title,
                           text=(source.text or "")[:MAX_CHARS])
    try:
        data = llm.generate_json(prompt)
        ai_type = SourceType(str(data["type"]).strip().lower())
        ai_reason = str(data.get("reason", "")).strip() or "sin motivo indicado"
    except (LLMError, KeyError, ValueError, TypeError, AttributeError):
        return stype, reason
    return ai_type, AI_PREFIX + ai_reason
