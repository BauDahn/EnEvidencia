"""Ingesta de fuentes completas con su procedencia."""
from __future__ import annotations

import io
import ipaddress
import socket
from datetime import date
from urllib.parse import urljoin, urlsplit

import httpx

from ..classification import classify
from ..models import Source


def source_from_text(url: str, text: str, *, outlet: str = "", author: str = "", published: date | None = None,
                     title: str = "", kind: str = "", rules: dict | None = None) -> Source:
    """Crea una fuente desde texto ya disponible (pegado a mano o descargado)."""
    if not text.strip():
        raise ValueError("El texto original de la fuente no puede estar vacío.")
    if not url.strip():
        raise ValueError("Toda fuente necesita URL (o identificador de procedencia).")
    stype, reason = classify(url, kind, rules)
    return Source(url=url.strip(), text=text, source_type=stype, outlet=outlet, author=author,
                  published=published, title=title, kind=kind, classification_reason=reason)


MAX_BYTES = 20 * 1024 * 1024
MAX_REDIRECTS = 5


def _resolve(host: str) -> list[str]:
    """Direcciones IP de un host (aislado para poder sustituirlo en tests)."""
    try:
        return [i[4][0] for i in socket.getaddrinfo(host, None)]
    except socket.gaierror:
        return []  # no resuelve: la descarga fallará por sí sola


def validate_url(url: str) -> str:
    """Solo http/https hacia hosts públicos (bloqueo básico de SSRF). Lanza ValueError si no es válida."""
    p = urlsplit((url or "").strip())
    if p.scheme not in ("http", "https"):
        raise ValueError("Solo se admiten URLs http:// o https://.")
    host = (p.hostname or "").lower().rstrip(".")
    if not host:
        raise ValueError("La URL no tiene host.")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise ValueError("No se permiten direcciones locales o privadas.")
    try:
        addrs = [ipaddress.ip_address(host)]
    except ValueError:
        addrs = []
        for a in _resolve(host):
            try:
                addrs.append(ipaddress.ip_address(a.split("%")[0]))
            except ValueError:
                pass
    for ip in addrs:
        if not ip.is_global:
            raise ValueError("No se permiten direcciones locales o privadas.")
    return url.strip()


def _pdf_to_text(data: bytes) -> tuple[str, str, str]:
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data))
        text = "\n\n".join((page.extract_text() or "").strip() for page in reader.pages).strip()
        meta = reader.metadata
        title, author = (meta.title or "", meta.author or "") if meta else ("", "")
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"No se pudo leer el PDF: {e}") from e
    if not text:
        raise ValueError("El PDF no contiene texto extraíble (¿es un documento escaneado?). "
                         "Pega el texto a mano o usa una versión con texto.")
    return text, title, author


def fetch_source(url: str, *, kind: str = "", client: httpx.Client | None = None,
                 rules: dict | None = None) -> Source:
    """Descarga una URL y extrae texto y metadatos (título, autor, fecha, medio) cuando existen."""
    import trafilatura

    own = client is None
    client = client or httpx.Client(timeout=20)
    try:
        current = validate_url(url)
        for _ in range(MAX_REDIRECTS + 1):  # redirecciones manuales: cada salto se valida
            resp = client.get(current, follow_redirects=False)
            if resp.is_redirect and resp.headers.get("location"):
                current = validate_url(urljoin(current, resp.headers["location"]))
                continue
            break
        else:
            raise ValueError("Demasiadas redirecciones.")
        resp.raise_for_status()
        if len(resp.content) > MAX_BYTES:
            raise ValueError("El documento es demasiado grande (más de 20 MB).")
        ctype = resp.headers.get("content-type", "").lower()
        if "application/pdf" in ctype or urlsplit(current).path.lower().endswith(".pdf"):
            text, title, author = _pdf_to_text(resp.content)
            host = urlsplit(current).hostname or ""
            return source_from_text(url, text, kind=kind, rules=rules, outlet=host, author=author, title=title)
        html = resp.text
    finally:
        if own:
            client.close()
    text = trafilatura.extract(html, include_comments=False) or ""
    meta = trafilatura.extract_metadata(html)
    published = None
    if meta and meta.date:
        try:
            published = date.fromisoformat(meta.date[:10])
        except ValueError:
            pass
    return source_from_text(
        url, text, kind=kind, rules=rules, published=published,
        outlet=(meta.sitename or meta.hostname or "") if meta else "",
        author=(meta.author or "") if meta else "", title=(meta.title or "") if meta else "",
    )
