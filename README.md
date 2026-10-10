# EnEvidencia

> “The truth is rarely pure and never simple.” — Oscar Wilde

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
### Modo asistido por IA ("Investigar tema")
Escribe un tema en la portada. Un único trabajo en segundo plano (con página de progreso, etapas y errores visibles) hace todo **sin que aceptes ni descartes nada** (así no se introduce tu sesgo): 1) la IA genera consultas neutras y se descargan fuentes (HTML y PDF con texto); 2) el código filtra con criterios objetivos y deterministas de `config/pipeline.yaml` (longitud mínima, términos distintivos del tema, sin duplicados, máximo por dominio y total; ninguno mide fiabilidad ni postura) y acepta o excluye cada fuente con un motivo legible; 3) la IA extrae afirmaciones de las aceptadas, una a una respetando la cuota, y el código guarda solo las que tienen cita literal verificada; 4) se compara y se muestra el hecho. Si se agota la cuota a mitad, el resultado es parcial y se conserva lo guardado.

La página **Auditoría** (`/facts/{id}/audit`) lista las consultas, todas las fuentes con su estado y motivo, las afirmaciones descartadas y el reparto de aceptadas por tipo y por dominio. El informe incluye un apartado **Metodología** con lo mismo en resumen. Después puedes corregir a mano en la pantalla de revisión (enlazada discretamente), agrupar por punto con IA y generar el "Informe con redacción IA". Sesgo residual: el buscador y la IA pueden influir en qué fuentes aparecen; se mitiga con consultas neutras y variadas y con transparencia total.

**Configurar la clave (gratis):**
1. Crea una clave en <https://aistudio.google.com/apikey>.
2. En la raíz del proyecto crea un archivo `.env` (ya está en `.gitignore`) con `GEMINI_API_KEY=tu_clave`.
3. Reinicia `enevidencia`. Sin clave el modo manual (abajo) sigue funcionando.

> **Aviso de privacidad:** los textos de las fuentes y el tema se envían a Google (Gemini). En el nivel gratuito, Google puede usarlos para mejorar sus modelos. No uses este modo con información confidencial o personal. La clave nunca se muestra en la interfaz.

### Modo manual
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
| `pipeline` | Filtros objetivos de aceptación de fuentes (`config/pipeline.yaml`) |
| `llm`, `discovery`, `classification_llm` | Cliente Gemini, búsqueda de fuentes, clasificación asistida |
| `extraction.llm`, `comparison.llm`, `reporting.llm` | Afirmaciones propuestas con cita verificada, agrupación, narrativa validada |
| `web` | Interfaz FastAPI + Jinja2 |

## Seguridad
Solo se descargan URLs `http(s)` hacia hosts públicos (se bloquean `file://`, `localhost`, IPs privadas y redirecciones a ellas). Todo contenido externo se escapa en las plantillas.

## Tests
```bash
pytest
```

Consulta [docs/PRINCIPIOS.md](docs/PRINCIPIOS.md). Licencia MIT.
