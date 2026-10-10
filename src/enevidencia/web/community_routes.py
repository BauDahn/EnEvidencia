"""Rutas de la comunidad (/comunidad) y de moderación (/admin/moderacion).

Integración: `install_community(app, db)` (una línea en app.py, después de install_auth).
Los textos de usuario son siempre texto plano: se escapan y solo se enlazan URLs http/https.
"""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from markupsafe import Markup, escape

from ..community import CATEGORIES, BODY_MAX, TITLE_MAX, Community, CommunityError, RateLimited
from .deps import current_user, require_admin, require_user

router = APIRouter()
AVISOS = {"hilo": "Tu hilo se ha publicado.", "respuesta": "Tu respuesta se ha publicado.",
          "denuncia": "Gracias: la denuncia se ha enviado a la moderación.", "editado": "Cambios guardados.",
          "eliminado": "El mensaje se ha eliminado."}
_URL = re.compile(r"https?://(?:(?!&lt;|&gt;|&#3[49];)[^\s])+")


def texto(value: str | None) -> Markup:
    """Texto plano -> HTML seguro: escapa todo, respeta saltos de línea y enlaza solo http/https con rel seguro."""
    esc = str(escape(value or ""))

    def link(m: re.Match) -> str:
        url = m.group(0)
        tail = ""
        while url and url[-1] in ".,;:!?)":
            tail, url = url[-1] + tail, url[:-1]
        return f'<a href="{url}" rel="nofollow ugc noopener noreferrer" target="_blank">{url}</a>{tail}'

    return Markup(_URL.sub(link, esc).replace("\n", "<br>\n"))


def _com(request: Request) -> Community:
    return request.app.state.community


def _render(request: Request, name: str, status: int = 200, **ctx):
    ctx.setdefault("section", "comunidad")
    ctx.setdefault("categories", CATEGORIES)
    return request.app.state.templates.TemplateResponse(request, name, ctx, status_code=status)


def _can_see(user, author_id: int) -> bool:
    return bool(user) and (user.is_admin or user.id == author_id)


def _state(row, user) -> dict:
    """Qué se muestra de un hilo o mensaje según su ocultación y quién mira."""
    h = row["hidden"]
    seer = _can_see(user, row["author_id"])
    shown = h == 0 or seer
    note = ""
    if h == 1:
        note = "Retirado por moderación: solo lo ven su autor y la administración."
    elif h == 2:
        note = "Eliminado por su autor: solo lo ve la administración." if (user and user.is_admin) else ""
        shown = bool(user and user.is_admin)
    placeholder = "[mensaje retirado por moderación]" if h == 1 else "[mensaje eliminado por su autor]"
    return {"shown": shown, "note": note, "placeholder": placeholder}


def _thread_cards(rows, user) -> list[dict]:
    out = []
    for r in rows:
        st = _state(r, user)
        body = r["body"] if st["shown"] else ""
        out.append({"r": r, **st, "excerpt": (body[:220] + "…") if len(body) > 220 else body})
    return out


def _qs(**kw) -> str:
    from urllib.parse import urlencode
    return urlencode({k: v for k, v in kw.items() if v not in (None, "", 1)})


# ---------------- lectura pública
@router.get("/comunidad", response_class=HTMLResponse)
def listing(request: Request, categoria: str = "", q: str = "", pagina: int = 1, hecho: int = 0):
    com, user = _com(request), current_user(request)
    cat = categoria if categoria in CATEGORIES else ""
    rows, total, page, pages = com.list_threads(cat, q, hecho or None, pagina)
    fact = request.app.state.community_db.get_fact(hecho) if hecho else None
    base = {"categoria": cat, "q": q.strip()[:80], "hecho": hecho or None}
    return _render(request, "comunidad.html", threads=_thread_cards(rows, user), total=total, page=page, pages=pages,
                   cat=cat, q=base["q"], fact=fact, hecho=hecho or "", qs=lambda p: _qs(**base, pagina=p),
                   aviso=AVISOS.get(request.query_params.get("aviso", "")))


@router.get("/comunidad/nuevo", response_class=HTMLResponse)
def new_form(request: Request, hecho: int = 0, user=Depends(require_user)):
    fact = request.app.state.community_db.get_fact(hecho) if hecho else None
    return _render(request, "comunidad_form.html", form={"category": "General", "fact_id": fact.id if fact else ""},
                   fact=fact, error=None)


