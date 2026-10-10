from enevidencia.classification_llm import AI_PREFIX, classify_with_fallback
from enevidencia.llm import FakeLLM, LLMError
from enevidencia.models import Source, SourceType


def src(url="https://blog.example.com/a", kind="", text="Texto de prueba"):
    return Source(url=url, text=text, source_type=SourceType.SECONDARY, kind=kind, outlet="Blog", title="Título")


def test_rule_wins_without_llm():
    llm = FakeLLM()
    t, reason = classify_with_fallback(src("https://www.boe.es/x"), llm)
    assert t == SourceType.INSTITUTIONAL and not reason.startswith(AI_PREFIX)
    assert llm.prompts == []


def test_llm_fallback():
    llm = FakeLLM(json_responses=[{"type": "primary", "reason": "es una entrevista"}])
    t, reason = classify_with_fallback(src(text="x" * 5000), llm)
    assert t == SourceType.PRIMARY and reason == AI_PREFIX + "es una entrevista"
    p = llm.prompts[0]
    assert "https://blog.example.com/a" in p and "Blog" in p and "Título" in p
    assert "x" * 1500 in p and "x" * 1501 not in p and "NO si es fiable" in p


def test_invalid_type_falls_back_to_default():
    t, reason = classify_with_fallback(src(), FakeLLM(json_responses=[{"type": "fiable", "reason": "r"}]))
    assert t == SourceType.SECONDARY and "valor por defecto" in reason


def test_llm_error_falls_back_to_default():
    t, reason = classify_with_fallback(src(), FakeLLM())  # sin respuestas -> LLMError
    assert t == SourceType.SECONDARY and not reason.startswith(AI_PREFIX)


def test_malformed_response():
    t, _ = classify_with_fallback(src(), FakeLLM(json_responses=[["no", "dict"]]))
    assert t == SourceType.SECONDARY
