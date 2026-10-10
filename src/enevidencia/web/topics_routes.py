"""Rutas de TEMAS: los lectores sugieren y apoyan temas; el administrador decide cuándo investigarlos.

Los votos solo miden interés (no deciden la verdad de nada). Todo texto de usuario se muestra escapado.
Uso en app.py: `install_topics(app, db)` (tras install_auth). Necesita `app.state.launch_investigation(titulo) -> Job`.
"""
from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from ..llm import LLMError
from ..storage import Storage
from ..topics import MAX_DESC, MAX_PER_DAY, MAX_TITLE, MIN_TITLE, STATUS_LABELS, Topics, TopicError
from .deps import current_user, require_admin, require_user

router = APIRouter()
FILTERS = {"": "Abiertos", "investigating": "En investigación", "done": "Investigados"}


def _render(request: Request, name: str, code: int = 200, **ctx):
    return request.app.state.templates.TemplateResponse(request, name, ctx, status_code=code)


def _topics(request: Request) -> Topics:
    return request.app.state.topics


def _is_admin(request: Request) -> bool:
    u = current_user(request)
    return bool(u and u.is_admin)


def _participant(request: Request):
    """Usuario con sesión y no baneado (los baneados ya no tienen sesión válida; se comprueba igualmente)."""
    user = require_user(request)
    if user.banned:
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="Tu cuenta no puede participar.")
    return user


def _list_ctx(request: Request, status: str, q: str, **extra) -> dict:
    admin = _is_admin(request)
    t = _topics(request)
    user = current_user(request)
    status = status if status in ("investigating", "done") or (admin and status == "rejected") else ""
    return {"topics": t.list(status=status, q=q, include_hidden=admin and request.query_params.get("ocultos") == "1"),
            "filtro": status, "q": q, "filters": FILTERS, "labels": STATUS_LABELS,
            "voted": t.voted_ids(user.id) if user else set(), "max_title": MAX_TITLE, "min_title": MIN_TITLE,
            "max_desc": MAX_DESC, "max_day": MAX_PER_DAY, "form": {}, "error": None, "duplicate": None, **extra}


@router.get("/temas", response_class=HTMLResponse)
def topics_index(request: Request, estado: str = "", q: str = ""):
    return _render(request, "temas.html", **_list_ctx(request, estado, q[:100]))


@router.post("/temas")
def topics_suggest(request: Request, titulo: str = Form(""), descripcion: str = Form(""), user=Depends(_participant)):
    try:
        tid = _topics(request).suggest(user.id, titulo, descripcion)
    except TopicError as e:
        return _render(request, "temas.html", 400, **_list_ctx(request, "", "", form={"titulo": titulo[:MAX_TITLE * 2],
                       "descripcion": descripcion[:MAX_DESC * 2]}, error=str(e), duplicate=e.existing_id))
    return RedirectResponse(f"/temas/{tid}", status_code=303)


@router.get("/temas/{topic_id}", response_class=HTMLResponse)
def topic_view(request: Request, topic_id: int):
    admin = _is_admin(request)
    t = _topics(request)
    topic = t.get(topic_id, include_hidden=admin)
    if not topic:
        return PlainTextResponse("Tema no encontrado", status_code=404)
    user = current_user(request)
    return _render(request, "tema.html", topic=topic, labels=STATUS_LABELS, is_admin=admin,
                   voted=bool(user and topic_id in t.voted_ids(user.id)),
                   reports=t.reports(topic_id) if admin else [], error=request.query_params.get("error"),
                   notice=request.query_params.get("ok"))


@router.post("/temas/{topic_id}/voto")
def topic_vote(request: Request, topic_id: int, next: str = Form(""), user=Depends(_participant)):
    t = _topics(request)
    topic = t.get(topic_id)
    if not topic or topic["status"] == "rejected":
        return PlainTextResponse("Tema no encontrado", status_code=404)
    back = next if next in ("/", "/temas") else f"/temas/{topic_id}"
    if not t.vote_limiter.allow(user.id):
        return PlainTextResponse("Demasiadas acciones seguidas. Espera un minuto.", status_code=429)
    t.toggle_vote(topic_id, user.id)
    return RedirectResponse(back + f"#tema-{topic_id}" if back == "/temas" else back, status_code=303)


@router.post("/temas/{topic_id}/denunciar")
def topic_report(request: Request, topic_id: int, motivo: str = Form(""), user=Depends(_participant)):
    t = _topics(request)
    if not t.get(topic_id):
        return PlainTextResponse("Tema no encontrado", status_code=404)
    if not t.vote_limiter.allow(user.id):
        return PlainTextResponse("Demasiadas acciones seguidas. Espera un minuto.", status_code=429)
    t.report(topic_id, user.id, motivo)
    return RedirectResponse(f"/temas/{topic_id}?ok=Gracias.+El+administrador+revisar%C3%A1+el+tema.", status_code=303)


# ---- administración
@router.post("/temas/{topic_id}/investigar", dependencies=[Depends(require_admin)])
def topic_investigate(request: Request, topic_id: int):
    t = _topics(request)
    topic = t.get(topic_id, include_hidden=True)
    if not topic:
        return PlainTextResponse("Tema no encontrado", status_code=404)
    if topic["fact_id"]:
        return RedirectResponse(f"/facts/{topic['fact_id']}", status_code=303)
    if not t.claim_investigation(topic_id):  # otra llamada ya lo reservó (doble clic / carrera)
        again = t.get(topic_id, include_hidden=True)
        return RedirectResponse(f"/facts/{again['fact_id']}" if again and again["fact_id"] else f"/temas/{topic_id}",
                                status_code=303)
    try:
        job = request.app.state.launch_investigation(topic["title"])
    except Exception as e:  # noqa: BLE001 - se libera la reserva para poder reintentar
        t.release_investigation(topic_id)
        if isinstance(e, LLMError):
            return RedirectResponse(f"/temas/{topic_id}?error={quote(str(e))}", status_code=303)
        raise
    t.set_status(topic_id, "investigating", fact_id=job.fact_id)
    return RedirectResponse(f"/jobs/{job.id}", status_code=303)


@router.post("/temas/{topic_id}/moderar", dependencies=[Depends(require_admin)])
def topic_moderate(request: Request, topic_id: int, accion: str = Form(...)):
    t = _topics(request)
    if not t.get(topic_id, include_hidden=True):
        return PlainTextResponse("Tema no encontrado", status_code=404)
    if accion == "ocultar":
        t.set_hidden(topic_id, True)
    elif accion == "mostrar":
        t.set_hidden(topic_id, False)
    elif accion == "rechazar":
        t.set_status(topic_id, "rejected")
    elif accion == "hecho":
        t.set_status(topic_id, "done")
    elif accion == "reabrir":
        t.set_status(topic_id, "open")
    else:
        return PlainTextResponse("Acción no válida", status_code=400)
    return RedirectResponse(f"/temas/{topic_id}", status_code=303)


def install_topics(app: FastAPI, db: Storage) -> Topics:
    app.state.topics = Topics(db.conn)
    app.include_router(router)
    return app.state.topics
