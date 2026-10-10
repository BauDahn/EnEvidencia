"""Ingesta de fuentes completas con su procedencia."""
from __future__ import annotations

import io
import ipaddress
import os
import socket
import time
import zlib
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
    text, cut = truncate_text(text)
    if cut:
        reason = f"{reason} [texto truncado a {len(text)} caracteres]".strip()
    return Source(url=url.strip(), text=text, source_type=stype, outlet=outlet, author=author,
                  published=published, title=title, kind=kind, classification_reason=reason)


MAX_BYTES = 20 * 1024 * 1024
MAX_REDIRECTS = 5
FETCH_DEADLINE = 30.0          # segundos totales por descarga (todas las redirecciones incluidas)
MAX_PDF_PAGES = 150
MAX_SOURCE_CHARS = 120_000     # tope de texto por fuente (config/pipeline.yaml: max_caracteres_fuente)
ALLOWED_PORTS = {80, 443}      # ampliable con ENEVIDENCIA_EXTRA_PORTS="8080,8443"
ALLOWED_ENCODINGS = {"", "identity", "gzip", "x-gzip", "deflate"}
_NAT64 = ipaddress.ip_network("64:ff9b::/96")


def allowed_ports() -> set[int]:
    extra = {int(x) for x in os.environ.get("ENEVIDENCIA_EXTRA_PORTS", "").replace(" ", "").split(",") if x.isdigit()}
    return ALLOWED_PORTS | extra


def truncate_text(text: str, limit: int | None = None) -> tuple[str, bool]:
    limit = limit or MAX_SOURCE_CHARS
    return (text[:limit], True) if len(text) > limit else (text, False)


def _resolve(host: str) -> list[str]:
    """Direcciones IP de un host (aislado para poder sustituirlo en tests)."""
    try:
        return [i[4][0] for i in socket.getaddrinfo(host, None)]
    except (socket.gaierror, UnicodeError):
        return []


def _is_public(ip) -> bool:
    if ip.version == 6:
        if ip in _NAT64 or ip.ipv4_mapped is not None or ip.sixtofour is not None or ip.teredo is not None:
            return False  # NAT64 / mapeadas / 6to4 / Teredo pueden esconder IPv4 internas
    return ip.is_global


def resolve_public(host: str) -> list[str]:
    """Resuelve UNA vez y exige que TODAS las IPs sean públicas. Si no resuelve, falla cerrado."""
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not _is_public(literal):
            _blocked()
        return [str(literal)]
    ips = []
    for a in _resolve(host):
        try:
            ip = ipaddress.ip_address(a.split("%")[0])
        except ValueError:
            continue
        if not _is_public(ip):
            _blocked()
        ips.append(str(ip))
    if not ips:
        raise ValueError("No se pudo resolver el host de la URL.")
    return ips


def _blocked():
    raise ValueError("No se permiten direcciones locales o privadas.")


def validate_url(url: str) -> str:
    """Solo http/https, puertos 80/443 y hosts públicos (SSRF). Lanza ValueError si no es válida."""
    p = urlsplit((url or "").strip())
    if p.scheme not in ("http", "https"):
        raise ValueError("Solo se admiten URLs http:// o https://.")
    host = (p.hostname or "").lower().rstrip(".")
    if not host:
        raise ValueError("La URL no tiene host.")
    try:
        port = p.port
    except ValueError:
        raise ValueError("Puerto de la URL no válido.") from None
    if port is not None and port not in allowed_ports():
        raise ValueError("Puerto no permitido (solo 80 y 443).")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        _blocked()
    resolve_public(host)
    return url.strip()


