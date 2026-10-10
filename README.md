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

La página **Auditoría** (`/facts/{id}/audit`) lista las consultas, todas las fuentes con su estado y motivo, las afirmaciones descartadas y el reparto de aceptadas por tipo y por dominio. El informe en página incluye un apartado **Metodología** con lo mismo en resumen. Después puedes corregir a mano en la pantalla de revisión (enlazada discretamente), agrupar por punto con IA y generar el informe con redacción IA. Sesgo residual: el buscador y la IA pueden influir en qué fuentes aparecen; se mitiga con consultas neutras y variadas y con transparencia total.

**Configurar la clave (gratis):**
1. Crea una clave en <https://aistudio.google.com/apikey>.
2. En la raíz del proyecto crea un archivo `.env` (ya está en `.gitignore`) con `GEMINI_API_KEY=tu_clave`.
3. Reinicia `enevidencia`. Sin clave el modo manual (abajo) sigue funcionando.

> **Aviso de privacidad:** los textos de las fuentes y el tema se envían a Google (Gemini). En el nivel gratuito, Google puede usarlos para mejorar sus modelos. No uses este modo con información confidencial o personal. La clave nunca se muestra en la interfaz.

### Modo manual
1. Crea un **hecho** (p. ej. "Renta exigida").
2. Añade **fuentes** (URL, y texto original pegado o descargado). Se clasifican según `config/classification.yaml`.
3. En cada fuente, registra **afirmaciones**: quién la dijo, cita textual (se verifica contra el texto) y un valor comparable opcional.
4. Mira la vista comparada del hecho y, en la pestaña **Informe** del mismo hecho, el resumen redactado con IA (el administrador lo genera una vez con el botón «Redactar informe con IA»; queda guardado y se muestra a todos, con citas [n] a cada fuente y metodología).

La base de datos se guarda en `data/enevidencia.db` (cambia la ruta con `ENEVIDENCIA_DB`).

## Estructura
| Módulo | Función |
|---|---|
| `ingestion` | Descarga/recibe fuentes completas con URL, fecha, medio, autor |
| `extraction` | Crea afirmaciones verificando que la cita está en la fuente |
| `comparison` | Agrupa por hecho y valor: coinciden / se contradicen |
| `reporting` | Numeración única [n] de fuentes e informe en página |
| `pipeline` | Filtros objetivos de aceptación de fuentes (`config/pipeline.yaml`) |
| `llm`, `discovery`, `classification_llm` | Cliente Gemini, búsqueda de fuentes, clasificación asistida |
| `extraction.llm`, `comparison.llm`, `reporting.llm` | Afirmaciones propuestas con cita verificada, agrupación, narrativa validada |
| `web` | Interfaz FastAPI + Jinja2 |

## Cuentas e inicio de sesión

Sin servicios externos: usuarios y sesiones viven en la misma base SQLite (`src/enevidencia/accounts.py`). Contraseñas con scrypt y sal por usuario; sesiones con token aleatorio guardado como sha256 (14 días); cookie `ee_session` HttpOnly, SameSite=Lax (y `Secure` si `ENEVIDENCIA_SECURE_COOKIES=1`, imprescindible tras HTTPS). El **primer usuario registrado es administrador** (ver «Seguridad y despliegue»). Rutas: `/registro`, `/entrar`, `/salir`, `/cuenta`, `/normas`.

- La lectura (portada, dossiers, auditoría, informes) es pública. Solo el administrador puede investigar temas, crear hechos, añadir fuentes, aceptar/descartar y corregir clasificaciones.
- Todo POST exige token CSRF (cookie `ee_csrf` + campo oculto `csrf_token`, o cabecera `X-CSRF-Token`). Cabeceras de seguridad y CSP en todas las respuestas.
- Intentos de login fallidos: 5 por IP+usuario y bloqueo de 15 minutos (en memoria).
- Para otras rutas: `from enevidencia.web.deps import current_user, require_user, require_admin`; en plantillas `user` y `csrf_token`.

## Temas propuestos por los lectores

En `/temas` cualquier usuario con sesión (no baneado) puede sugerir un tema (titular de 8 a 140 caracteres, contexto opcional de hasta 1000; máximo 5 sugerencias cada 24 horas; se rechazan titulares duplicados), apoyar los de otros (un apoyo por usuario, retirable) y denunciar los inadecuados. El administrador ve las denuncias, puede ocultar, rechazar o marcar como hecho un tema y pulsar «Investigar este tema», que lanza la misma investigación automática que `/investigate` (y gasta cuota de IA) y enlaza el tema con su dossier. Código: `src/enevidencia/topics.py` y `web/topics_routes.py`.

