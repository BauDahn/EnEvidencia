# Principios

1. **Evidencia, no veredictos.** EnEvidencia muestra qué dice cada fuente y dónde coinciden o se contradicen. Nunca marca algo como verdadero o falso.
2. **Trazabilidad total.** Toda afirmación tiene fuente, autor de la afirmación y cita textual literal. El sistema rechaza citas que no aparezcan en el texto original.
3. **Clasificación descriptiva y editable.** Primaria / secundaria / institucional describen el tipo de documento, no su fiabilidad. Las reglas viven en `config/classification.yaml` y cada fuente puede corregirse a mano.
4. **El LLM propone, el código verifica con criterios documentados, todo queda auditable y el usuario puede corregir después.** La IA sugiere consultas, tipos, afirmaciones, agrupaciones y redacción. El código decide qué entra: comprueba cada cita literal contra el texto, valida referencias y URLs, y aplica a las fuentes filtros objetivos y deterministas (longitud mínima, términos distintivos del tema, sin duplicados, máximo por dominio y total) definidos en `config/pipeline.yaml`. Ningún filtro mide fiabilidad, postura u orientación. El flujo "Investigar tema" es automático de principio a fin: el usuario no acepta ni descarta nada porque eso introduciría su sesgo. A cambio, cada decisión queda registrada en la página de **Auditoría** (consultas, todas las fuentes con su motivo de aceptación o exclusión, afirmaciones descartadas, reparto por tipo y dominio) y el informe incluye un apartado de **Metodología**. La revisión manual sigue disponible después para corregir. Si la IA falla o se rechaza su texto, se indica; nunca se silencia.

**Sesgo residual.** El buscador y el LLM que propone consultas pueden sesgar qué fuentes aparecen; el código no puede eliminarlo. Se mitiga con consultas neutras y variadas (el tema literal siempre primero, términos descriptivos, cobertura de noticias, comunicados, sentencias y entrevistas), con límites por dominio que evitan que un solo medio domine, y con transparencia: la metodología y todas las exclusiones son visibles para que cualquiera pueda juzgar el equilibrio.

## Criterios de clasificación
- **Institucional**: publicada por una administración u organismo público (reglas por dominio).
- **Primaria**: el documento es la declaración en sí (entrevista, sentencia, comunicado).
- **Secundaria**: pieza que cita o resume a terceros (noticia, artículo, reportaje).

Las reglas se evalúan en orden y gana la primera que coincida; cada fuente guarda el motivo aplicado.

## Cuentas y moderación
- **Lectura abierta, escritura con cuenta.** Cualquiera puede leer dossiers, auditorías e informes. Las acciones que cambian datos (investigar, crear hechos, añadir fuentes, aceptar o descartar, corregir clasificaciones) las hace el administrador, que es el primer usuario registrado.
- **Datos mínimos.** Se guardan usuario (público), correo (no público) y contraseña (solo su resumen scrypt con sal). Sin rastreadores ni servicios externos de identidad. Ver `/normas`.
- **Seguridad por defecto.** Sesiones revocables en servidor, cookies HttpOnly/SameSite, CSRF en todo POST, límite de intentos de acceso y cabeceras de seguridad.
- **Moderación.** Las cuentas pueden bloquearse (`Accounts.ban`), lo que cierra sus sesiones; las normas de convivencia piden fuentes reales y trato respetuoso. La moderación actúa sobre conductas y cuentas, nunca decide qué es verdad: la neutralidad de los dossiers no cambia por quién participe.

## Temas propuestos

La comunidad propone y apoya temas; el administrador decide cuándo gastar cuota de IA investigándolos. Los apoyos miden interés, no verdad: no deciden nada sobre lo que dicen las fuentes ni entran en ninguna comparación. Un tema es una pregunta o un hecho a contrastar, nunca una opinión ni una acusación; se prohíben los datos personales de particulares y los temas que los incumplan pueden denunciarse, ocultarse o rechazarse. Todo texto de usuario se muestra como texto plano escapado.

## Comunidad y moderación
- **Opiniones de lectores, no evidencia.** `/comunidad` es una tribuna ("cartas al director") con secciones (General, Metodología, Fuentes y datos, Errores y correcciones, Sugerencias). Lo que se publica ahí **nunca entra en comparaciones, estados entre fuentes ni informes**; los dossiers solo enlazan a su debate ("Debate de lectores").
- **Lectura pública, escritura con cuenta.** Anti-spam: máximo 10 mensajes por hora, duplicados rechazados, mensajes de 5 a 5000 caracteres y títulos de 8 a 140. Se puede editar lo propio durante 15 minutos y eliminarlo siempre.
- **Moderación sobre conductas, no sobre la verdad.** Los lectores denuncian; en `/admin/moderacion` la administración oculta o restaura mensajes, cierra o fija hilos, cierra denuncias y bloquea o desbloquea cuentas (con motivo; nadie puede bloquearse a sí mismo ni a otro administrador). El contenido retirado se muestra como "[mensaje retirado por moderación]" (lo siguen viendo su autor y la administración, con aviso). Toda acción queda en un registro (`mod_log`) visible en esa página.
- **Seguridad.** Todo texto de usuario se escapa; solo se enlazan URLs http/https con `rel="nofollow ugc noopener noreferrer"`; CSRF en todo POST; consultas parametrizadas.
