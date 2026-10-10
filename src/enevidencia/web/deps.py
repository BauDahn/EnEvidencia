"""Dependencias de autenticación y CSRF reutilizables.

Contrato para otras rutas:
    from enevidencia.web.deps import current_user, require_user, require_admin
    @router.post("/algo", dependencies=[Depends(require_admin)])           # solo admin
    def ver(user = Depends(require_user)): ...                               # redirige a /entrar?next=...
En plantillas están siempre disponibles `user` (User | None; .username, .is_admin) y `csrf_token`.
Todo POST del sitio debe llevar `<input type="hidden" name="csrf_token" value="{{ csrf_token }}">`.
"""
from __future__ import annotations

import hmac
import os
import secrets
from urllib.parse import quote

from fastapi import HTTPException, Request

from ..accounts import User

SESSION_COOKIE = "ee_session"
CSRF_COOKIE = "ee_csrf"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class AuthRedirect(Exception):
    def __init__(self, url: str) -> None:
        self.url = url


def secure_cookies() -> bool:
    return os.environ.get("ENEVIDENCIA_SECURE_COOKIES", "") in {"1", "true", "yes"}


def cookie_flags() -> dict:
    return {"httponly": True, "samesite": "lax", "secure": secure_cookies(), "path": "/"}


def safe_next(path: str | None, default: str = "/") -> str:
    """Solo rutas internas (evita open redirect, también con barras invertidas o caracteres de control)."""
    p = path or ""
    if (p.startswith("/") and not p.startswith("//") and "\\" not in p and not any(ord(c) < 32 for c in p)):
        return p
    return default


def current_user(request: Request) -> User | None:
    if not hasattr(request.state, "user"):
        token = request.cookies.get(SESSION_COOKIE)
        request.state.user = request.app.state.accounts.user_from_token(token)
    return request.state.user


def _login_url(request: Request) -> str:
    target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    return "/entrar?next=" + quote(target, safe="/") if request.method == "GET" else "/entrar"


def require_user(request: Request) -> User:
    user = current_user(request)
    if user is None:
        raise AuthRedirect(_login_url(request))
    return user


def require_admin(request: Request) -> User:
    user = current_user(request)
    if user is None and request.method == "GET":
        raise AuthRedirect(_login_url(request))
    if user is None or not user.is_admin:
        raise HTTPException(status_code=403, detail="Solo el administrador puede hacer esto. Entra con una cuenta de administrador.")
    return user


def new_csrf() -> str:
    return secrets.token_urlsafe(32)


async def csrf_check(request: Request) -> None:
    """Doble cookie: el token del formulario (o cabecera X-CSRF-Token) debe coincidir con la cookie."""
    if request.method in SAFE_METHODS:
        return
    cookie = request.cookies.get(CSRF_COOKIE, "")
    sent = request.headers.get("x-csrf-token", "")
    if not sent:
        ctype = request.headers.get("content-type", "")
        if ctype.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
            sent = str((await request.form()).get("csrf_token", ""))
    if not cookie or not sent or not hmac.compare_digest(cookie.encode(), sent.encode()):
        raise HTTPException(status_code=403, detail="Formulario caducado o no válido (CSRF). Recarga la página e inténtalo de nuevo.")
