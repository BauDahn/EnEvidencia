import re

import pytest
from fastapi.testclient import TestClient

from conftest import AdminClient, csrf_of
from enevidencia.community import Community
from enevidencia.models import Fact
from enevidencia.storage import Storage
from enevidencia.web.app import create_app

PW = "una-contrasena-larga"
BODY = "Este es un mensaje de prueba suficientemente largo."
TITLE = "Titular de prueba para el hilo"


def make():
    db = Storage(":memory:")
    app = create_app(db)
    return db, app


def user_client(app, name):
    c = TestClient(app)
    r = c.post("/registro", data={"username": name, "email": f"{name}@example.org", "password": PW, "acepto": "1",
                                  "csrf_token": csrf_of(c)}, follow_redirects=False)
    assert r.status_code == 303
    return c


def post(c, url, data=None, **kw):
    return c.post(url, data={**(data or {}), "csrf_token": csrf_of(c)}, follow_redirects=False, **kw)


def new_thread(c, title=TITLE, body=BODY, category="General", **extra):
    r = post(c, "/comunidad", {"title": title, "body": body, "category": category, **extra})
    return r


def tid_of(r):
    return int(re.search(r"/comunidad/(\d+)", r.headers["location"]).group(1))


@pytest.fixture
def env():
    db, app = make()
    admin = AdminClient(app)
    ana = user_client(app, "ana")
    return db, app, admin, ana


def test_crear_hilo_y_ver(env):
    db, app, admin, ana = env
    r = new_thread(ana)
    assert r.status_code == 303
    page = TestClient(app).get(f"/comunidad/{tid_of(r)}")
    assert TITLE in page.text and "@ana" in page.text
    assert TITLE in TestClient(app).get("/comunidad").text


def test_anonimo_redirigido_y_lectura_publica(env):
    db, app, admin, ana = env
    anon = TestClient(app)
    assert anon.get("/comunidad").status_code == 200
    assert "Entra o crea una cuenta para participar" in anon.get("/comunidad").text
    r = post(anon, "/comunidad", {"title": TITLE, "body": BODY, "category": "General"})
    assert r.status_code == 303 and "/entrar" in r.headers["location"]
    assert db.conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0] == 0
    assert anon.get("/comunidad/nuevo", follow_redirects=False).status_code == 303


def test_responder_y_paginacion(env):
    db, app, admin, ana = env
    tid = tid_of(new_thread(ana))
    com = app.state.community
    com.max_per_hour = 1000
    for i in range(25):
        r = post(ana, f"/comunidad/{tid}/responder", {"body": f"Respuesta número {i} del lector"})
        assert r.status_code == 303
    p1, p2 = ana.get(f"/comunidad/{tid}").text, ana.get(f"/comunidad/{tid}?pagina=2").text
    assert "Respuesta número 0 " in p1 and "Respuesta número 24 " not in p1
    assert "Respuesta número 24 " in p2
    assert ana.get(f"/comunidad/{tid}?pagina=999").status_code == 200
    assert ana.get(f"/comunidad/{tid}?pagina=-5").status_code == 200
    assert "25 respuestas" in p1


def test_listado_paginado_busqueda_categoria(env):
    db, app, admin, ana = env
    app.state.community.max_per_hour = 1000
    for i in range(17):
        assert new_thread(ana, f"Titular número {i:02d} distinto", f"Cuerpo distinto número {i}", "Fuentes y datos").status_code == 303
    new_thread(ana, "Una pregunta sobre metodología", "Texto con la palabra zorro dentro", "Metodología")
    anon = TestClient(app)
    assert "Siguiente" in anon.get("/comunidad").text
    assert len(re.findall(r'class="com-item', anon.get("/comunidad").text)) == 15
    assert "zorro" in anon.get("/comunidad?q=zorro").text
    assert "Ninguna carta coincide" in anon.get("/comunidad?q=%25%27%3B--").text
    assert "Una pregunta" not in anon.get("/comunidad?categoria=Fuentes+y+datos&pagina=2").text
    assert anon.get("/comunidad?pagina=abc").status_code in (200, 422)  # nunca un error de servidor


def test_baneado_rechazado(env):
    db, app, admin, ana = env
    uid = db.conn.execute("SELECT id FROM users WHERE username='ana'").fetchone()[0]
    app.state.accounts.ban(uid)
    r = new_thread(ana)
    assert r.status_code == 303 and "/entrar" in r.headers["location"]
    assert db.conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0] == 0
    # a nivel de módulo también
    u = app.state.accounts.get_user(uid)
    from enevidencia.community import CommunityError
    with pytest.raises(CommunityError):
        app.state.community.create_thread(u, TITLE, BODY, "General")


