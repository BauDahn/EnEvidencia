import pytest

from enevidencia.llm import FakeLLM
from enevidencia.models import Claim, Fact, Source, SourceType
from enevidencia.reporting import DISCLAIMER
from enevidencia.reporting.llm import NarrativeRejected, draft_narrative, render_full_report
from enevidencia.storage import Storage


@pytest.fixture
def setup():
    db = Storage(":memory:")
    fact = db.add_fact(Fact("Renta del contrato"))
    for i, (who, val) in enumerate([("Casero", "2650 €"), ("Inquilina", "2400 €")]):
        s = db.add_source(Source(f"http://x/{i}", "texto", SourceType.PRIMARY, author=who, title=f"Fuente {i}"))
        db.add_claim(Claim(f"La renta era {val}", who, "cita", s.id, fact.id, val))
    return db, fact


GOOD = "Según el casero, la renta era de 2650 € [1]. Según la inquilina, era de 2400 € [2]."


def test_valid(setup):
    db, fact = setup
    r = draft_narrative(db, fact, FakeLLM(text_responses=[GOOD]))
    assert r.warnings == [] and set(r.references) == {1, 2}
    assert "[1]" in r.text and "## Fuentes" in r.text


def test_nonexistent_ref_rejected(setup):
    db, fact = setup
    with pytest.raises(NarrativeRejected):
        draft_narrative(db, fact, FakeLLM(text_responses=["Dice algo [9].", "Otra vez [7]."]))


def test_nonexistent_ref_retry_ok(setup):
    db, fact = setup
    llm = FakeLLM(text_responses=["Dice algo [9].", GOOD])
    assert draft_narrative(db, fact, llm).warnings == []
    assert len(llm.prompts) == 2


def test_unreferenced_sentence_warns(setup):
    db, fact = setup
    r = draft_narrative(db, fact, FakeLLM(text_responses=[GOOD + " Esta frase larga no tiene ninguna referencia asociada."]))
    assert any("sin referencia" in w for w in r.warnings)


def test_no_claims_no_llm():
    db = Storage(":memory:")
    fact = db.add_fact(Fact("Vacío"))
    llm = FakeLLM()
    r = draft_narrative(db, fact, llm)
    assert llm.prompts == [] and "No hay afirmaciones" in r.text


def test_sources_section_by_code(setup):
    db, fact = setup
    r = draft_narrative(db, fact, FakeLLM(text_responses=["Texto [1]. ## Fuentes\n[1] inventada"]))
    assert "http://x/0" in r.text and "http://x/1" in r.text and DISCLAIMER in r.text


def test_full_report(setup):
    db, fact = setup
    out = render_full_report(db, fact, FakeLLM(text_responses=[GOOD]))
    assert "Resumen redactado (asistido por IA, revisar)" in out
    assert "Resumen redactado" not in render_full_report(db, fact)
    assert "Resumen redactado" not in render_full_report(db, fact, FakeLLM())
