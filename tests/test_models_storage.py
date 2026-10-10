import pytest

from enevidencia.models import Claim, Fact, Source, SourceType, normalize_text
from enevidencia.storage import Storage


def test_claim_requires_quote_and_speaker():
    with pytest.raises(ValueError):
        Claim("x", "Ana", "  ", 1, 1)
    with pytest.raises(ValueError):
        Claim("x", "", "cita", 1, 1)


def test_normalize_text_quotes_and_spaces():
    assert normalize_text("«Hola   mundo»") == '"hola mundo"'


def test_storage_roundtrip():
    db = Storage(":memory:")
    f = db.add_fact(Fact("Hecho"))
    s = db.add_source(Source("http://a.com", "texto", SourceType.PRIMARY, outlet="A"))
    db.add_claim(Claim("afirm", "Ana", "texto", s.id, f.id, "5"))
    rec = db.claims_for_fact(f.id)[0]
    assert rec.source.url == "http://a.com" and rec.claim.speaker == "Ana"
    db.set_source_type(s.id, SourceType.SECONDARY)
    assert db.get_source(s.id).source_type is SourceType.SECONDARY