def test_limites_y_duplicados(env):
    db, app, admin, ana = env
    assert new_thread(ana, "corto", BODY).status_code == 400
    assert new_thread(ana, "x" * 141, BODY).status_code == 400
    assert new_thread(ana, TITLE, "abcd").status_code == 400
    assert new_thread(ana, TITLE, "a" * 5001).status_code == 400
    assert new_thread(ana, TITLE, BODY, "Inventada").status_code == 400
    assert new_thread(ana, TITLE, BODY, fact_id="999").status_code == 400
    assert new_thread(ana).status_code == 303
    dup = new_thread(ana, "Otro titular totalmente nuevo")
    assert dup.status_code == 400 and "duplicados" in dup.text
    tid = db.conn.execute("SELECT id FROM threads").fetchone()[0]
    assert post(ana, f"/comunidad/{tid}/responder", {"body": "hola"}).status_code == 400


def test_limite_por_hora(env):
    db, app, admin, ana = env
    for i in range(10):
        assert post(ana, "/comunidad", {"title": f"Titular válido {i}", "body": f"Cuerpo válido número {i}", "category": "General"}).status_code == 303
    r = post(ana, "/comunidad", {"title": "Otro titular más", "body": "Cuerpo número once", "category": "General"})
    assert r.status_code == 429
    # el admin no está limitado por este tope
    assert post(admin, "/comunidad", {"title": "Titular de admin", "body": "Cuerpo del admin", "category": "General"}).status_code == 303


def test_editar_propio_ajeno_y_ventana(env):
    db, app, admin, ana = env
    beto = user_client(app, "beto")
    pid_thread = tid_of(new_thread(ana))
    post(beto, f"/comunidad/{pid_thread}/responder", {"body": "Mi primera respuesta de beto"})
    pid = db.conn.execute("SELECT id FROM posts").fetchone()[0]
    assert ana.get(f"/comunidad/mensaje/{pid}/editar").status_code == 403
    assert post(ana, f"/comunidad/mensaje/{pid}/editar", {"body": "intento ajeno de editar"}).status_code == 403
    assert post(ana, f"/comunidad/mensaje/{pid}/eliminar").status_code == 403
    assert post(beto, f"/comunidad/mensaje/{pid}/editar", {"body": "Texto corregido por beto"}).status_code == 303
    assert "Texto corregido por beto" in ana.get(f"/comunidad/{pid_thread}").text
    # fuera de ventana
    db.conn.execute("UPDATE posts SET created_at='2000-01-01T00:00:00+00:00'")
    db.conn.commit()
    r = post(beto, f"/comunidad/mensaje/{pid}/editar", {"body": "ya es tarde para editar"})
    assert r.status_code == 400 and "15 minutos" in r.text
    assert f"/comunidad/mensaje/{pid}/editar" not in beto.get(f"/comunidad/{pid_thread}").text
    # eliminar lo propio sí se puede siempre
    assert post(beto, f"/comunidad/mensaje/{pid}/eliminar").status_code == 303
    assert "eliminado por su autor" in ana.get(f"/comunidad/{pid_thread}").text
    # hilo ajeno
    assert post(beto, f"/comunidad/{pid_thread}/editar", {"body": "no es mío, no edito"}).status_code == 403
    assert post(ana, f"/comunidad/{pid_thread}/editar", {"body": "Cuerpo del hilo editado ok"}).status_code == 303


def test_denunciar_y_cola(env):
    db, app, admin, ana = env
    beto = user_client(app, "beto")
    tid = tid_of(new_thread(ana))
    assert post(beto, "/comunidad/denunciar", {"kind": "thread", "target_id": tid, "reason": "Insultos"}).status_code == 303
    r = post(beto, "/comunidad/denunciar", {"kind": "thread", "target_id": tid, "reason": "Insultos otra vez"})
    assert r.status_code == 400 and "Ya has denunciado" in r.text
    assert post(beto, "/comunidad/denunciar", {"kind": "post", "target_id": 999, "reason": "x" * 5}).status_code == 400
    assert post(beto, "/comunidad/denunciar", {"kind": "thread", "target_id": tid, "reason": ""}).status_code in (400,)
    page = admin.get("/admin/moderacion").text
    assert "Insultos" in page and "@beto" in page


