"""Filtros OBJETIVOS de aceptación automática de fuentes (deterministas, sin LLM).

Ningún filtro mide fiabilidad, postura u orientación de una fuente: solo comprueban longitud, relación
con el tema, duplicados y límites de cantidad. Los valores se editan en config/pipeline.yaml.
"""
from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from .discovery import normalize_url
from .models import Source

DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "config" / "pipeline.yaml"

DEFAULTS = {
    "min_caracteres": 300,
    "min_terminos_tema": 1,
    "terminos_genericos": ["españa", "español", "española", "madrid", "caso", "tema", "hecho", "noticia"],
    "max_por_dominio": 2,
    "max_total": 12,
    "umbral_duplicado": 0.85,
    "max_candidatas": 30,
}

STOPWORDS = set("""a al algo ante con contra cual cuando de del desde donde durante e el ella ellas ello ellos en entre
es esa esas ese eso esos esta estas este esto estos fue ha han hasta la las le les lo los me mi mis mas más muy ni no
nos o para pero por porque que quien se sea según sin sobre son su sus te tu tus un una unas uno unos y ya the of and""".split())


@dataclass
class Decision:
    source: Source
    accepted: bool
    reason: str


@dataclass
class TopicTerms:
    terms: list[str] = field(default_factory=list)    # términos exigidos (sin acentos, minúsculas)
    kind: str = ""                                    # "nombres propios" | "términos distintivos" | ""


def load_config(path: str | Path | None = None) -> dict:
    """Configuración con valores por defecto; `path` o ENEVIDENCIA_PIPELINE o config/pipeline.yaml."""
    cfg = dict(DEFAULTS)
    p = Path(path or os.environ.get("ENEVIDENCIA_PIPELINE") or DEFAULT_CONFIG)
    if p.exists():
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        cfg.update((data.get("fuentes") or {}))
    return cfg


def _plain(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def topic_terms(topic: str, generic: list[str] | None = None) -> TopicTerms:
    """Términos distintivos del tema: sin palabras vacías ni genéricas. Prefiere nombres propios."""
    generic_plain = {_plain(g) for g in (generic or [])}
    words = re.findall(r"[\wáéíóúüñÁÉÍÓÚÜÑ]+", topic)
    distinctive, proper = [], []
    for i, w in enumerate(words):
        p = _plain(w)
        if len(p) < 3 or w.lower() in STOPWORDS or p in STOPWORDS or p in generic_plain:
            continue
        if p not in distinctive:
            distinctive.append(p)
        if i > 0 and w[0].isupper() and p not in proper:
            proper.append(p)
    if proper:
        return TopicTerms(proper, "nombres propios")
    if distinctive:
        return TopicTerms(distinctive, "términos distintivos")
    return TopicTerms()


def _term_in(term: str, plain_text: str) -> bool:
    return re.search(r"\b" + re.escape(term), plain_text) is not None


def domain_of(url: str) -> str:
    host = urlsplit(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def _shingles(text: str, n: int = 5) -> set[int]:
    words = re.findall(r"\w+", _plain(text))
    if len(words) < n:
        return {hash(" ".join(words))} if words else set()
    return {hash(" ".join(words[i:i + n])) for i in range(len(words) - n + 1)}


def similarity(a: set[int], b: set[int]) -> float:
    """Fracción del texto más corto que está contenida en el otro (detecta copias y reediciones)."""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def filter_sources(sources: list[Source], topic: str, cfg: dict | None = None) -> tuple[list[Decision], TopicTerms]:
    """Aplica los filtros en orden fijo y devuelve una decisión (con motivo legible) por fuente."""
    cfg = {**DEFAULTS, **(cfg or {})}
    tt = topic_terms(topic, cfg["terminos_genericos"])
    need = min(int(cfg["min_terminos_tema"]), len(tt.terms))
    decisions: list[Decision] = []
    seen_urls: set[str] = set()
    kept_shingles: list[tuple[set[int], str]] = []
    per_domain: dict[str, int] = {}
    total = 0
    for s in sources:
        def out(ok: bool, why: str) -> None:
            decisions.append(Decision(s, ok, why))

        text = (s.text or "").strip()
        if len(text) < int(cfg["min_caracteres"]):
            out(False, f"Texto demasiado corto ({len(text)} caracteres; mínimo {cfg['min_caracteres']})")
            continue
        if need:
            plain = _plain(text)
            found = [t for t in tt.terms if _term_in(t, plain)]
            if len(found) < need:
                out(False, f"No menciona los términos del tema ({tt.kind}: {', '.join(tt.terms)}); "
                           f"aparecen {len(found)} de los {need} exigidos")
                continue
        key = normalize_url(s.url)
        if key in seen_urls:
            out(False, "URL duplicada")
            continue
        sh = _shingles(text)
        dup = next((u for sset, u in kept_shingles if similarity(sh, sset) >= float(cfg["umbral_duplicado"])), None)
        if dup:
            out(False, f"Texto casi idéntico a otra fuente ya aceptada ({dup})")
            continue
        dom = domain_of(s.url)
        if per_domain.get(dom, 0) >= int(cfg["max_por_dominio"]):
            out(False, f"Ya hay {cfg['max_por_dominio']} fuentes aceptadas del dominio {dom} (máximo por dominio)")
            continue
        if total >= int(cfg["max_total"]):
            out(False, f"Superado el máximo de {cfg['max_total']} fuentes aceptadas")
            continue
        seen_urls.add(key)
        kept_shingles.append((sh, s.url))
        per_domain[dom] = per_domain.get(dom, 0) + 1
        total += 1
        out(True, "Cumple los filtros objetivos (longitud, términos del tema, sin duplicados, límites)")
    return decisions, tt