def _thread_page(request: Request, thread_id: int, page: int = 1, status: int = 200, error=None, draft="", aviso=None):
    com, user = _com(request), current_user(request)
    t = com.get_thread(thread_id)
    if t is None or (t["hidden"] == 2 and not (user and user.is_admin)):
        raise HTTPException(404, "Este hilo no existe.")
    rows, total, page, pages = com.list_posts(thread_id, page)
    posts = []
    for p in rows:
        st = _state(p, user)
        posts.append({"r": p, **st, "can_edit": p["hidden"] == 0 and com.can_edit(p["author_id"], p["created_at"], user),
                      "mine": bool(user) and user.id == p["author_id"]})
    tst = _state(t, user)
    fact = request.app.state.community_db.get_fact(t["fact_id"]) if t["fact_id"] else None
    return _render(request, "comunidad_hilo.html", status, t=t, tst=tst, posts=posts, total=total, page=page, pages=pages,
                   error=error, draft=draft, fact=fact, qs_pag=lambda p: _qs(pagina=p), aviso=AVISOS.get(aviso or ""),
                   t_can_edit=t["hidden"] == 0 and com.can_edit(t["author_id"], t["created_at"], user),
                   t_mine=bool(user) and user.id == t["author_id"], max_body=BODY_MAX)


@router.get("/comunidad/{thread_id:int}", response_class=HTMLResponse)
def thread_view(request: Request, thread_id: int, pagina: int = 1, aviso: str = ""):
    return _thread_page(request, thread_id, pagina, aviso=aviso)


# ---------------- escribir (requiere sesión)
def _status(e: CommunityError) -> int:
    return 429 if isinstance(e, RateLimited) else 400


@router.post("/comunidad")
def create(request: Request, title: str = Form(""), body: str = Form(""), category: str = Form("General"),
           fact_id: str = Form(""), user=Depends(require_user)):
    fid = int(fact_id) if fact_id.strip().isdigit() and len(fact_id) < 10 else None
    try:
        tid = _com(request).create_thread(user, title, body, category, fid)
    except CommunityError as e:
        fact = request.app.state.community_db.get_fact(fid) if fid else None
        return _render(request, "comunidad_form.html", _status(e), form={"title": title, "body": body, "category": category,
                       "fact_id": fid or ""}, fact=fact, error=str(e))
    return RedirectResponse(f"/comunidad/{tid}?aviso=hilo", status_code=303)


@router.post("/comunidad/{thread_id:int}/responder")
def reply(request: Request, thread_id: int, body: str = Form(""), user=Depends(require_user)):
    com = _com(request)
    try:
        pid = com.add_post(user, thread_id, body)
    except CommunityError as e:
        return _thread_page(request, thread_id, 1, _status(e), str(e), body)
    rows, total, page, pages = com.list_posts(thread_id, 10 ** 6)
    return RedirectResponse(f"/comunidad/{thread_id}?{_qs(pagina=page, aviso='respuesta')}#m{pid}", status_code=303)


def _edit_page(request, kind, row, status=200, error=None, body=None):
    return _render(request, "comunidad_editar.html", status, kind=kind, row=row, error=error,
                   body=row["body"] if body is None else body, max_body=BODY_MAX)


def _own_or_403(row, user):
    if row is None:
        raise HTTPException(404, "No existe.")
    if row["author_id"] != user.id:
        raise HTTPException(403, "Solo el autor puede modificar su mensaje.")


@router.get("/comunidad/mensaje/{post_id:int}/editar", response_class=HTMLResponse)
def edit_post_form(request: Request, post_id: int, user=Depends(require_user)):
    p = _com(request).get_post(post_id)
    _own_or_403(p, user)
    return _edit_page(request, "post", p)


@router.post("/comunidad/mensaje/{post_id:int}/editar")
def edit_post(request: Request, post_id: int, body: str = Form(""), user=Depends(require_user)):
    com = _com(request)
    p = com.get_post(post_id)
    _own_or_403(p, user)
    try:
        com.edit_post(user, post_id, body)
    except CommunityError as e:
        return _edit_page(request, "post", p, 400, str(e), body)
    return RedirectResponse(f"/comunidad/{p['thread_id']}?aviso=editado#m{post_id}", status_code=303)


@router.get("/comunidad/{thread_id:int}/editar", response_class=HTMLResponse)
def edit_thread_form(request: Request, thread_id: int, user=Depends(require_user)):
    t = _com(request).get_thread(thread_id)
    _own_or_403(t, user)
    return _edit_page(request, "thread", t)


@router.post("/comunidad/{thread_id:int}/editar")
def edit_thread(request: Request, thread_id: int, body: str = Form(""), user=Depends(require_user)):
    com = _com(request)
    t = com.get_thread(thread_id)
    _own_or_403(t, user)
    try:
        com.edit_thread(user, thread_id, body)
    except CommunityError as e:
        return _edit_page(request, "thread", t, 400, str(e), body)
    return RedirectResponse(f"/comunidad/{thread_id}?aviso=editado", status_code=303)