def test_moderacion_ocultar_restaurar_bloquear_fijar(env):
    db, app, admin, ana = env
    beto = user_client(app, "beto")
    tid = tid_of(new_thread(ana))
    post(beto, f"/comunidad/{tid}/responder", {"body": "Respuesta que será retirada"})
    pid = db.conn.execute("SELECT id FROM posts").fetchone()[0]
    post(beto, "/comunidad/denunciar", {"kind": "post", "target_id": pid, "reason": "Spam"})
    rid = db.conn.execute("SELECT id FROM reports").fetchone()[0]
    assert admin.post("/admin/moderacion/accion", {"accion": "ocultar", "kind": "post", "target_id": pid, "motivo": "spam"},
                      follow_redirects=False).status_code == 303
    anon = TestClient(app)
    t = anon.get(f"/comunidad/{tid}").text
    assert "[mensaje retirado por moderación]" in t and "Respuesta que será retirada" not in t
    assert "Respuesta que será retirada" in beto.get(f"/comunidad/{tid}").text  # autor lo ve con aviso
    assert "Retirado por moderación" in beto.get(f"/comunidad/{tid}").text
    assert "Respuesta que será retirada" in admin.get(f"/comunidad/{tid}").text
    admin.post("/admin/moderacion/accion", {"accion": "restaurar", "kind": "post", "target_id": pid}, follow_redirects=False)
    assert "Respuesta que será retirada" in anon.get(f"/comunidad/{tid}").text
    # hilo: ocultar
    admin.post("/admin/moderacion/accion", {"accion": "ocultar", "kind": "thread", "target_id": tid}, follow_redirects=False)
    assert TITLE not in anon.get(f"/comunidad/{tid}").text and TITLE not in anon.get("/comunidad").text
    assert "[hilo retirado por moderación]" in anon.get("/comunidad").text
    admin.post("/admin/moderacion/accion", {"accion": "restaurar", "kind": "thread", "target_id": tid}, follow_redirects=False)
    # bloquear
    admin.post("/admin/moderacion/accion", {"accion": "bloquear", "kind": "thread", "target_id": tid}, follow_redirects=False)
    r = post(beto, f"/comunidad/{tid}/responder", {"body": "Intento en hilo cerrado"})
    assert r.status_code == 400 and "cerrado" in r.text
    admin.post("/admin/moderacion/accion", {"accion": "desbloquear", "kind": "thread", "target_id": tid}, follow_redirects=False)
    assert post(beto, f"/comunidad/{tid}/responder", {"body": "Ahora sí se puede responder"}).status_code == 303
    # fijar: el hilo fijado va primero
    t2 = tid_of(new_thread(ana, "Segundo hilo para ordenar", "Cuerpo del segundo hilo"))
    admin.post("/admin/moderacion/accion", {"accion": "fijar", "kind": "thread", "target_id": tid}, follow_redirects=False)
    listing = anon.get("/comunidad").text
    assert listing.index(f"/comunidad/{tid}\"") < listing.index(f"/comunidad/{t2}\"")
    # cerrar denuncia + registro
    admin.post("/admin/moderacion/accion", {"accion": "cerrar_denuncia", "report_id": rid, "motivo": "revisada"}, follow_redirects=False)
    page = admin.get("/admin/moderacion").text
    assert "ocultar_post" in page and "restaurar_post" in page and "bloquear_hilo" in page and "fijar_hilo" in page and "cerrar_denuncia" in page
    assert db.conn.execute("SELECT resolved FROM reports").fetchone()[0] == 1


def test_ban_unban_y_autoban(env):
    db, app, admin, ana = env
    uid = db.conn.execute("SELECT id FROM users WHERE username='ana'").fetchone()[0]
    r = admin.post("/admin/moderacion/accion", {"accion": "banear", "kind": "user", "target_id": uid}, follow_redirects=False)
    assert "error=" in r.headers["location"]  # falta motivo
    admin.post("/admin/moderacion/accion", {"accion": "banear", "kind": "user", "target_id": uid, "motivo": "spam repetido"}, follow_redirects=False)
    assert app.state.accounts.get_user(uid).banned
    assert ana.get("/cuenta", follow_redirects=False).status_code == 303  # sesión cerrada
    admin.post("/admin/moderacion/accion", {"accion": "desbanear", "kind": "user", "target_id": uid}, follow_redirects=False)
    assert not app.state.accounts.get_user(uid).banned
    r = admin.post("/admin/moderacion/accion", {"accion": "banear", "kind": "user", "target_id": 1, "motivo": "yo"}, follow_redirects=False)
    assert "error=" in r.headers["location"] and not app.state.accounts.get_user(1).banned
    assert "spam repetido" in admin.get("/admin/moderacion").text


