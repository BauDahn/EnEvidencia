from conftest import TEXT_A, TEXT_B
from enevidencia.extraction import build_claim
from enevidencia.ingestion import source_from_text
from enevidencia.reporting import render_html, render_markdown


def test_report_cites_every_source(db, fact):
    for url, text, quote, val in [("https://a.com", TEXT_A, "2.650 €", "2650"), ("https://b.com", TEXT_B, "2.400 €", "2400")]:
        s = db.add_source(source_from_text(url, text, outlet="Medio"))
        db.add_claim(build_claim(s, fact.id, "afirm", "Ana", quote, val))
    md = render_markdown(db, fact)
    assert "https://a.com" in md and "https://b.com" in md and "[1]" in md and "[2]" in md
    assert "No determina cuál es la verdad" in md
    page = render_html(db, fact)
    assert "<pre" not in page and "<style>" in page and "https://a.com" in page


def _two(db, fact, outlet="Medio"):
    for url, text, quote, val in [("https://a.com", TEXT_A, "2.650 €", "2650"), ("https://b.com", TEXT_B, "2.400 €", "2400")]:
        s = db.add_source(source_from_text(url, text, outlet=outlet))
        db.add_claim(build_claim(s, fact.id, "afirm", "Ana", quote, val))


def test_html_autocontenido_y_estructura(db, fact):
    import re
    _two(db, fact)
    page = render_html(db, fact)
    assert page.startswith("<!doctype html>") and "<style>" in page and "@media print" in page
    assert "prefers-color-scheme:dark" in page and "@page" in page
    # autocontenido: sin recursos externos ni /static
    assert "<link" not in page and "<script" not in page and "/static" not in page
    assert not re.search(r"(src|href)=[\"']https?://(?!a\.com|b\.com)", page)
    assert "url(" not in page and "@import" not in page
    for needle in ("EnEvidencia", "Metodología", "Nota editorial", "Estado entre fuentes", "licencia MIT",
                   'href="#fn-1"', 'id="fn-1"', 'id="fn-2"', "[1]", "[2]", "https://a.com", "https://b.com", "«2.650 €»"):
        assert needle in page, needle
    assert "no-print" not in page.split("<body>")[1]          # versión descargada: sin controles
    # sin colores de veredicto ni iconos
    assert not re.search(r"#(?:f00|0f0|[ef][0-9a-f]{0,1}0000|00[89a-f]000)\b|\\bred\\b|\\bgreen\\b|✓|✗|⚠", page.split("</style>")[0] + page.split("</style>")[1])


def test_html_controles_solo_en_pantalla(db, fact):
    _two(db, fact)
    page = render_html(db, fact, controls={"back": "/facts/1", "md": "/facts/1/report.md"})
    assert 'class="controls no-print"' in page and "Descargar .md" in page and "Imprimir / guardar PDF" in page
    assert ".no-print{display:none" in page


def test_html_escapa_contenido_externo(db, fact):
    s = db.add_source(source_from_text("javascript:alert(1)", "dijo <b>hola</b> mundo", outlet="<script>x</script>"))
    db.add_claim(build_claim(s, fact.id, "<img src=x onerror=1>", "<i>Ana</i>", "<b>hola</b>", "<v>"))
    page = render_html(db, fact)
    for raw in ("<script>x", "<img src=x", "<i>Ana", "<b>hola</b>", "<v>"):
        assert raw not in page, raw
    assert "&lt;b&gt;hola&lt;/b&gt;" in page and 'href="javascript:' not in page


def test_html_ia_rotulado_y_refs_enlazadas(db, fact):
    from enevidencia.reporting.llm import render_full_html
    from enevidencia.llm import FakeLLM
    _two(db, fact)
    llm = FakeLLM(text_responses=["Según Ana la renta fue 2.650 € [1]. Otra fuente indica 2.400 € [2].\n\nFrase larga sin referencia alguna que debe avisar al lector."])
    page = render_full_html(db, fact, llm)
    assert "Resumen redactado (asistido por IA, revisar)" in page and "Asistido por IA" in page
    assert 'href="#fn-1">[1]</a>' in page and "Avisos de la validación" in page and "Frase sin referencia" in page
    bad = render_full_html(db, fact, FakeLLM(text_responses=["Inventado [9].", "Inventado [9]."]))
    assert "Narrativa de IA no incluida" in bad and "Resumen redactado" not in bad
    assert "Resumen redactado" not in render_full_html(db, fact)
