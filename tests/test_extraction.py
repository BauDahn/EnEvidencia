import pytest

from conftest import TEXT_A
from enevidencia.extraction import QuoteNotInSource, build_claim, suggest_candidates
from enevidencia.ingestion import source_from_text


def saved_source(db, text=TEXT_A):
    return db.add_source(source_from_text("https://a.com", text))


def test_claim_with_valid_quote(db, fact):
    s = saved_source(db)
    c = build_claim(s, fact.id, "Renta de 2.650 €", "El casero", "la renta exigida era de  2.650 €", "2650 €")
    assert c.source_id == s.id


def test_quote_not_in_source_rejected(db, fact):
    s = saved_source(db)
    with pytest.raises(QuoteNotInSource):
        build_claim(s, fact.id, "x", "Ana", "frase inventada", "1")


def test_suggestions_find_figures():
    assert any("2.650" in c for c in suggest_candidates(TEXT_A))