def test_unban_accounts():
    from enevidencia.accounts import Accounts
    import sqlite3
    acc = Accounts(sqlite3.connect(":memory:", check_same_thread=False))
    acc.register("admin", "a@example.org", PW)
    u = acc.register("beto", "b@example.org", PW)
    acc.ban(u.id)
    assert acc.get_user(u.id).banned
    acc.unban(u.id)
    assert not acc.get_user(u.id).banned
    acc.authenticate("beto", PW)


def test_acceso_moderacion_por_rol(env):
    db, app, admin, ana = env
    assert ana.get("/admin/moderacion").status_code == 403
    assert post(ana, "/admin/moderacion/accion", {"accion": "ocultar", "kind": "thread", "target_id": 1}).status_code == 403
    anon = TestClient(app)
    assert anon.get("/admin/moderacion", follow_redirects=False).status_code == 303
    assert post(anon, "/admin/moderacion/accion", {"accion": "ocultar"}).status_code == 403
    assert admin.get("/admin/moderacion").status_code == 200
    assert "Moderación" not in ana.get("/comunidad").text.split("<main")[0]


def test_xss_escapado(env):
    db, app, admin, ana = env
    xss = '<script>alert(1)</script> <img src=x onerror=alert(2)> http://example.org/a?b=1&c="><b>x</b>'
    tid = tid_of(new_thread(ana, "Titular <b>negrita</b> peligrosa", xss))
    post(ana, f"/comunidad/{tid}/responder", {"body": "javascript:alert(1) y <svg onload=alert(3)> más texto"})
    page = TestClient(app).get(f"/comunidad/{tid}").text
    for bad in ("<script>alert(1)", "<img src=x", "<svg onload", "<b>negrita", '<b>x</b>'):
        assert bad not in page
    assert "&lt;script&gt;" in page
    assert 'rel="nofollow ugc noopener noreferrer"' in page and 'href="http://example.org/a?b=1&amp;c="' in page
    assert 'href="javascript' not in page
    assert "&lt;b&gt;negrita" in TestClient(app).get("/comunidad").text


def test_saltos_de_linea(env):
    db, app, admin, ana = env
    tid = tid_of(new_thread(ana, body="Línea uno del mensaje\nLínea dos del mensaje"))
    assert "Línea uno del mensaje<br>" in ana.get(f"/comunidad/{tid}").text


def test_enlace_hecho_debate(env):
    db, app, admin, ana = env
    f = db.add_fact(Fact("Renta del contrato"))
    page = admin.get(f"/facts/{f.id}").text
    assert f"/comunidad?hecho={f.id}" in page or f"/comunidad/nuevo?hecho={f.id}" in page
    assert "Debate de lectores" in page and "Abrir debate sobre este hecho" in page
    assert ana.get(f"/comunidad/nuevo?hecho={f.id}").status_code == 200
    tid = tid_of(new_thread(ana, "Debate sobre la renta pactada", "Creo que falta una fuente", fact_id=str(f.id)))
    page = admin.get(f"/facts/{f.id}").text
    assert "Debate sobre la renta pactada" in page
    assert "Debate sobre la renta pactada" in ana.get(f"/comunidad?hecho={f.id}").text
    assert "Renta del contrato" in ana.get(f"/comunidad/{tid}").text


def test_csrf_y_navegacion(env):
    db, app, admin, ana = env
    r = ana.post("/comunidad", data={"title": TITLE, "body": BODY, "category": "General"}, follow_redirects=False)
    assert r.status_code == 403
    assert 'href="/comunidad"' in TestClient(app).get("/").text


def test_admin_insignia_y_404(env):
    db, app, admin, ana = env
    tid = tid_of(new_thread(admin, "Aviso de la administración", "Texto de aviso oficial"))
    assert "Admin" in TestClient(app).get(f"/comunidad/{tid}").text
    assert TestClient(app).get("/comunidad/9999").status_code == 404


def test_denuncias_de_temas_si_existen(env):
    db, app, admin, ana = env
    assert admin.get("/admin/moderacion").status_code == 200  # sin tabla topic_reports o con ella vacía
    db.conn.execute("INSERT INTO reports(kind,target_id,reporter_id,reason,created_at) VALUES ('topic',1,2,'Tema ofensivo','2026-01-01T00:00:00')")
    db.conn.commit()
    assert "Tema ofensivo" in admin.get("/admin/moderacion").text
