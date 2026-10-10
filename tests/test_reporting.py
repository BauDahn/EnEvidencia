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
    assert "<pre" in render_html(db, fact)
