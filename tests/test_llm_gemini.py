"""Pruebas de GeminiClient con SDK falso (sin red ni clave real)."""
import pytest

from enevidencia.llm import LLMError
from enevidencia.llm.gemini import GeminiClient


class Resp:
    def __init__(self, text):
        self.text = text


class FakeSDK:
    def __init__(self, respuestas):
        self.respuestas = list(respuestas)
        self.llamadas = 0
        self.models = self

    def generate_content(self, model, contents, config):
        self.llamadas += 1
        r = self.respuestas.pop(0)
        if isinstance(r, Exception):
            raise r
        return Resp(r)


class Err429(Exception):
    code = 429


def _cliente(tmp_path, sdk, **kw):
    pausas = []
    t = {"now": 0.0}
    kw.setdefault("min_interval", 0.0)
    c = GeminiClient("clave-falsa", cache_dir=tmp_path, client=sdk,
                     sleep=lambda s: (pausas.append(s), t.__setitem__("now", t["now"] + s)),
                     clock=lambda: t["now"], **kw)
    return c, pausas


def test_json_valido(tmp_path):
    c, _ = _cliente(tmp_path, FakeSDK(['{"a": 1}']))
    assert c.generate_json("hola", schema={"type": "object"}) == {"a": 1}


def test_json_invalido(tmp_path):
    c, _ = _cliente(tmp_path, FakeSDK(["no es json"]))
    with pytest.raises(LLMError):
        c.generate_json("hola")


def test_reintento_tras_429(tmp_path):
    sdk = FakeSDK([Err429("RESOURCE_EXHAUSTED"), Err429("429"), "texto"])
    c, pausas = _cliente(tmp_path, sdk)
    assert c.generate_text("p") == "texto"
    assert sdk.llamadas == 3
    assert pausas == [2.0, 4.0]


def test_agota_reintentos(tmp_path):
    sdk = FakeSDK([Err429("429")] * 3)
    c, _ = _cliente(tmp_path, sdk, max_retries=2)
    with pytest.raises(LLMError, match="intentos"):
        c.generate_text("p")
    assert sdk.llamadas == 3


def test_error_no_reintentable(tmp_path):
    sdk = FakeSDK([ValueError("clave inválida")])
    c, _ = _cliente(tmp_path, sdk)
    with pytest.raises(LLMError):
        c.generate_text("p")
    assert sdk.llamadas == 1


def test_cache(tmp_path):
    sdk = FakeSDK(['{"x": [1, 2]}'])
    c, _ = _cliente(tmp_path, sdk)
    assert c.generate_json("q") == {"x": [1, 2]}
    assert c.generate_json("q") == {"x": [1, 2]}
    assert sdk.llamadas == 1
    # una instancia nueva también aprovecha la caché en disco
    c2, _ = _cliente(tmp_path, FakeSDK([]))
    assert c2.generate_json("q") == {"x": [1, 2]}


def test_pausa_entre_llamadas(tmp_path):
    sdk = FakeSDK(["a", "b"])
    c, pausas = _cliente(tmp_path, sdk, min_interval=4.0)
    c.generate_text("1")
    assert pausas == []
    c.generate_text("2")
    assert pausas == [4.0]


def test_repr_no_expone_clave(tmp_path):
    c, _ = _cliente(tmp_path, FakeSDK([]))
    assert "clave-falsa" not in repr(c)
