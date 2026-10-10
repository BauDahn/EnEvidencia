"""Descubrimiento de fuentes: consultas neutras -> URLs -> descarga -> fuentes PROPUESTAS.

No guarda nada en base de datos: el usuario revisa y acepta las fuentes después.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..ingestion import fetch_source
from ..llm import LLMClient
from ..models import Source, Status

MIN_TEXT_CHARS = 300
MAX_PER_DOMAIN = 2

PROMPT_QUERIES = """Eres un documentalista que prepara una búsqueda de fuentes sobre un caso real y polémico.
Tema: {topic}

Genera {n} consultas de búsqueda en español, NEUTRALES y variadas.
Reglas obligatorias de neutralidad:
- Está PROHIBIDO usar adjetivos valorativos, calificativos o términos cargados (por ejemplo "escándalo",
  "polémico", "injusto", "farsa", "víctima", "culpable"). Usa solo términos descriptivos y verificables.
- No presupongas la tesis de ninguna de las partes ni formules la consulta como pregunta retórica.
- Cubre las distintas partes implicadas por igual.
Cubre estos tipos de documento (al menos una consulta de cada tipo):
1. noticias de prensa sobre los hechos;
2. comunicados oficiales de las instituciones implicadas;
3. sentencias, autos y resoluciones judiciales;
4. entrevistas y declaraciones de las distintas partes.

Responde SOLO con una lista JSON de cadenas, por ejemplo: ["consulta 1", "consulta 2"]."""

_TRACKING = ("utm_", "fbclid", "gclid", "mc_", "ref_src")


@dataclass
class DiscoveryResult:
    sources: list[Source] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)
    # Solo en modo sin filtros (filtrar=False): (url, motivo) de lo encontrado pero no descargado/duplicado.
    excluded: list[tuple[str, str]] = field(default_factory=list)


def generate_queries(topic: str, llm: LLMClient, n: int = 5) -> list[str]:
    """Consultas neutras generadas por el LLM; el tema literal va siempre primero."""
    topic = topic.strip()
    result = [topic] if topic else []
    seen = {topic.lower()}
    try:
        raw = llm.generate_json(PROMPT_QUERIES.format(topic=topic, n=n))
    except Exception:
        raw = []
    if isinstance(raw, dict):  # tolera {"consultas": [...]}
        raw = next((v for v in raw.values() if isinstance(v, list)), [])
    if not isinstance(raw, list):
        raw = []
    for q in raw:
        if not isinstance(q, str):
            continue
        q = " ".join(q.split())
        if not q or q.lower() in seen:
            continue
        seen.add(q.lower())
        result.append(q)
        if len(result) >= n:
            break
    return result[:n] if n > 0 else []


def _default_searcher(query: str, max_results: int) -> list[dict]:
    from ddgs import DDGS

    return list(DDGS().text(query, region="es-es", max_results=max_results) or [])


def search_urls(query: str, max_results: int = 8,
                searcher: Callable[[str, int], list[dict]] | None = None) -> list[dict]:
    """Devuelve [{'url','title','snippet'}]. Ante cualquier error de red/ddgs devuelve []."""
    try:
        raw = (searcher or _default_searcher)(query, max_results)
    except Exception:
        return []
    out = []
    for r in raw or []:
        if not isinstance(r, dict):
            continue
        url = r.get("href") or r.get("url") or ""
        if not url.startswith(("http://", "https://")):
            continue
        out.append({"url": url, "title": r.get("title", "") or "", "snippet": r.get("body", r.get("snippet", "")) or ""})
    return out[:max_results]


def normalize_url(url: str) -> str:
    """Sin fragmento ni parámetros de seguimiento, host en minúsculas, sin barra final."""
    p = urlsplit(url.strip())
    query = urlencode([(k, v) for k, v in parse_qsl(p.query) if not k.lower().startswith(_TRACKING)])
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/") or "/", query, ""))


def _domain(url: str) -> str:
    host = urlsplit(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def discover(topic: str, llm: LLMClient, *, searcher=None, fetcher=None, n_queries: int = 5, per_query: int = 8,
             max_sources: int = 12, filtrar: bool = True,
             max_candidates: int = 30, on_progress: Callable[[str], None] | None = None) -> DiscoveryResult:
    """Descubre fuentes candidatas (status=PROPOSED) para un tema. No persiste nada.

    filtrar=False: devuelve TODO lo descargado (sin mínimo de texto, tope por dominio ni máximo); los filtros
    objetivos los aplica entonces `enevidencia.pipeline`, que registra el motivo de cada exclusión.
    """
    fetcher = fetcher or fetch_source
    note = on_progress or (lambda msg: None)
    res = DiscoveryResult()

    res.queries = generate_queries(topic, llm, n_queries)
    note(f"{len(res.queries)} consultas generadas")

    seen: set[str] = set()
    per_domain: dict[str, int] = {}
    candidates: list[str] = []
    for q in res.queries:
        found = search_urls(q, per_query, searcher)
        note(f"Consulta «{q}»: {len(found)} resultados")
        for r in found:
            key = normalize_url(r["url"])
            dom = _domain(key)
            if not filtrar:
                if key in seen:
                    if r["url"] in candidates or any(r["url"] == u for u, _ in res.excluded):   # misma URL devuelta por otra consulta: no es otra fuente
                        continue
                    res.excluded.append((r["url"], "URL duplicada (misma URL normalizada que otra ya encontrada)"))
                    continue
                if len(candidates) >= max_candidates:
                    res.excluded.append((r["url"], f"Fuera del límite de {max_candidates} candidatas a descargar"))
                    continue
                seen.add(key)
                candidates.append(r["url"])
                continue
            if key in seen or per_domain.get(dom, 0) >= MAX_PER_DOMAIN:
                continue
            seen.add(key)
            per_domain[dom] = per_domain.get(dom, 0) + 1
            candidates.append(r["url"])

    for url in candidates:
        if filtrar and len(res.sources) >= max_sources:
            break
        note(f"Descargando {url}")
        try:
            src = fetcher(url)
        except Exception as e:  # noqa: BLE001 - cualquier fallo de descarga se anota y se sigue
            res.errors.append(f"{url}: no se pudo descargar ({e})")
            res.excluded.append((url, f"No se pudo descargar ({e})"))
            continue
        if not filtrar:
            src.status = Status.PROPOSED
            res.sources.append(src)
            continue
        if not src.text or len(src.text.strip()) < MIN_TEXT_CHARS:
            res.errors.append(f"{url}: texto vacío o demasiado corto (<{MIN_TEXT_CHARS} caracteres)")
            continue
        src.status = Status.PROPOSED
        res.sources.append(src)
    note(f"{len(res.sources)} fuentes propuestas, {len(res.errors)} descartadas")
    return res
