import pytest

from enevidencia.extraction.llm import extract_claims, split_text
from enevidencia.llm import FakeLLM, LLMError
from enevidencia.models import Source, SourceType, Status

TEXT = "El juzgado dictó sentencia el 5 de mayo. La renta exigida era de 2.650 euros al mes según el casero."


def src(text=TEXT, id=1):
    return Source(url="http://x", text=text, source_type=SourceType.SECONDARY, id=id)


def item(quote="La renta exigida era de 2.650 euros al mes", statement="La renta era de 2.650 €"):
    return {"statement": statement, "speaker": "El casero", "quote": quote, "value": "2650 €"}


def test_cita_real_aceptada_y_proposed():
    r = extract_claims(src(), 1, FakeLLM(json_responses=[[item()]]))
    assert r.error is None and len(r.claims) == 1
    c = r.claims[0]
    assert c.status == Status.PROPOSED and c.source_id == 1 and c.fact_id == 1 and c.value == "2650 €"


def test_cita_inventada_descartada():
    r = extract_claims(src(), 1, FakeLLM(json_responses=[[item(quote="La renta era de 9.999 euros")]]))
    assert r.claims == [] and len(r.discarded) == 1
    assert "literalmente" in r.discarded[0]["reason"]


def test_campos_vacios_descartados():
    bad = item()
    bad["speaker"] = ""
    r = extract_claims(src(), 1, FakeLLM(json_responses=[[bad, "basura"]]))
    assert r.claims == [] and len(r.discarded) == 2


def test_json_malformado():
    r = extract_claims(src(), 1, FakeLLM(json_responses=["no soy una lista"]))
    assert r.error and r.claims == []


def test_llm_error():
    r = extract_claims(src(), 1, FakeLLM())
    assert r.error and "LLM" in r.error and r.claims == []


def test_fuente_sin_id():
    with pytest.raises(ValueError):
        extract_claims(src(id=None), 1, FakeLLM())


def test_dedupe():
    r = extract_claims(src(), 1, FakeLLM(json_responses=[[item(), item()]]))
    assert len(r.claims) == 1


def test_troceo_texto_largo_y_fusion():
    a = "Alfa dijo que fueron 100 personas. "
    b = "Beta aseguró que fueron 200 personas. "
    text = a + ("relleno " * 1000) + b
    assert len(split_text(text)) >= 2

    def handler(prompt):
        out = []
        if "fueron 100 personas" in prompt:
            out.append({"statement": "100 personas", "speaker": "Alfa", "quote": "fueron 100 personas"})
        if "fueron 200 personas" in prompt:
            out.append({"statement": "200 personas", "speaker": "Beta", "quote": "fueron 200 personas"})
        return out

    llm = FakeLLM(handler=handler)
    r = extract_claims(src(text), 1, llm)
    assert len(llm.prompts) >= 2
    assert {c.speaker for c in r.claims} == {"Alfa", "Beta"}


def test_solape_dedupe_entre_tramos():
    text = ("palabra " * 700) + "dato clave de 42 casos. " + ("palabra " * 700)
    same = {"statement": "42 casos", "speaker": "X", "quote": "dato clave de 42 casos"}
    r = extract_claims(src(text), 1, FakeLLM(handler=lambda p: [same] if "dato clave" in p else []))
    assert len(r.claims) == 1


def test_max_claims():
    items = [item(statement=f"afirmación {i}") for i in range(5)]
    r = extract_claims(src(), 1, FakeLLM(json_responses=[items]), max_claims=2)
    assert len(r.claims) == 2 and len(r.discarded) == 3
