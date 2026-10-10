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
