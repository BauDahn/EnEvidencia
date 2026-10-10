from fastapi.testclient import TestClient

from conftest import TEXT_A
from enevidencia.storage import Storage
from enevidencia.web.app import create_app


def test_full_flow():
    db = Storage(":memory:")
    c = TestClient(create_app(db))
    assert c.get("/").status_code == 200
    r = c.post("/facts", data={"title": "Renta"}, follow_redirects=False)
    assert r.status_code == 303
    c.post("/sources", data={"url": "https://a.com", "text": TEXT_A, "kind": "noticia"})
    bad = c.post("/claims", data={"source_id": 1, "fact_id": 1, "statement": "x", "speaker": "Ana", "quote": "inventada"})
    assert "no+aparece" in str(bad.url) or "no%20aparece" in str(bad.url) or "error" in str(bad.url)
    c.post("/claims", data={"source_id": 1, "fact_id": 1, "statement": "Renta 2.650 €", "speaker": "Casero",
                            "quote": "2.650 €", "value": "2650"})
    page = c.get("/facts/1").text
    assert "Renta 2.650" in page and "https://a.com" in page
    assert c.get("/facts/1/report.md").status_code == 200
