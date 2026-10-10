from enevidencia.discovery import (discover, generate_queries, normalize_url, search_urls)
from enevidencia.llm import FakeLLM
from enevidencia.models import Source, SourceType, Status

LONG = "texto " * 100


def mk(url, text=LONG):
    return Source(url=url, text=text, source_type=SourceType.SECONDARY)


def test_queries_tema_primero_validadas_y_neutralidad():
    llm = FakeLLM(json_responses=[["Caso X", "  sentencia   caso X ", "sentencia caso x", 5, "", "comunicado caso X",
                                   "entrevista caso X", "noticias caso X", "extra"]])
    qs = generate_queries("Caso X", llm, n=4)
    assert qs == ["Caso X", "sentencia caso X", "comunicado caso X", "entrevista caso X"]
    p = llm.prompts[0].lower()
    assert "neutrales" in p and "prohibido" in p and "adjetivos valorativos" in p
    assert "sentencias" in p and "comunicados oficiales" in p and "entrevistas" in p


def test_queries_llm_falla_o_basura():
    assert generate_queries("Tema", FakeLLM(json_responses=[{"a": "b"}])) == ["Tema"]
    assert generate_queries("Tema", FakeLLM()) == ["Tema"]


def test_search_urls_error_devuelve_vacio():
    def boom(q, n):
        raise RuntimeError("red caída")
    assert search_urls("x", searcher=boom) == []


def test_search_urls_formato_ddgs():
    out = search_urls("x", searcher=lambda q, n: [{"href": "https://a.es/1", "title": "T", "body": "B"},
                                                  {"href": "ftp://no"}, {"title": "sin url"}])
    assert out == [{"url": "https://a.es/1", "title": "T", "snippet": "B"}]


def test_normalize_url():
    assert normalize_url("https://A.es/x/?utm_source=a&id=3#frag") == "https://a.es/x?id=3"


def test_discover_dedupe_limite_dominio_y_errores():
    llm = FakeLLM(json_responses=[["q2"]])
    results = {
        "T": [{"href": "https://a.es/1?utm_x=1"}, {"href": "https://a.es/1#f"}, {"href": "https://a.es/2"},
              {"href": "https://a.es/3"}, {"href": "https://b.es/1"}],
        "q2": [{"href": "https://c.es/roto"}, {"href": "https://d.es/corto"}, {"href": "https://b.es/1/"}],
    }

    def fetcher(url):
        if "roto" in url:
            raise OSError("404")
        return mk(url, "corto" if "corto" in url else LONG)

    msgs = []
    r = discover("T", llm, searcher=lambda q, n: results[q], fetcher=fetcher, on_progress=msgs.append)
    urls = [s.url for s in r.sources]
    assert urls == ["https://a.es/1?utm_x=1", "https://a.es/2", "https://b.es/1"]
    assert all(s.status == Status.PROPOSED for s in r.sources)
    assert len(r.errors) == 2
    assert any("roto" in e for e in r.errors) and any("corto" in e for e in r.errors)
    assert r.queries == ["T", "q2"] and msgs


def test_discover_max_sources_y_ddgs_roto():
    llm = FakeLLM(json_responses=[[]])
    res = discover("T", llm, searcher=lambda q, n: [{"href": f"https://s{i}.es/"} for i in range(5)],
                   fetcher=mk, max_sources=3)
    assert len(res.sources) == 3

    def boom(q, n):
        raise RuntimeError("x")
    vacio = discover("T", FakeLLM(json_responses=[[]]), searcher=boom, fetcher=mk)
    assert vacio.sources == [] and vacio.errors == []
