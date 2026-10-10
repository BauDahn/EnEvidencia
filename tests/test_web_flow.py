"""Flujo completo investigar -> revisar -> aceptar -> comparar -> informe, sin red (FakeLLM + searcher + fetcher)."""
import io

import httpx
import pytest
from fastapi.testclient import TestClient

from enevidencia import ingestion
from enevidencia.ingestion import fetch_source, validate_url
from enevidencia.llm import FakeLLM, LLMError
from enevidencia.models import Source, SourceType
from enevidencia.storage import Storage
from enevidencia.web.app import create_app

TXT_A = ("El casero declaró ante el periódico que la renta exigida era de 2.650 € al mes. " * 1 +
         "Relleno de texto para superar la longitud mínima. " * 8)
TXT_B = ("El comunicado oficial indica que la renta pactada fue de 2.400 € mensuales. " +
         "Más relleno de texto para superar la longitud mínima. " * 8)
TEXTS = {"https://diario.example/a": TXT_A, "https://inst.example/b": TXT_B}


def handler(prompt: str):
    if "consultas de búsqueda" in prompt:
        return ["sentencia renta"]
    if "Clasifica el TIPO" in prompt:
        return {"type": "primary", "reason": "es una declaración"}
    if "localiza afirmaciones" in prompt:
        if "casero declaró" in prompt:
            return [{"statement": "La renta era 2.650 €", "speaker": "Casero", "quote": "la renta exigida era de 2.650 €",
                     "value": "2.650 €"},
                    {"statement": "Inventada", "speaker": "X", "quote": "esta cita no existe", "value": ""}]
        return [{"statement": "La renta era 2.400 €", "speaker": "Comunicado", "quote": "la renta pactada fue de 2.400 €",
                 "value": "2400 €"}]
    if "Agrupa estas" in prompt:
        import re
        ids = [int(i) for i in re.findall(r'"claim_id": (\d+)', prompt)]
        return {"groups": [{"topic": "Importe de la renta", "claim_ids": ids}], "normalized": {str(i): "x" for i in ids}}
    return "Según el casero, la renta era de 2.650 € [1]. Según el comunicado, de 2.400 € [2]."


def searcher(q, n):
    return [{"href": u, "title": "t", "body": "b"} for u in TEXTS]


def fetcher(url):
    return Source(url=url, text=TEXTS[url], source_type=SourceType.SECONDARY, title=f"T {url}")


def make(llm=None):
    db = Storage(":memory:")
    app = create_app(db, llm=llm or FakeLLM(handler=handler), searcher=searcher, fetcher=fetcher)
    return db, app, TestClient(app)


def wait(app, resp):
    job_id = str(resp.headers["location"]).rsplit("/", 1)[1]
    app.state.wait(job_id)
    return app.state.jobs[job_id]


TOPIC = "Renta del contrato"


def run(app, c, topic=TOPIC):
    return wait(app, c.post("/investigate", data={"topic": topic}, follow_redirects=False))


