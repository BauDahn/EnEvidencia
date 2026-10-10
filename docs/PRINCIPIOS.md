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
