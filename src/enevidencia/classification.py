"""Clasificación descriptiva de fuentes según reglas editables (config/classification.yaml)."""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

import yaml

from .models import SourceType

DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "config" / "classification.yaml"


def load_rules(path: str | Path | None = None) -> dict:
    with open(path or DEFAULT_CONFIG, encoding="utf-8") as f:
        return yaml.safe_load(f)


def classify(url: str, kind: str = "", rules: dict | None = None) -> tuple[SourceType, str]:
    """Devuelve (tipo, motivo). El motivo explica qué regla se aplicó."""
    rules = rules or load_rules()
    host = (urlparse(url).hostname or "").lower()
    kind = kind.strip().lower().replace(" ", "_")
    for rule in rules.get("rules", []):
        domains = [d.lstrip(".") for d in rule.get("domain_suffixes", [])]
        if host and any(host == d or host.endswith("." + d) for d in domains):
            return SourceType(rule["type"]), rule["reason"]
        if kind and kind in rule.get("kind_hints", []):
            return SourceType(rule["type"]), rule["reason"]
    return SourceType(rules.get("default", "secondary")), "Sin regla aplicable: valor por defecto"
