"""Narrativa redactada por LLM a partir de afirmaciones ACEPTADAS. El código valida las referencias."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..llm import LLMClient, LLMError
from ..models import ClaimRecord, Fact, Source
from ..storage import Storage
from . import DISCLAIMER, _ref_line, render_markdown

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
    body: str = ""              # solo el texto narrativo, sin lista de fuentes ni aviso


def _numbering(records: list[ClaimRecord]) -> tuple[dict[int, int], dict[int, ClaimRecord]]:
    """source_id -> n (orden de aparición) y n -> primer registro de esa fuente."""
    nums: dict[int, int] = {}
    first: dict[int, ClaimRecord] = {}
    for r in records:
        if r.source.id not in nums:
            nums[r.source.id] = len(nums) + 1
            first[nums[r.source.id]] = r
    return nums, first


def build_prompt(fact: Fact, records: list[ClaimRecord], nums: dict[int, int], first: dict[int, ClaimRecord]) -> str:
    src = "\n".join(_ref_line(n, r) for n, r in sorted(first.items()))
    cl = "\n".join(
        f"- [{nums[r.source.id]}] Autor: {r.claim.speaker} | Afirmación: {r.claim.statement} | "
        f"Valor: {r.claim.value or '(sin valor)'} | Cita: «{r.claim.quote}»" for r in records)
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
        "- No incluyas una sección de fuentes; solo usa las referencias [n] dadas.\n\n"
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
    nums, first = _numbering(records)
    refs = {n: r.source for n, r in first.items()}
    prompt = build_prompt(fact, records, nums, first)

    body = ""
    for attempt in range(2):
        body = llm.generate_text(prompt).strip()
        invalid = sorted({int(m) for m in REF_RE.findall(body)} - set(refs))
        if not invalid:
            break
        if attempt == 1:
            raise NarrativeRejected(f"El texto cita referencias inexistentes: {invalid}")
        prompt += (f"\n\nAVISO: tu respuesta anterior citó referencias inexistentes {invalid}. "
                   f"Usa solo {sorted(refs)}.\n")
    warnings = []
    if not REF_RE.search(body):
        warnings.append("El texto no contiene ninguna referencia [n].")
    for s in _unreferenced_sentences(body):
        warnings.append(f"Frase sin referencia: «{s[:100]}»")
    sources = "\n".join(_ref_line(n, r) for n, r in sorted(first.items()))
    text = f"{body}\n\n## Fuentes\n{sources}\n\n> {DISCLAIMER}\n"
    return NarrativeResult(text, refs, warnings, body)


def render_full_report(storage: Storage, fact: Fact, llm: LLMClient | None = None) -> str:
    report = render_markdown(storage, fact)
    if llm is None:
        return report
    try:
        res = draft_narrative(storage, fact, llm)
    except (NarrativeRejected, LLMError) as e:
        # No se silencia: el informe indica que la narrativa de IA no se incluyó y por qué.
        return (f"{report}\n## Narrativa de IA no incluida\n\n"
                f"La redacción asistida por IA se rechazó o falló ({e}). Este informe contiene solo "
                f"las afirmaciones aceptadas, citadas por el código.\n")
    extra = "\n".join(f"- Aviso: {w}" for w in res.warnings)
    return (f"{report}\n## Resumen redactado (asistido por IA, revisar)\n\n{res.text}"
            + (f"\n{extra}\n" if extra else ""))


def render_full_html(storage: Storage, fact: Fact, llm: LLMClient | None = None, controls: dict | None = None,
                     scrub=str) -> str:
    """Informe HTML autocontenido; con `llm`, añade la narrativa asistida por IA claramente rotulada."""
    from .render import render_report_html
    ai: dict = {"state": "none"}
    if llm is not None:
        try:
            res = draft_narrative(storage, fact, llm)
            ai = {"state": "ok", "body": res.body, "warnings": res.warnings, "refs": {n: s.id for n, s in res.references.items()}}
        except (NarrativeRejected, LLMError) as e:
            ai = {"state": "rejected", "error": scrub(e)}
    return render_report_html(storage, fact, ai=ai, controls=controls)