@router.post("/comunidad/mensaje/{post_id:int}/eliminar")
def delete_post(request: Request, post_id: int, user=Depends(require_user)):
    com = _com(request)
    p = com.get_post(post_id)
    _own_or_403(p, user)
    com.delete_post(user, post_id)
    return RedirectResponse(f"/comunidad/{p['thread_id']}?aviso=eliminado", status_code=303)


@router.post("/comunidad/{thread_id:int}/eliminar")
def delete_thread(request: Request, thread_id: int, user=Depends(require_user)):
    com = _com(request)
    _own_or_403(com.get_thread(thread_id), user)
    com.delete_thread(user, thread_id)
    return RedirectResponse("/comunidad?aviso=eliminado", status_code=303)


@router.post("/comunidad/denunciar")
def report(request: Request, kind: str = Form(""), target_id: int = Form(0), reason: str = Form(""),
           user=Depends(require_user)):
    com = _com(request)
    try:
        com.report(user, kind, target_id, reason)
    except CommunityError as e:
        tid = 0
        row = com.get_thread(target_id) if kind == "thread" else (com.get_post(target_id) if kind == "post" else None)
        if row is not None:
            tid = target_id if kind == "thread" else row["thread_id"]
        if not tid:
            raise HTTPException(400, str(e))
        return _thread_page(request, tid, 1, _status(e), str(e))
    tid = target_id if kind == "thread" else com.get_post(target_id)["thread_id"]
    return RedirectResponse(f"/comunidad/{tid}?aviso=denuncia", status_code=303)


# ---------------- moderación (solo administradores)
@router.get("/admin/moderacion", response_class=HTMLResponse)
def moderation(request: Request, user=Depends(require_admin), aviso: str = "", error: str = ""):
    com = _com(request)
    return _render(request, "admin_moderacion.html", section="moderacion", reports=[r for r in com.open_reports() if r["kind"] != "topic"],
                   topic_reports=com.topic_reports(), users=com.list_users(), log=com.mod_log(),
                   aviso="Acción registrada." if aviso else None, error=error[:200] or None)


@router.post("/admin/moderacion/accion")
def moderation_action(request: Request, accion: str = Form(""), kind: str = Form(""), target_id: int = Form(0),
                      motivo: str = Form(""), report_id: int = Form(0), user=Depends(require_admin)):
    com, acc = _com(request), request.app.state.accounts
    motivo = motivo.strip()[:500]
    try:
        if accion in ("ocultar", "restaurar"):
            com.set_hidden(user.id, kind, target_id, accion == "ocultar", motivo)
        elif accion in ("ocultar_tema", "restaurar_tema"):
            com.set_topic_hidden(user.id, target_id, accion == "ocultar_tema", motivo)
        elif accion == "cerrar_denuncias_tema":
            com.clear_topic_reports(user.id, target_id, motivo)
        elif accion in ("bloquear", "desbloquear", "fijar", "desfijar"):
            flag = "locked" if accion in ("bloquear", "desbloquear") else "pinned"
            com.set_flag(user.id, target_id, flag, accion in ("bloquear", "fijar"), motivo)
        elif accion == "cerrar_denuncia":
            com.resolve_report(user.id, report_id, motivo)
        elif accion in ("banear", "desbanear"):
            target = acc.get_user(target_id)
            if target is None:
                raise CommunityError("El usuario no existe.")
            if target.id == user.id:
                raise CommunityError("No puedes bloquear tu propia cuenta.")
            if target.is_admin:
                raise CommunityError("No se puede bloquear a otro administrador.")
            if accion == "banear":
                if not motivo:
                    raise CommunityError("Indica el motivo del bloqueo.")
                acc.ban(target.id)
            else:
                acc.unban(target.id)
            com.log(user.id, accion, f"user:{target.id} (@{target.username})", motivo)
        else:
            raise CommunityError("Acción no válida.")
    except CommunityError as e:
        return RedirectResponse(f"/admin/moderacion?{_qs(error=str(e))}", status_code=303)
    return RedirectResponse("/admin/moderacion?aviso=1", status_code=303)


def install_community(app: FastAPI, db) -> Community:
    """Crea tablas, registra el filtro `comtexto`, la función `debates_de_hecho` y las rutas."""
    app.state.community_db = db
    com = app.state.community = Community(db.conn)
    env = app.state.templates.env
    env.filters["comtexto"] = texto
    env.globals["debates_de_hecho"] = lambda fact_id: com.threads_for_fact(fact_id)
    app.include_router(router)
    return com
