import pytest

from enevidencia.llm import FakeLLM
from enevidencia.models import Claim, Fact, Source, SourceType
from enevidencia.reporting import DISCLAIMER
from enevidencia.reporting.llm import NarrativeRejected, draft_narrative
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
    assert "[1]" in r.text


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


def test_numeracion_unica_prompt_y_lista(setup):
    """El prompt y la lista de fuentes guardada usan exactamente la misma numeración [n]."""
    db, fact = setup
    llm = FakeLLM(text_responses=[GOOD])
    r = draft_narrative(db, fact, llm)
    for e in r.entries:
        assert f"[{e['n']}]" in llm.prompts[0] and e["url"] in llm.prompts[0]
        assert r.references[e["n"]].url == e["url"]
    assert [e["url"] for e in r.entries] == ["http://x/0", "http://x/1"]
