"""Temas propuestos: sugerir, votar, moderar e investigar (sin red)."""
from conftest import AdminClient, csrf_of
from fastapi.testclient import TestClient

from enevidencia.llm import FakeLLM
from enevidencia.models import Source, SourceType
from enevidencia.storage import Storage
from enevidencia.web.app import create_app

TITLE = "¿Cuál fue la renta fijada en el contrato?"


def new_app(**kw):
    db = Storage(":memory:")
    return db, create_app(db, **kw)


def user_client(app, name="beto"):
    c = TestClient(app)
    tok = csrf_of(c)
    r = c.post("/registro", data={"username": name, "email": f"{name}@example.org", "password": "contrasena-larga-1",
                                  "acepto": "1", "csrf_token": tok}, follow_redirects=False)
    assert r.status_code == 303
    return c


def post(c, url, **data):
    return c.post(url, data={**data, "csrf_token": csrf_of(c)}, follow_redirects=False)


def suggest(c, title=TITLE, desc=""):
    return post(c, "/temas", titulo=title, descripcion=desc)


def test_sugerir_y_ver():
    _, app = new_app()
    AdminClient(app)
    c = user_client(app)
    r = suggest(c, desc="Contexto")
    assert r.status_code == 303 and r.headers["location"] == "/temas/1"
    page = c.get("/temas").text
    assert TITLE in page and "@beto" in page and "Apoyar" in page
    assert c.get("/temas/1").status_code == 200 and c.get("/temas/99").status_code == 404


def test_validacion_de_longitudes_y_duplicado():
    _, app = new_app()
    AdminClient(app)
    c = user_client(app)
    assert suggest(c, "corto").status_code == 400
    assert suggest(c, "x" * 141).status_code == 400
    assert suggest(c, TITLE, "d" * 1001).status_code == 400
    assert suggest(c).status_code == 303
    r = suggest(c, "  ¿CUAL fue la renta, fijada en el contrato? ")
    assert r.status_code == 400 and "Ya existe" in r.text and 'href="/temas/1"' in r.text


def test_voto_unico_y_retirada():
    _, app = new_app()
    AdminClient(app)
    c = user_client(app)
    suggest(c)
    assert post(c, "/temas/1/voto").status_code == 303
    assert app.state.topics.get(1)["votes"] == 1 and "Retirar mi apoyo" in c.get("/temas/1").text
    post(c, "/temas/1/voto")
    assert app.state.topics.get(1)["votes"] == 0
    d = user_client(app, "carla")
    post(c, "/temas/1/voto"), post(d, "/temas/1/voto")
    assert app.state.topics.get(1)["votes"] == 2


def test_anonimo_redirige_a_entrar():
    _, app = new_app()
    AdminClient(app)
    anon = TestClient(app)
    r = post(anon, "/temas", titulo=TITLE)
    assert r.status_code == 303 and r.headers["location"].startswith("/entrar")
    assert post(anon, "/temas/1/voto").headers["location"].startswith("/entrar")
    assert "Entrar o crear cuenta para sugerir y votar" in anon.get("/temas").text
    assert app.state.topics.list() == []


def test_baneado_rechazado():
    _, app = new_app()
    AdminClient(app)
    c = user_client(app)
    suggest(c)
    app.state.accounts.ban(2)
    assert suggest(c, "Otro tema distinto de prueba").headers["location"].startswith("/entrar")
    assert post(c, "/temas/1/voto").headers["location"].startswith("/entrar")
    assert len(app.state.topics.list()) == 1


def test_limite_diario():
    _, app = new_app()
    AdminClient(app)
    c = user_client(app)
    for i in range(5):
        assert suggest(c, f"Tema numero {i} sobre algo concreto").status_code == 303
    r = suggest(c, "Tema sexto sobre algo concreto")
    assert r.status_code == 400 and "límite" in r.text
    assert len(app.state.topics.list()) == 5


def test_limite_de_votos_por_minuto():
    _, app = new_app()
    AdminClient(app)
    c = user_client(app)
    suggest(c)
    codes = [post(c, "/temas/1/voto").status_code for _ in range(32)]
    assert codes.count(429) == 2