def test_flujo_automatico_completo():
    db, app, c = make()
    r = c.post("/investigate", data={"topic": TOPIC}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/jobs/")
    job = wait(app, r)
    assert job.status == "done" and not job.error, job.error
    prog = c.get(r.headers["location"]).text
    assert "Tarea" in prog and "/facts/1" in prog and "Auditoría" in prog

    # todo se aceptó solo, sin intervención del usuario
    assert [s.status.value for s in db.list_sources()] == ["accepted", "accepted"]
    assert len(db.claims_for_fact(1)) == 2                 # la cita inventada se descartó por verificación
    page = c.get("/facts/1").text
    assert "se contradicen" in page and "Valor literal: <code>2.650 €</code>" in page and "2400 €" in page
    assert "auditoría" in page.lower() and "/facts/1/review" in page      # revisión manual enlazada, posterior
    assert "Inventada" not in page
    grouped = c.get("/facts/1?grouped=1").text
    assert "Importe de la renta" in grouped
    ai = c.get("/facts/1/report-ia")
    assert "Resumen redactado (asistido por IA" in ai.text and "[1]" in ai.text and "Metodología" in ai.text

    # corrección posterior: descartar una afirmación la saca del informe
    c.post("/claims/1/status", data={"status": "rejected", "next": "/facts/1/review"})
    assert len(db.claims_for_fact(1)) == 1
    review = c.get("/facts/1/review").text
    assert "descartó automáticamente <strong>1</strong>" in review


def test_auditoria_y_metodologia():
    db, app, c = make()
    run(app, c)
    a = c.get("/facts/1/audit")
    assert a.status_code == 200
    t = a.text
    assert "Consultas usadas" in t and "sentencia renta" in t and "Renta del contrato" in t
    assert "diario.example/a" in t and "inst.example/b" in t and "aceptada" in t
    assert "esta cita no existe" in t and "no aparece literalmente" in t       # afirmación descartada y motivo
    assert "Secundaria" in t or "Primaria" in t
    rep = c.get("/facts/1/report.md").text
    assert "Metodología" in rep and "sentencia renta" in rep and "consideradas: 2" in rep
    assert "aceptadas: 2" in rep and "excluidas: 0" in rep and "por tipo" in rep and "no decide la verdad" in rep
    rep = c.get("/facts/1/report.html").text
    assert "Metodología" in rep and "sentencia renta" in rep and "no decide la verdad" in rep
    assert "Fuentes consideradas</dt><dd>2" in rep and "Fuentes aceptadas</dt><dd>2" in rep
    assert "Fuentes excluidas</dt><dd>0" in rep and "Aceptadas por tipo" in rep
    assert c.get("/facts/99/audit").status_code == 404


def test_filtro_terminos_del_tema_excluye_irrelevante():
    texts = dict(TEXTS)
    texts["https://otro.example/c"] = "Receta de paella valenciana con ingredientes y pasos detallados. " * 10
    llm = FakeLLM(handler=handler)
    db = Storage(":memory:")
    app = create_app(db, llm=llm, searcher=lambda q, n: [{"href": u} for u in texts],
                     fetcher=lambda u: Source(url=u, text=texts[u], source_type=SourceType.SECONDARY))
    c = TestClient(app)
    job = run(app, c)
    assert job.status == "done"
    by_url = {s.url: s for s in db.list_sources()}
    assert by_url["https://otro.example/c"].status.value == "rejected"
    assert by_url["https://diario.example/a"].status.value == "accepted"
    audit = c.get("/facts/1/audit").text
    assert "No menciona los términos del tema" in audit and "renta" in audit and "excluida" in audit
    assert len(db.claims_for_fact(1)) == 2                  # de la irrelevante no se extrajo nada
    assert not any("receta" in p.lower() for p in llm.prompts if "localiza afirmaciones" in p)
    assert "excluidas: 1" in c.get("/facts/1/report.md").text


def test_filtro_duplicados_url_y_texto():
    texts = {"https://diario.example/a": TXT_A,
             "https://diario.example/a?utm_source=x#f": TXT_A,                 # misma URL normalizada
             "https://copia.example/z": TXT_A + " Añadido final.",             # texto casi idéntico
             "https://inst.example/b": TXT_B}
    db = Storage(":memory:")
    app = create_app(db, llm=FakeLLM(handler=handler), searcher=lambda q, n: [{"href": u} for u in texts],
                     fetcher=lambda u: Source(url=u, text=texts[u], source_type=SourceType.SECONDARY))
    c = TestClient(app)
    run(app, c)
    acc = [s.url for s in db.list_sources() if s.status.value == "accepted"]
    assert acc == ["https://diario.example/a", "https://inst.example/b"]
    audit = c.get("/facts/1/audit").text
    assert "URL duplicada" in audit and "Texto casi idéntico" in audit


def test_limite_por_dominio_y_texto_corto():
    texts = {f"https://medio.example/{i}": f"Renta distinta número {i}: " + ("contenido propio %d " % i) * 60 for i in range(4)}
    texts["https://corto.example/x"] = "renta"
    db = Storage(":memory:")
    app = create_app(db, llm=FakeLLM(handler=lambda p: [] if "localiza" in p else handler(p)),
                     searcher=lambda q, n: [{"href": u} for u in texts],
                     fetcher=lambda u: Source(url=u, text=texts[u], source_type=SourceType.SECONDARY))
    c = TestClient(app)
    run(app, c)
    audit = c.get("/facts/1/audit").text
    assert sum(1 for s in db.list_sources() if s.status.value == "accepted") == 2
    assert "máximo por dominio" in audit and "demasiado corto" in audit


def test_cuota_agotada_a_mitad_da_resultado_parcial():
    def h(prompt):
        if "localiza afirmaciones" in prompt and "comunicado oficial" in prompt:
            raise LLMError("429 RESOURCE_EXHAUSTED cuota agotada")
        return handler(prompt)
    db, app, c = make(FakeLLM(handler=h))
    job = run(app, c)
    assert job.status == "done"
    assert "Resultado parcial" in job.error and "cuota" in job.error
    assert len(db.claims_for_fact(1)) == 1                  # lo ya guardado se conserva
    page = c.get(f"/jobs/{job.id}").text
    assert "Resultado parcial" in page and "/facts/1" in page
    assert "Pendiente de extraer" in c.get("/facts/1/review").text      # se puede reanudar
    assert c.get("/facts/1").status_code == 200


def test_rechazar_fuente_rechaza_sus_afirmaciones():
    db, app, c = make()
    run(app, c)
    assert len(db.claims_for_fact(1)) == 2
    c.post("/sources/1/status", data={"status": "rejected"})
    assert len(db.claims_for_fact(1)) == 1


def test_no_acepta_afirmacion_de_fuente_no_aceptada():
    db, app, c = make()
    run(app, c)
    c.post("/claims/1/status", data={"status": "rejected"})
    c.post("/sources/1/status", data={"status": "proposed"})
    c.post("/claims/1/status", data={"status": "accepted", "next": "/"})
    assert all(r.source.id != 1 for r in db.claims_for_fact(1))


def test_sin_clave_mensaje_claro_y_modo_manual(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr("enevidencia.llm.load_env", lambda *a, **k: None)
    db = Storage(":memory:")
    c = TestClient(create_app(db))
    r = c.post("/investigate", data={"topic": "X"})
    assert "GEMINI_API_KEY" in r.text and "aistudio.google.com/apikey" in r.text
    assert db.list_facts() == []
    c.post("/facts", data={"title": "Manual"})
    assert "Manual" in c.get("/").text


def test_error_de_cuota_visible_en_la_tarea():
    def boom(prompt):
        raise LLMError("429 cuota agotada key=SECRETO123")
    db, app, c = make(FakeLLM(handler=boom))
    import os
    os.environ["GEMINI_API_KEY"] = "SECRETO123"
    try:
        job = run(app, c)
        page = c.get(f"/jobs/{job.id}").text
    finally:
        del os.environ["GEMINI_API_KEY"]
    assert "SECRETO123" not in page and "SECRETO123" not in job.error
    assert "cuota" in page and job.status in ("done", "error")


def test_extraccion_manual_posterior_desde_la_revision():
    """Si la extracción automática falló, la revisión permite reanudarla (queda como propuesta)."""
    state = {"fail": True}

    def h(prompt):
        if state["fail"] and "localiza afirmaciones" in prompt:
            raise LLMError("cuota agotada")
        return handler(prompt)
    db, app, c = make(FakeLLM(handler=h))
    run(app, c)
    assert db.claims_for_fact(1) == []
    state["fail"] = False
    job = wait(app, c.post("/facts/1/extract", follow_redirects=False))
    assert job.status == "done", job.error
    assert db.claims_for_fact(1) == [] and len(db.claims_for_fact(1, None)) == 2   # propuestas manuales


def test_informe_ia_indica_narrativa_rechazada():
    def h(prompt):
        if "Redacta en español" in prompt:
            return "Algo inventado [9]."
        return handler(prompt)
    db, app, c = make(FakeLLM(handler=h))
    run(app, c)
    assert "Narrativa de IA no incluida" in c.get("/facts/1/report-ia").text


def test_escapa_contenido_externo_y_enlaces_peligrosos():
    db = Storage(":memory:")
    c = TestClient(create_app(db))
    c.post("/sources", data={"url": "javascript:alert(1)", "text": "<script>alert(1)</script> texto"})
    page = c.get("/sources/1").text
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page
    assert 'href="javascript:' not in page


def test_url_manual_no_valida_no_500():
    c = TestClient(create_app(Storage(":memory:")))
    r = c.post("/sources", data={"url": "file:///etc/passwd"})
    assert r.status_code == 200 and "http" in r.text


def test_open_redirect_bloqueado():
    db = Storage(":memory:")
    c = TestClient(create_app(db))
    c.post("/sources", data={"url": "https://a.com", "text": "texto"})
    r = c.post("/sources/1/status", data={"status": "accepted", "next": "https://evil.com"}, follow_redirects=False)
    assert r.headers["location"] == "/sources/1"


# ---- SSRF
@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://a.com/x", "http://localhost:8000", "http://127.0.0.1/",
                                 "http://192.168.1.5/x", "http://10.0.0.1", "http://[::1]/", "http://169.254.169.254/",
                                 "http://foo.local/", "http://"])
