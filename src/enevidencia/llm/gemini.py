"""Cliente Gemini (SDK google-genai) con reintentos, ritmo mínimo entre llamadas y caché en disco."""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Callable

from . import LLMError

MODELO_POR_DEFECTO = "gemini-3.8-flash"
_CODIGOS_REINTENTABLES = {429, 500, 503, 504}
_MARCAS_REINTENTABLES = ("429", "503", "resource_exhausted", "unavailable", "quota", "rate limit", "overloaded")


def _es_reintentable(exc: Exception) -> bool:
    codigo = getattr(exc, "code", None)
    if isinstance(codigo, int) and codigo in _CODIGOS_REINTENTABLES:
        return True
    texto = str(exc).lower()
    return any(m in texto for m in _MARCAS_REINTENTABLES)


class GeminiClient:
    def __init__(self, api_key: str, model: str | None = None, cache_dir: str | Path = "data/llm_cache",
                 min_interval: float = 4.0, max_retries: int = 4,
                 sleep: Callable[[float], None] = time.sleep, client: Any = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.model = model or os.environ.get("GEMINI_MODEL") or MODELO_POR_DEFECTO
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.min_interval = min_interval
        self.max_retries = max_retries
        self._sleep = sleep
        self._clock = clock
        self._ultima: float | None = None
        if client is None:
            try:
                from google import genai
            except ImportError as e:  # pragma: no cover
                raise LLMError("Falta el paquete google-genai (pip install google-genai)") from e
            client = genai.Client(api_key=api_key)
        self._client = client

    def __repr__(self) -> str:  # nunca exponer la clave
        return f"GeminiClient(model={self.model!r})"

    # --- caché ---
    def _clave(self, tipo: str, prompt: str, schema: dict | None) -> str:
        bruto = json.dumps([self.model, tipo, prompt, schema], sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(bruto.encode("utf-8")).hexdigest()

    def _leer_cache(self, clave: str) -> Any | None:
        if not self.cache_dir:
            return None
        f = self.cache_dir / f"{clave}.json"
        if f.exists():
            try:
                return json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None
        return None

    def _escribir_cache(self, clave: str, valor: Any) -> None:
        if not self.cache_dir:
            return
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            (self.cache_dir / f"{clave}.json").write_text(
                json.dumps({"valor": valor}, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass  # la caché es opcional

    # --- llamada al SDK ---
    def _esperar_turno(self) -> None:
        if self._ultima is not None:
            falta = self.min_interval - (self._clock() - self._ultima)
            if falta > 0:
                self._sleep(falta)

    def _llamar(self, prompt: str, config: Any) -> str:
        for intento in range(self.max_retries + 1):
            self._esperar_turno()
            try:
                resp = self._client.models.generate_content(model=self.model, contents=prompt, config=config)
                self._ultima = self._clock()
                texto = getattr(resp, "text", None)
                if not texto:
                    raise LLMError("Gemini devolvió una respuesta vacía")
                return texto
            except LLMError:
                raise
            except Exception as e:  # noqa: BLE001 - errores del SDK varían
                self._ultima = self._clock()
                if not _es_reintentable(e):
                    raise LLMError(f"Error de Gemini: {type(e).__name__}: {e}") from e
                if intento >= self.max_retries:
                    raise LLMError(
                        f"Gemini sigue sin responder tras {self.max_retries + 1} intentos "
                        f"(cuota o servicio saturado): {e}") from e
                self._sleep(2.0 * (2 ** intento))
        raise LLMError("Reintentos agotados")  # pragma: no cover

    def _config(self, json_mode: bool, schema: dict | None) -> Any:
        from google.genai import types

        if not json_mode:
            return types.GenerateContentConfig()
        kwargs: dict[str, Any] = {"response_mime_type": "application/json"}
        if schema is not None:
            kwargs["response_schema"] = schema
        return types.GenerateContentConfig(**kwargs)

    # --- API pública ---
    def generate_json(self, prompt: str, schema: dict | None = None) -> Any:
        clave = self._clave("json", prompt, schema)
        c = self._leer_cache(clave)
        if c is not None:
            return c["valor"]
        texto = self._llamar(prompt, self._config(True, schema))
        try:
            valor = json.loads(texto)
        except ValueError as e:
            raise LLMError(f"Gemini no devolvió JSON válido: {texto[:200]!r}") from e
        self._escribir_cache(clave, valor)
        return valor

    def generate_text(self, prompt: str) -> str:
        clave = self._clave("text", prompt, None)
        c = self._leer_cache(clave)
        if c is not None:
            return c["valor"]
        texto = self._llamar(prompt, self._config(False, None))
        self._escribir_cache(clave, texto)
        return texto
