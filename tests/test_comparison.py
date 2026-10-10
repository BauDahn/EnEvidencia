from conftest import TEXT_A, TEXT_B
from enevidencia.comparison import AGREE, DIFFER, NO_VALUE, compare, normalize_value
from enevidencia.extraction import build_claim
from enevidencia.ingestion import source_from_text


def add(db, fact, text, quote, value):
    s = db.add_source(source_from_text("https://x.com/" + str(len(text)), text))
    return db.add_claim(build_claim(s, fact.id, "afirm", "Ana", quote, value))


def test_normalize_value():
    assert normalize_value("2.650 €") == normalize_value("2650€")


def test_contradiction_and_agreement(db, fact):
    add(db, fact, TEXT_A, "2.650 €", "2.650 €")
    add(db, fact, TEXT_B, "2.400 €", "2.400 €")
    assert compare(fact.id, db.claims_for_fact(fact.id)).status == DIFFER
    add(db, fact, TEXT_A + " Otra.", "2.650 €", "2650€")
    c = compare(fact.id, db.claims_for_fact(fact.id))
    assert len(c.groups) == 2 and max(len(g.records) for g in c.groups) == 2


def test_agree_and_no_value(db, fact):
    add(db, fact, TEXT_A, "2.650 €", "2650 €")
    assert compare(fact.id, db.claims_for_fact(fact.id)).status == AGREE
    assert compare(fact.id, []).status == NO_VALUE