def test_validate_url_bloquea(url):
    with pytest.raises(ValueError):
        validate_url(url)


def test_validate_url_bloquea_dns_privado(monkeypatch):
    monkeypatch.setattr(ingestion, "_resolve", lambda h: ["10.1.2.3"])
    with pytest.raises(ValueError):
        validate_url("https://interno.example/")
    monkeypatch.setattr(ingestion, "_resolve", lambda h: ["93.184.216.34"])
    assert validate_url("https://publico.example/") == "https://publico.example/"


def test_fetch_source_bloquea_redireccion_a_localhost():
    def h(req):
        return httpx.Response(302, headers={"location": "http://127.0.0.1/admin"})
    with pytest.raises(ValueError):
        fetch_source("https://a.example/x", client=httpx.Client(transport=httpx.MockTransport(h)))


def test_fetcher_inyectado_tambien_se_valida():
    llm = FakeLLM(handler=handler)
    db = Storage(":memory:")
    app = create_app(db, llm=llm, fetcher=fetcher, searcher=lambda q, n: [{"href": "http://127.0.0.1/x"}])
    c = TestClient(app)
    job = wait(app, c.post("/investigate", data={"topic": "T"}, follow_redirects=False))
    assert db.list_sources() == [] and any("locales" in line for line in job.log)


