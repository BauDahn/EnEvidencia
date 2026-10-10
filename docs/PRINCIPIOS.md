# Principios

1. **Evidencia, no veredictos.** EnEvidencia muestra qué dice cada fuente y dónde coinciden o se contradicen. Nunca marca algo como verdadero o falso.
2. **Trazabilidad total.** Toda afirmación tiene fuente, autor de la afirmación y cita textual literal. El sistema rechaza citas que no aparezcan en el texto original.
3. **Clasificación descriptiva y editable.** Primaria / secundaria / institucional describen el tipo de documento, no su fiabilidad. Las reglas viven en `config/classification.yaml` y cada fuente puede corregirse a mano.

## Criterios de clasificación
- **Institucional**: publicada por una administración u organismo público (reglas por dominio).
- **Primaria**: el documento es la declaración en sí (entrevista, sentencia, comunicado).
- **Secundaria**: pieza que cita o resume a terceros (noticia, artículo, reportaje).

Las reglas se evalúan en orden y gana la primera que coincida; cada fuente guarda el motivo aplicado.
