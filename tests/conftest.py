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


from fastapi.testclient import TestClient as _TC  # noqa: E402


def csrf_of(client) -> str:
    """Token CSRF del cliente (la cookie se crea en la primera respuesta)."""
    if "ee_csrf" not in client.cookies:
        client.get("/normas")
    return client.cookies["ee_csrf"]


class AdminClient(_TC):
    """Cliente ya autenticado como administrador y que añade el token CSRF a cada POST."""

    def __init__(self, app, *a, **kw):
        super().__init__(app, *a, **kw)
        acc = app.state.accounts
        user = acc.get_user(1) or acc.register("admin", "admin@example.org", "contrasena-larga-1")
        self.cookies.set("ee_session", acc.create_session(user))
        csrf_of(self)

    def post(self, url, data=None, **kw):
        if "files" not in kw and "json" not in kw:
            data = {**(data or {}), "csrf_token": self.cookies["ee_csrf"]}
        return super().post(url, data=data, **kw)


@pytest.fixture(autouse=True)
def _sin_limites_de_registro(monkeypatch):
    """El retraso mínimo y el límite de registros por IP se prueban expresamente en tests/test_hardening.py."""
    import enevidencia.accounts as accounts

    monkeypatch.setenv("ENEVIDENCIA_MIN_FORM_SECONDS", "0")
    monkeypatch.setattr(accounts, "REG_PER_HOUR", 10_000)
    monkeypatch.setattr(accounts, "REG_PER_DAY", 10_000)
