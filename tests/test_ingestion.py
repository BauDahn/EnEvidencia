import httpx
import pytest

from enevidencia.ingestion import fetch_source, source_from_text
from enevidencia.models import SourceType


def test_source_from_text_classifies():
    s = source_from_text("https://x.com/a", "texto", kind="entrevista")
    assert s.source_type is SourceType.PRIMARY and s.classification_reason


def test_rejects_empty():
    with pytest.raises(ValueError):
        source_from_text("https://x.com", "  ")
    with pytest.raises(ValueError):
        source_from_text("", "texto")


def test_fetch_source_with_mock_transport():
    html = "<html><head><title>T</title></head><body><article><p>" + "Texto de prueba largo. " * 30 + "</p></article></body></html>"
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=html)))
    s = fetch_source("https://diario.com/n", kind="noticia", client=client)
    assert "Texto de prueba" in s.text and s.url == "https://diario.com/n"
