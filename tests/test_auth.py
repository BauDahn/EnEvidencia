import re

import pytest
from fastapi.testclient import TestClient

from conftest import AdminClient, csrf_of
from enevidencia.accounts import MAX_FAILS, Accounts
from enevidencia.storage import Storage
from enevidencia.web.app import create_app

PW = "una-contrasena-larga"


def new_app():
    db = Storage(":memory:")
    return db, create_app(db)


def signup(c, user="ana", email="ana@example.org", pw=PW, ok="1", **extra):
    data = {"username": user, "email": email, "password": pw, "csrf_token": csrf_of(c), **extra}
    if ok:
        data["acepto"] = ok
    return c.post("/registro", data=data, follow_redirects=False)


def login(c, user, pw=PW, **extra):
    return c.post("/entrar", data={"username": user, "password": pw, "csrf_token": csrf_of(c), **extra}, follow_redirects=False)


def test_primer_usuario_admin_y_segundo_user():
    db, app = new_app()
    a, b = TestClient(app), TestClient(app)
    assert signup(a).status_code == 303
    assert signup(b, "beto", "beto@example.org").status_code == 303
    roles = dict(db.conn.execute("SELECT username, role FROM users").fetchall())
    assert roles == {"ana": "admin", "beto": "user"}
    assert "@ana" in a.get("/").text and "Salir" in a.get("/").text


def test_validaciones_de_registro():
    _, app = new_app()
    c = TestClient(app)
    assert signup(c, pw="corta").status_code == 400
    assert signup(c, user="AB").status_code == 400
    assert signup(c, user="a b!").status_code == 400
    assert signup(c, email="no-es-correo").status_code == 400
    assert signup(c, ok="").status_code == 400          # sin aceptar las normas
    assert signup(c).status_code == 303
    dup = signup(TestClient(app), "ANA", "otra@example.org")  # usuario duplicado sin distinguir mayúsculas
    assert dup.status_code == 400
    assert signup(TestClient(app), "otra", "ana@example.org").status_code == 400


def test_contrasena_hasheada_con_sal():
    db, app = new_app()
    signup(TestClient(app))
    signup(TestClient(app), "beto", "b@example.org")
    hs = [r[0] for r in db.conn.execute("SELECT password_hash FROM users")]
    assert all(h.startswith("scrypt$") and PW not in h for h in hs) and hs[0] != hs[1]


def test_login_correcto_e_incorrecto_mensaje_generico():
    _, app = new_app()
    signup(TestClient(app))
    c = TestClient(app)
    bad_user = login(c, "nadie", PW)
    bad_pass = login(c, "ana", "incorrecta-larga")
    assert bad_user.status_code == bad_pass.status_code == 400
    msg = lambda r: re.search(r'role="alert">.*?<p>(.*?)</p>', r.text, re.S).group(1)  # noqa: E731
    assert msg(bad_user) == msg(bad_pass)
    assert login(c, "ANA").status_code == 303  # el usuario no distingue mayúsculas
    assert login(c, "ana").status_code == 303 and "ee_session" in c.cookies


def test_bloqueo_por_intentos_fallidos():
    _, app = new_app()
    signup(TestClient(app))
    c = TestClient(app)
    for _ in range(MAX_FAILS):
        assert login(c, "ana", "mala-contrasena-1").status_code == 400
    r = login(c, "ana", PW)       # aunque sea la correcta, está bloqueado
    assert r.status_code == 400 and "Demasiados intentos" in r.text


def test_bloqueo_expira_con_el_tiempo():
    t = [0.0]
    db = Storage(":memory:")
    acc = Accounts(db.conn, clock=lambda: t[0])
    acc.register("ana", "ana@example.org", PW)
    for _ in range(MAX_FAILS):
        with pytest.raises(Exception):
            acc.authenticate("ana", "mala-contrasena-1", "1.1.1.1")
    assert acc.is_locked("1.1.1.1", "ana") and not acc.is_locked("2.2.2.2", "ana")
    t[0] += 16 * 60
    assert acc.authenticate("ana", PW, "1.1.1.1").username == "ana"


def test_logout_invalida_la_sesion():
    _, app = new_app()
    c = TestClient(app)
    signup(c)
    token = c.cookies["ee_session"]
    assert c.get("/cuenta").status_code == 200
    r = c.post("/salir", data={"csrf_token": csrf_of(c)}, follow_redirects=False)
    assert r.status_code == 303
    old = TestClient(app)
    old.cookies.set("ee_session", token)
    assert old.get("/cuenta", follow_redirects=False).status_code == 303   # el token viejo ya no vale
    assert app.state.accounts.user_from_token(token) is None


def test_sesion_caducada_no_vale():
    db, app = new_app()
    c = TestClient(app)
    signup(c)
    db.conn.execute("UPDATE sessions SET expires_at='2000-01-01T00:00:00'")
    db.conn.commit()
    assert app.state.accounts.user_from_token(c.cookies["ee_session"]) is None


def test_usuario_bloqueado_pierde_la_sesion():
    _, app = new_app()
    c = TestClient(app)
    signup(c)
    app.state.accounts.ban(1)
    assert c.get("/cuenta", follow_redirects=False).status_code == 303
    assert login(TestClient(app), "ana").status_code == 400