## Comunidad y moderación

`/comunidad` (módulo `src/enevidencia/community.py`, rutas en `web/community_routes.py`): foro de lectores con hilos por sección, respuestas paginadas, búsqueda, edición propia (15 min), denuncias y debate por hecho (`/comunidad?hecho=ID`, bloque "Debate de lectores" al final de cada dossier). Leer es público; escribir requiere cuenta y no estar bloqueado. Los administradores moderan en `/admin/moderacion` (denuncias, ocultar/restaurar, cerrar y fijar hilos, bloquear/desbloquear usuarios y registro de acciones). Las aportaciones son opiniones, no evidencia: no entran en comparaciones ni informes. Tablas: `threads`, `posts`, `reports`, `mod_log` (misma base SQLite).

## Seguridad y despliegue

**Red y HTTPS.** El servidor escucha solo en `127.0.0.1:8000` por defecto. Para publicarlo, ponlo detrás de un proxy inverso con HTTPS (Caddy, nginx...) y arranca con `ENEVIDENCIA_SECURE_COOKIES=1` para que las cookies lleven `Secure`.

**Proxy y IP del cliente.** Los límites por IP (registro, login) usan `request.client.host`. Uvicorn solo sustituye esa IP por `X-Forwarded-For` si el proxy está en `forwarded-allow-ips` (por defecto `127.0.0.1`, o la variable `FORWARDED_ALLOW_IPS`). Si el proxy corre en la misma máquina no hay que tocar nada; si está en otra, define `FORWARDED_ALLOW_IPS=<ip del proxy>` y nunca `*` con el puerto expuesto: cualquiera podría falsear su IP y saltarse los límites. Si no usas proxy, deja el valor por defecto.

**Administrador.** El primer usuario registrado es admin (alta atómica: dos procesos no pueden crear dos). Con `ENEVIDENCIA_ADMIN_USER=<usuario>` solo ese nombre será admin al registrarse. Para promover o recuperar una cuenta: `enevidencia make-admin <usuario>` (usa `ENEVIDENCIA_DB`).

**Registro y login.** Máximo 3 registros por IP y hora y 10 por día (persistido en la BD, sobrevive a reinicios), campo trampa (honeypot) oculto y retraso mínimo de 2 s entre mostrar y enviar el formulario (`ENEVIDENCIA_MIN_FORM_SECONDS`, 0 lo desactiva; con varios procesos fija `ENEVIDENCIA_SECRET`). El login se bloquea tras 5 fallos por (IP, usuario) y tras 20 fallos por IP, 15 minutos. *Riesgo aceptado (M4):* el formulario de registro revela si un usuario o correo ya existe (enumeración); se asume a cambio de un mensaje de error útil, mitigado por los límites anteriores.

**Cuota de IA.** Todo lo que gasta cuota de Gemini es solo para administradores (investigar, extraer, informe, agrupación con IA `?grouped=1`); los lectores anónimos nunca disparan llamadas al LLM. Cada fuente se trunca a 120.000 caracteres y se envían como máximo 12 trozos al LLM (`config/pipeline.yaml`: `max_caracteres_fuente`, `max_trozos_extraccion`); ante un error de cuota la extracción se detiene y devuelve lo ya obtenido. Los truncados quedan en la auditoría.

**Descargas (SSRF).** Solo `http(s)` a los puertos 80/443 (ampliable con `ENEVIDENCIA_EXTRA_PORTS=8080,8443`), hacia IPs públicas: se bloquean localhost, redes privadas, NAT64 y direcciones IPv6 mapeadas. Si el host no resuelve, falla. El host se resuelve una vez, se valida y se conecta a esa IP (sin reresolver; evita DNS rebinding), conservando Host y SNI. Máximo 20 MB (también descomprimido; solo gzip/deflate), 30 s en total, 150 páginas por PDF.

**Cuerpos de petición.** 413 si Content-Length supera 1 MB (64 KB en formularios; 2 MB solo en `/sources`), también cortando el flujo si no hay Content-Length; se aplica antes de la comprobación CSRF.

**Inyección de prompt.** El contenido de las fuentes va delimitado (`<<<FUENTE ... FUENTE>>>`, delimitadores neutralizados) y marcado como datos. El código verifica la cita literal y la atribución (el autor debe aparecer en la cita o ~400 caracteres alrededor, o ser el medio/autor); si no, la afirmación queda PROPUESTA y en la auditoría («atribución no verificada»). Los informes se reintentan una vez si hay frases factuales sin referencia `[n]`.

## Tests
```bash
pytest
```

Consulta [docs/PRINCIPIOS.md](docs/PRINCIPIOS.md). Licencia MIT.