def test_xss_escapado():
    _, app = new_app()
    admin = AdminClient(app)
    c = user_client(app)
    evil = "<script>alert(1)</script> titular largo"
    suggest(c, evil, '<img src=x onerror=alert(2)> http://x.com "z"')
    for page in (c.get("/temas").text, c.get("/temas/1").text, admin.get("/").text):
        assert "<script>alert(1)" not in page and "<img src=x" not in page
    assert "&lt;script&gt;alert(1)" in c.get("/temas/1").text and "<a href=\"http://x.com" not in c.get("/temas/1").text
    post(c, "/temas/1/denunciar", motivo="<b>x</b>")
    assert "<b>x</b>" not in admin.get("/temas/1").text


def test_orden_por_votos_filtros_y_busqueda():
    _, app = new_app()
    AdminClient(app)
    a, b = user_client(app, "ana"), user_client(app, "ben")
    suggest(a, "Tema primero sobre alquileres"), suggest(a, "Tema segundo sobre sentencias")
    suggest(a, "Tema tercero sobre alquileres")
    post(b, "/temas/2/voto"), post(a, "/temas/2/voto"), post(b, "/temas/1/voto")
    assert [t["id"] for t in app.state.topics.list()] == [2, 1, 3]
    assert [t["id"] for t in app.state.topics.top(3)] == [2, 1]
    page = a.get("/temas?q=alquileres").text
    assert "primero" in page and "tercero" in page and "segundo" not in page
    assert "Los más votados" in a.get("/").text and "/temas/2" in a.get("/").text
    assert 'href="/temas"' in a.get("/").text


def test_portada_y_temas_vacios():
    _, app = new_app()
    c = TestClient(app)
    assert "Aún no hay temas" in c.get("/").text and "Ningún" not in c.get("/temas").text
    assert "Aún no hay temas" in c.get("/temas").text


def test_ocultar_rechazar_y_denunciar():
    _, app = new_app()
    admin = AdminClient(app)
    c = user_client(app)
    suggest(c)
    assert post(c, "/temas/1/denunciar", motivo="motivo-secreto-xyz").status_code == 303
    assert "motivo-secreto-xyz" in admin.get("/temas/1").text and "motivo-secreto-xyz" not in c.get("/temas/1").text
    assert post(c, "/temas/1/moderar", accion="ocultar").status_code == 403
    assert admin.post("/temas/1/moderar", data={"accion": "ocultar"}, follow_redirects=False).status_code == 303
    assert TITLE not in TestClient(app).get("/temas").text and TestClient(app).get("/temas/1").status_code == 404
    assert admin.get("/temas/1").status_code == 200
    admin.post("/temas/1/moderar", data={"accion": "mostrar"})
    admin.post("/temas/1/moderar", data={"accion": "rechazar"})
    assert TITLE not in c.get("/temas").text and post(c, "/temas/1/voto").status_code == 404
    admin.post("/temas/1/moderar", data={"accion": "hecho"})
    assert TITLE in c.get("/temas?estado=done").text


def test_admin_investiga_tema():
    texts = {"https://diario.example/a": "El casero declaró que la renta exigida era de 2.650 € al mes. " + "Relleno largo. " * 20}

    def handler(prompt):
        if "consultas de búsqueda" in prompt:
            return ["renta"]
        if "Clasifica el TIPO" in prompt:
            return {"type": "primary", "reason": "x"}
        if "localiza afirmaciones" in prompt:
            return [{"statement": "Renta 2.650", "speaker": "Casero", "quote": "la renta exigida era de 2.650 €", "value": "2.650 €"}]
        return "ok"

    db, app = new_app(llm=FakeLLM(handler=handler),
                      searcher=lambda q, n: [{"href": u, "title": "t", "body": "b"} for u in texts],
                      fetcher=lambda u: Source(url=u, text=texts[u], source_type=SourceType.SECONDARY, title="T"))
    admin = AdminClient(app)
    c = user_client(app)
    suggest(c, "Renta del contrato de alquiler")
    assert post(c, "/temas/1/investigar").status_code == 403
    assert "Investigar este tema" in admin.get("/temas/1").text and "Investigar este tema" not in c.get("/temas/1").text
    r = admin.post("/temas/1/investigar", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/jobs/")
    app.state.wait(r.headers["location"].rsplit("/", 1)[1])
    t = app.state.topics.get(1)
    assert t["status"] == "investigating" and t["fact_id"] == 1
    assert db.get_fact(1).title == "Renta del contrato de alquiler"
    assert "/facts/1" in c.get("/temas/1").text
    assert admin.post("/temas/1/investigar", follow_redirects=False).headers["location"] == "/facts/1"