def test_csrf_rechaza_post_sin_token():
    db, app = new_app()
    c = AdminClient(app)
    plain = TestClient(app)
    plain.cookies.set("ee_session", c.cookies["ee_session"])   # sesión de admin pero sin token CSRF
    assert plain.post("/facts", data={"title": "X"}).status_code == 403
    csrf_of(plain)
    assert plain.post("/facts", data={"title": "X", "csrf_token": "falso"}).status_code == 403
    assert plain.post("/entrar", data={"username": "a", "password": "b"}).status_code == 403
    assert db.list_facts() == []
    assert plain.post("/facts", data={"title": "X", "csrf_token": plain.cookies["ee_csrf"]}).status_code in (200, 303)
    assert len(db.list_facts()) == 1


def test_csrf_tambien_con_cabecera():
    _, app = new_app()
    c = AdminClient(app)
    r = TestClient.post(c, "/facts", data={"title": "Y"}, headers={"x-csrf-token": c.cookies["ee_csrf"]})
    assert r.status_code in (200, 303)


def test_solo_admin_puede_investigar_y_escribir():
    db, app = new_app()
    admin = AdminClient(app)
    user = TestClient(app)
    signup(user, "beto", "beto@example.org")
    anon = TestClient(app)
    for c in (user, anon):
        tok = csrf_of(c)
        for url, data in [("/investigate", {"topic": "T"}), ("/facts", {"title": "T"}),
                          ("/sources", {"url": "https://a.com", "text": "t"}),
                          ("/sources/1/status", {"status": "rejected"}), ("/claims/1/status", {"status": "rejected"}),
                          ("/sources/1/type", {"source_type": "primary"}), ("/facts/1/extract", {}),
                          ("/claims", {"source_id": 1, "fact_id": 1, "statement": "a", "speaker": "b", "quote": "c"})]:
            r = c.post(url, data={**data, "csrf_token": tok}, follow_redirects=False)
            assert r.status_code == 403, url
    assert db.list_facts() == [] and db.list_sources() == []
    assert admin.post("/facts", data={"title": "Ok"}, follow_redirects=False).status_code == 303


def test_lectura_publica_y_formularios_ocultos():
    _, app = new_app()
    admin = AdminClient(app)
    admin.post("/facts", data={"title": "Hecho público"})
    anon = TestClient(app)
    home = anon.get("/").text
    assert "Hecho público" in home and 'action="/investigate"' not in home and "Entrar para participar" in home
    assert anon.get("/facts/1").status_code == 200 and anon.get("/facts/1/audit").status_code == 200
    assert 'action="/investigate"' in admin.get("/").text
    assert f'name="csrf_token" value="{admin.cookies["ee_csrf"]}"' in admin.get("/").text
    user = TestClient(app)
    signup(user, "beto", "b@example.org")
    assert 'action="/investigate"' not in user.get("/").text


def test_review_oculta_controles_a_no_admin():
    db, app = new_app()
    admin = AdminClient(app)
    admin.post("/sources", data={"url": "https://a.com", "text": "texto", "kind": "noticia"})
    db.add_fact(__import__("enevidencia.models", fromlist=["Fact"]).Fact("H"))
    db.conn.execute("INSERT INTO fact_sources(fact_id, source_id) VALUES (1,1)")
    db.conn.commit()
    assert "/sources/1/status" in admin.get("/facts/1/review").text
    assert "/sources/1/status" not in TestClient(app).get("/facts/1/review").text
    assert "/sources/1/type" not in TestClient(app).get("/sources/1").text


def test_flags_de_cookie(monkeypatch):
    _, app = new_app()
    r = TestClient(app).get("/")
    csrf = r.headers["set-cookie"].lower()
    assert "httponly" in csrf and "samesite=lax" in csrf and "secure" not in csrf
    monkeypatch.setenv("ENEVIDENCIA_SECURE_COOKIES", "1")
    c = TestClient(app, base_url="https://testserver")
    r = signup(c, "carla", "c@example.org")
    cookies = [h.lower() for h in r.headers.get_list("set-cookie") if h.lower().startswith("ee_session")]
    assert cookies and all("httponly" in h and "samesite=lax" in h and "secure" in h for h in cookies)


@pytest.mark.parametrize("nxt", ["https://evil.com", "//evil.com", "/\\evil.com", "javascript:alert(1)"])
def test_next_externo_rechazado(nxt):
    _, app = new_app()
    signup(TestClient(app))
    r = login(TestClient(app), "ana", next=nxt)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert login(TestClient(app), "ana", next="/facts/1").headers["location"] == "/facts/1"


def test_require_user_redirige_con_next_interno():
    _, app = new_app()
    r = TestClient(app).get("/cuenta", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/entrar")


def test_html_del_nombre_de_usuario_se_escapa():
    db, app = new_app()
    c = TestClient(app)
    db.conn.execute("INSERT INTO users(username,email,password_hash,role,created_at) VALUES (?,?,?,?,?)",
                    ("<script>alert(1)</script>", "x@example.org", "x", "user", "2026-01-01T00:00:00"))
    db.conn.commit()
    c.cookies.set("ee_session", app.state.accounts.create_session(app.state.accounts.get_user(1)))
    page = c.get("/cuenta").text + c.get("/").text
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page


def test_cabeceras_de_seguridad():
    _, app = new_app()
    h = TestClient(app).get("/").headers
    assert h["x-content-type-options"] == "nosniff" and h["x-frame-options"] == "DENY"
    assert "referrer-policy" in h and "frame-ancestors 'none'" in h["content-security-policy"]


def test_normas_y_registro_enlazan():
    _, app = new_app()
    c = TestClient(app)
    assert "no es público" in c.get("/normas").text
    assert 'href="/normas"' in c.get("/registro").text and 'href="/normas"' in c.get("/").text
