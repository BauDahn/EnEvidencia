import pytest

from enevidencia.models import Fact
from enevidencia.storage import Storage

TEXT_A = 'El casero afirmó: "La renta exigida era de 2.650 €". Firmó el 3 de mayo.'
TEXT_B = "Según el comunicado, la renta pactada fue de 2.400 € mensuales."


@pytest.fixture
def db():
    return Storage(":memory:")


@pytest.fixture
def fact(db):
    return db.add_fact(Fact("Renta del contrato"))


@pytest.fixture(autouse=True)
def _dns_publico(monkeypatch):
    """Los tests no usan red: todo host se resuelve a una IP pública salvo que el test lo cambie."""
    import enevidencia.ingestion as ing

    monkeypatch.setattr(ing, "_resolve", lambda host: ["93.184.216.34"])
