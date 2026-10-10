"""Middleware ASGI que limita el tamaño de las peticiones (413) ANTES de cualquier dependencia (CSRF incluido)."""
from __future__ import annotations

MAX_BODY = 1024 * 1024            # 1 MB en general
MAX_FORM = 64 * 1024              # 64 KB para formularios normales (urlencoded / multipart)
MAX_SOURCE_FORM = 2 * 1024 * 1024  # 2 MB solo en /sources (texto pegado de una fuente)
BODY_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def limit_for(path: str, content_type: str) -> int:
    if path.rstrip("/") == "/sources":
        return MAX_SOURCE_FORM
    if content_type.lower().startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
        return MAX_FORM
    return MAX_BODY


class BodyLimitMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or scope["method"] not in BODY_METHODS:
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        limit = limit_for(scope["path"], headers.get("content-type", ""))
        declared = headers.get("content-length", "")
        if declared.isdigit() and int(declared) > limit:
            return await self._reject(send)
        received = 0
        exceeded = rejected = started = False

        async def counting_receive():
            nonlocal received, exceeded
            if exceeded:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:  # sin Content-Length (o mentido): se corta el flujo
                    exceeded = True
                    return {"type": "http.disconnect"}
            return message

        async def tracking_send(message):
            nonlocal rejected, started
            if exceeded and not started:
                # FastAPI/Starlette convierten un fallo de lectura en 400: se sustituye por 413
                if not rejected:
                    rejected = True
                    await self._reject(send)
                return
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        await self.app(scope, counting_receive, tracking_send)

    @staticmethod
    async def _reject(send) -> None:
        body = "Petición demasiado grande.".encode()
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                                (b"content-length", str(len(body)).encode()), (b"connection", b"close")]})
        await send({"type": "http.response.body", "body": body})
