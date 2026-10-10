"""Narrativa redactada por LLM a partir de afirmaciones ACEPTADAS. El código valida las referencias."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..llm import FENCE_NOTICE, LLMClient, LLMError, fence
from ..models import ClaimRecord, Fact, Source
from ..storage import Storage
from . import _ref_line, numbering, reference_entry

REF_RE = re.compile(r"\[(\d+)\]")
LONG_SENTENCE_WORDS = 8
NO_CLAIMS_TEXT = "No hay afirmaciones aceptadas para este hecho; no se ha redactado ninguna narrativa."


class NarrativeRejected(RuntimeError):
    """El texto del LLM no supera la validación (p. ej. cita una referencia inexistente)."""


@dataclass
class NarrativeResult:
    text: str
    references: dict[int, Source] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    body: str = ""              # solo el texto narrativo
    entries: list[dict] = field(default_factory=list)   # lista numerada de fuentes (misma numeración que el texto)


def build_prompt(fact: Fact, records: list[ClaimRecord], nums: dict[int, int], first: dict[int, ClaimRecord]) -> str:
    src = fence("\n".join(_ref_line(n, r) for n, r in sorted(first.items())))
    cl = fence("\n".join(
        f"- [{nums[r.source.id]}] Autor: {r.claim.speaker} | Afirmación: {r.claim.statement} | "
        f"Valor: {r.claim.value or '(sin valor)'} | Cita: «{r.claim.quote}»" for r in records))
    return (
        "Redacta en español un texto narrativo breve, neutral y descriptivo sobre el hecho indicado, "
        "usando SOLO las afirmaciones listadas.\n"
        "Reglas estrictas:\n"
        "- No añadas información externa ni inferencias.\n"
        "- Cada frase factual debe terminar con la referencia [n] de su fuente.\n"
        "- Atribuye siempre ('según X', 'X afirma').\n"
        "- Señala coincidencias y discrepancias entre fuentes sin decir cuál es verdadera y sin lenguaje valorativo.\n"
        "- No concluyas ni emitas veredictos.\n"
        "- No uses adjetivos sobre las personas implicadas.\n"
        "- No incluyas una sección de fuentes; solo usa las referencias [n] dadas.\n"
        f"- {FENCE_NOTICE}\n\n"
        f"HECHO: {fact.title}\n{fact.description}\n\nFUENTES:\n{src}\n\nAFIRMACIONES ACEPTADAS:\n{cl}\n")


def _unreferenced_sentences(text: str) -> list[str]:
    out = []
    for s in re.split(r"(?<=[.!?])\s+|\n+", text):
        s = s.strip()
        if len(s.split()) >= LONG_SENTENCE_WORDS and not REF_RE.search(s) and not s.startswith("#"):
            out.append(s)
    return out


def draft_narrative(storage: Storage, fact: Fact, llm: LLMClient) -> NarrativeResult:
    records = storage.claims_for_fact(fact.id)
    if not records:
        return NarrativeResult(NO_CLAIMS_TEXT, {}, ["Sin afirmaciones aceptadas: no se llamó al LLM."], NO_CLAIMS_TEXT)
    nums, first = numbering(records)
    refs = {n: r.source for n, r in first.items()}
    prompt = build_prompt(fact, records, nums, first)

    body = ""
    for attempt in range(2):
        try:
            new = llm.generate_text(prompt).strip()
        except LLMError:
            if attempt == 0 or not body:
                raise
            break  # el reintento por frases sin referencia falló: se conserva el primer borrador con avisos
        invalid = sorted({int(m) for m in REF_RE.findall(new)} - set(refs))
        loose = [] if invalid else _unreferenced_sentences(new)
        if invalid:
            if attempt == 1:
                raise NarrativeRejected(f"El texto cita referencias inexistentes: {invalid}")
            prompt += (f"\n\nAVISO: tu respuesta anterior citó referencias inexistentes {invalid}. "
                       f"Usa solo {sorted(refs)}.\n")
            continue
        body = new
        if not loose and REF_RE.search(new):
            break
        if attempt == 0:
            prompt += ("\n\nAVISO: tu respuesta anterior tenía frases factuales sin referencia [n]. "
                       "Cada frase factual debe terminar con [n]; no añadas frases sin referencia.\n")
    warnings = []
    if not REF_RE.search(body):
        warnings.append("El texto no contiene ninguna referencia [n].")
    for s in _unreferenced_sentences(body):
        warnings.append(f"Frase sin referencia: «{s[:100]}»")
    entries = [reference_entry(n, r) for n, r in sorted(first.items())]
    return NarrativeResult(body, refs, warnings, body, entries)


def generate_report(storage: Storage, fact: Fact, llm: LLMClient, model: str = "") -> dict:
    """Redacta UNA vez y guarda en BD (fact_reports). Una narrativa rechazada se guarda con su motivo.
    Un LLMError (cuota, red) se propaga sin guardar nada: no se pierde el informe anterior."""
    n_claims = len(storage.claims_for_fact(fact.id))
    if n_claims == 0:
        raise NarrativeRejected(NO_CLAIMS_TEXT)
    try:
        res = draft_narrative(storage, fact, llm)
    except NarrativeRejected as e:
        storage.save_report(fact.id, "", [], [], model, n_claims, error=str(e))
    else:
        storage.save_report(fact.id, res.body, res.warnings, res.entries, model, n_claims)
    return storage.get_report(fact.id)