class PinnedTransport(httpx.BaseTransport):
    """Resuelve el host UNA vez por petición, valida que sea público y conecta a esa IP (evita DNS rebinding).
    Se conserva la cabecera Host y el SNI/verificación del certificado con el nombre original."""

    def __init__(self, inner: httpx.BaseTransport | None = None) -> None:
        self.inner = inner or httpx.HTTPTransport()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        ip = resolve_public(host)[0]
        ext = dict(request.extensions)
        if request.url.scheme == "https":
            ext["sni_hostname"] = host
        headers = request.headers.copy()
        headers["Host"] = request.url.netloc.decode("ascii")
        pinned = httpx.Request(request.method, request.url.copy_with(host=ip), headers=headers,
                               stream=request.stream, extensions=ext)
        return self.inner.handle_request(pinned)

    def close(self) -> None:
        self.inner.close()


def _decoder(encoding: str):
    enc = encoding.strip().lower()
    if enc in ("", "identity"):
        return None
    if enc not in ALLOWED_ENCODINGS:
        raise ValueError(f"Codificación de contenido no admitida: {encoding[:30]}")
    return zlib.decompressobj(47 if enc != "deflate" else 15)  # 47: gzip o zlib automático


def _read_body(resp: httpx.Response, deadline: float) -> bytes:
    """Lee el cuerpo en streaming con tope de bytes (comprimidos y descomprimidos) y plazo total."""
    declared = resp.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BYTES:
        raise ValueError("El documento es demasiado grande (más de 20 MB).")
    codings = [c for c in resp.headers.get("content-encoding", "").split(",") if c.strip()]
    if len(codings) > 1:
        raise ValueError("Codificación de contenido múltiple no admitida.")
    consumed = resp.is_stream_consumed  # respuesta ya en memoria (transportes simulados): httpx ya la decodificó
    dec = None if consumed else _decoder(codings[0] if codings else "")
    raw = out = 0
    parts: list[bytes] = []
    for chunk in ([resp.content] if consumed else resp.iter_raw()):
        if time.monotonic() > deadline:
            raise ValueError("La descarga superó el tiempo máximo (30 s).")
        raw += len(chunk)
        if raw > MAX_BYTES:
            raise ValueError("El documento es demasiado grande (más de 20 MB).")
        if dec is None:
            parts.append(chunk)
            continue
        try:
            data = dec.decompress(chunk, MAX_BYTES + 1 - out)
        except zlib.error as e:
            raise ValueError(f"Cuerpo comprimido no válido: {e}") from e
        out += len(data)
        if out > MAX_BYTES:
            raise ValueError("El documento descomprimido es demasiado grande (más de 20 MB).")
        parts.append(data)
    return b"".join(parts)


def _pdf_to_text(data: bytes) -> tuple[str, str, str]:
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data))
        if len(reader.pages) > MAX_PDF_PAGES:
            raise ValueError(f"El PDF tiene demasiadas páginas ({len(reader.pages)}; máximo {MAX_PDF_PAGES}).")
        text = "\n\n".join((page.extract_text() or "").strip() for page in reader.pages).strip()
        meta = reader.metadata
        title, author = (meta.title or "", meta.author or "") if meta else ("", "")
    except ValueError:
        raise
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
    client = client or httpx.Client(timeout=20, transport=PinnedTransport(), headers={"Accept-Encoding": "gzip, deflate"})
    deadline = time.monotonic() + FETCH_DEADLINE
    try:
        current = validate_url(url)
        for _ in range(MAX_REDIRECTS + 1):  # redirecciones manuales: cada salto se valida
            with client.stream("GET", current, follow_redirects=False) as resp:
                if resp.is_redirect and resp.headers.get("location"):
                    current = validate_url(urljoin(current, resp.headers["location"]))
                    continue
                resp.raise_for_status()
                body = _read_body(resp, deadline)
                ctype = resp.headers.get("content-type", "").lower()
                break
        else:
            raise ValueError("Demasiadas redirecciones.")
        if "application/pdf" in ctype or urlsplit(current).path.lower().endswith(".pdf"):
            text, title, author = _pdf_to_text(body)
            host = urlsplit(current).hostname or ""
            return source_from_text(url, text, kind=kind, rules=rules, outlet=host, author=author, title=title)
        html = httpx.Response(200, headers={"content-type": ctype}, content=body).text
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
