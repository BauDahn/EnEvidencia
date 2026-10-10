# EnEvidencia

Portal local de código abierto para recopilar afirmaciones de distintas fuentes sobre un mismo hecho, guardarlas con su procedencia y verlas comparadas. **No decide cuál es la verdad.**

## Instalación
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .
```

## Uso
```bash
enevidencia          # abre el portal en http://127.0.0.1:8000
```
1. Crea un **hecho** (p. ej. "Renta exigida").
2. Añade **fuentes** (URL, y texto original pegado o descargado). Se clasifican según `config/classification.yaml`.
3. En cada fuente, registra **afirmaciones**: quién la dijo, cita textual (se verifica contra el texto) y un valor comparable opcional.
4. Mira la vista comparada del hecho y descarga el **informe** (.md / .html) con citas a cada fuente.

La base de datos se guarda en `data/enevidencia.db` (cambia la ruta con `ENEVIDENCIA_DB`).

## Estructura
| Módulo | Función |
|---|---|
| `ingestion` | Descarga/recibe fuentes completas con URL, fecha, medio, autor |
| `extraction` | Crea afirmaciones verificando que la cita está en la fuente |
| `comparison` | Agrupa por hecho y valor: coinciden / se contradicen |
| `reporting` | Informe con notas a cada fuente |
| `web` | Interfaz FastAPI + Jinja2 |

## Tests
```bash
pytest
```

Consulta [docs/PRINCIPIOS.md](docs/PRINCIPIOS.md). Licencia MIT.
