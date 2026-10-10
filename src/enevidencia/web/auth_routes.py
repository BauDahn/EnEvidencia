"""Rutas de cuentas: /registro, /entrar, /salir, /cuenta, /normas; más middleware de cookies CSRF y cabeceras."""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time

from fastapi import APIRouter, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from ..accounts import Accounts, AccountError
from ..storage import Storage
from .deps import (CSRF_COOKIE, SESSION_COOKIE, AuthRedirect, cookie_flags, csrf_check, current_user, new_csrf,
                   safe_next)

router = APIRouter()
SESSION_MAX_AGE = 14 * 24 * 3600
CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data:; form-action 'self'; frame-ancestors 'none'; base-uri 'self'; object-src 'none'")


_FORM_KEY = os.environ.get("ENEVIDENCIA_SECRET", "").encode() or secrets.token_bytes(32)
DEFAULT_MIN_FORM_SECONDS = 2.0


def min_form_seconds() -> float:
    """Retraso mínimo entre mostrar y enviar el formulario de registro (0 lo desactiva; útil en tests)."""
    try:
        return float(os.environ.get("ENEVIDENCIA_MIN_FORM_SECONDS", DEFAULT_MIN_FORM_SECONDS))
    except ValueError:
        return DEFAULT_MIN_FORM_SECONDS


def form_stamp() -> str:
    ts = str(int(time.time()))
    return f"{ts}.{hmac.new(_FORM_KEY, ts.encode(), hashlib.sha256).hexdigest()[:32]}"


def stamp_age(stamp: str) -> float | None:
    """Segundos desde que se emitió el sello, o None si falta o no es auténtico."""
    ts, _, sig = (stamp or "").partition(".")
    good = hmac.new(_FORM_KEY, ts.encode(), hashlib.sha256).hexdigest()[:32]
    if not ts.isdigit() or not hmac.compare_digest(sig, good):
        return None
    return time.time() - int(ts)


def _render(request: Request, name: str, status: int = 200, **ctx):
    return request.app.state.templates.TemplateResponse(request, name, ctx, status_code=status)


def _ip(request: Request) -> str:
    return request.client.host if request.client else ""


def _login(request: Request, resp: RedirectResponse, user) -> RedirectResponse:
    acc: Accounts = request.app.state.accounts
    acc.logout(request.cookies.get(SESSION_COOKIE))  # nunca reutilizar una sesión previa
    resp.set_cookie(SESSION_COOKIE, acc.create_session(user), max_age=SESSION_MAX_AGE, **cookie_flags())
    return resp


@router.get("/registro", response_class=HTMLResponse)
def register_form(request: Request, next: str = "/"):
    if current_user(request):
        return RedirectResponse("/cuenta", status_code=303)
    return _render(request, "registro.html", form={}, next=safe_next(next), error=None, stamp=form_stamp())


@router.post("/registro")
def register(request: Request, username: str = Form(""), email: str = Form(""), password: str = Form(""),
             acepto: str = Form(""), next: str = Form("/"), website: str = Form(""), stamp: str = Form("")):
    form = {"username": username, "email": email}
    acc: Accounts = request.app.state.accounts
    ip = _ip(request)
    try:
        if website.strip():  # honeypot: un humano no ve este campo
            raise AccountError("No se pudo crear la cuenta.")
        minimum = min_form_seconds()
        if minimum > 0:
            age = stamp_age(stamp)
            if age is None or age < minimum:
                raise AccountError("Formulario enviado demasiado rápido o caducado. Espera un momento y vuelve a enviarlo.")
        if not acc.registration_allowed(ip):
            raise AccountError("Demasiados registros desde tu conexión. Inténtalo más tarde.")
        if not acepto:
            raise AccountError("Debes aceptar las normas y el aviso de privacidad para crear una cuenta.")
        user = acc.register(username, email, password)
        acc.record_registration(ip)
    except AccountError as e:
        return _render(request, "registro.html", 400, form=form, next=safe_next(next), error=str(e), stamp=form_stamp())
    return _login(request, RedirectResponse(safe_next(next), status_code=303), user)


@router.get("/entrar", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/"):
    if current_user(request):
        return RedirectResponse(safe_next(next), status_code=303)
    return _render(request, "entrar.html", form={}, next=safe_next(next), error=None)


@router.post("/entrar")
def login(request: Request, username: str = Form(""), password: str = Form(""), next: str = Form("/")):
    try:
        user = request.app.state.accounts.authenticate(username, password, _ip(request))
    except AccountError as e:
        return _render(request, "entrar.html", 400, form={"username": username}, next=safe_next(next), error=str(e))
    return _login(request, RedirectResponse(safe_next(next), status_code=303), user)


@router.post("/salir")
def logout(request: Request):
    request.app.state.accounts.logout(request.cookies.get(SESSION_COOKIE))
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


@router.get("/cuenta", response_class=HTMLResponse)
def account(request: Request):
    if not current_user(request):
        return RedirectResponse("/entrar?next=/cuenta", status_code=303)
    return _render(request, "cuenta.html")


@router.get("/normas", response_class=HTMLResponse)
def rules(request: Request):
    return _render(request, "normas.html")


def install_auth(app: FastAPI, db: Storage, templates) -> Accounts:
    """Registra cuentas, CSRF, cabeceras y rutas. Llamar justo después de crear la app y la base."""
    app.state.accounts = Accounts(db.conn)
    app.state.templates = templates

    def ctx(request: Request) -> dict:
        return {"user": current_user(request), "csrf_token": getattr(request.state, "csrf_token", "")}

    if not any(getattr(p, "_ee_auth", False) for p in templates.context_processors):
        ctx._ee_auth = True  # type: ignore[attr-defined]
        templates.context_processors.append(ctx)

    @app.exception_handler(AuthRedirect)
    async def _redirect(request: Request, exc: AuthRedirect):
        return RedirectResponse(exc.url, status_code=303)

    @app.middleware("http")
    async def security(request: Request, call_next):
        token = request.cookies.get(CSRF_COOKIE)
        fresh = not token or len(token) > 100
        request.state.csrf_token = token = new_csrf() if fresh else token
        response = await call_next(request)
        if fresh:
            response.set_cookie(CSRF_COOKIE, token, **cookie_flags())
        h = response.headers
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("X-Frame-Options", "DENY")
        h.setdefault("Referrer-Policy", "same-origin")
        h.setdefault("Content-Security-Policy", CSP)
        if request.url.path in {"/entrar", "/registro", "/cuenta"}:
            h.setdefault("Cache-Control", "no-store")
        return response

    app.include_router(router)
    return app.state.accounts
