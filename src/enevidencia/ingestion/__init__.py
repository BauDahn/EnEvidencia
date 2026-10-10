"""Ingesta de fuentes completas con su procedencia."""
from __future__ import annotations

from datetime import date

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


def fetch_source(url: str, *, kind: str = "", client: httpx.Client | None = None,
                 rules: dict | None = None) -> Source:
    """Descarga una URL y extrae texto y metadatos (título, autor, fecha, medio) cuando existen."""
    import trafilatura

    own = client is None
    client = client or httpx.Client(follow_redirects=True, timeout=20)
    try:
        resp = client.get(url)
        resp.raise_for_status()
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
