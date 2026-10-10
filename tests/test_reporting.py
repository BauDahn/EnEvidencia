from conftest import TEXT_A, TEXT_B
from enevidencia.extraction import build_claim
from enevidencia.ingestion import source_from_text
from enevidencia.reporting import numbering
from enevidencia.reporting.render import narrative_blocks


def test_numbering_orden_de_aparicion(db, fact):
    recs = []
    for url, text, quote, val in [("https://a.com", TEXT_A, "2.650 €", "2650"), ("https://b.com", TEXT_B, "2.400 €", "2400")]:
        s = db.add_source(source_from_text(url, text, outlet="Medio"))
        db.add_claim(build_claim(s, fact.id, "afirm", "Ana", quote, val))
    nums, first = numbering(db.claims_for_fact(fact.id))
    assert sorted(nums.values()) == [1, 2] and first[1].source.url == "https://a.com"


def test_narrative_blocks_escapa_y_enlaza():
    html = "".join(str(b["html"]) for b in narrative_blocks("Dijo <b>x</b> [1] y [9].", {1}))
    assert "&lt;b&gt;x&lt;/b&gt;" in html and 'href="#fn-1"' in html and "[9]" in html and "#fn-9" not in html