# ---- PDF
def make_pdf(text: str) -> bytes:
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offs = io.BytesIO(), []
    out.write(b"%PDF-1.4\n")
    for i, o in enumerate(objs, 1):
        offs.append(out.tell())
        out.write(b"%d 0 obj\n" % i + o + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1))
    for o in offs:
        out.write(b"%010d 00000 n \n" % o)
    out.write(b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref))
    return out.getvalue()


def test_pdf_con_texto():
    pdf = make_pdf("La renta pactada fue de 2400 euros")
    cl = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, content=pdf, headers={"content-type": "application/pdf"})))
    s = fetch_source("https://inst.example/doc", client=cl)
    assert "renta pactada fue de 2400 euros" in s.text
    # también por extensión .pdf aunque el content-type sea genérico
    cl2 = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=pdf)))
    assert "2400" in fetch_source("https://inst.example/doc.pdf", client=cl2).text


def test_pdf_escaneado_falla_con_mensaje_claro():
    from pypdf import PdfWriter
    w = PdfWriter()
    w.add_blank_page(200, 200)
    buf = io.BytesIO()
    w.write(buf)
    cl = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, content=buf.getvalue(), headers={"content-type": "application/pdf"})))
    with pytest.raises(ValueError, match="escaneado"):
        fetch_source("https://inst.example/scan", client=cl)
