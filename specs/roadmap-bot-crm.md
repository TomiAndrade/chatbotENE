# Roadmap de mejoras del bot y CRM

Fecha: 21 de septiembre de 2026.

## Forma de trabajo

Codex coordina el alcance, redacta criterios de aceptación y revisa resultados.
Claude Code realiza toda la implementación: código, tests, migraciones y documentación técnica.
Se trabaja en `develop`, en entregas pequeñas y verificables. No hacer deploy,
push ni cambios sobre datos reales como parte de estas entregas locales.
Los cambios preexistentes se preservan. `PENDIENTES.md` sigue siendo la fuente
de verdad de pendientes operativos; este documento ordena las mejoras de producto.

Este roadmap amplía deliberadamente el alcance inicial del CRM. Antes de cada
etapa, documentar su contrato en un spec concreto, sin interpretar las exclusiones
históricas como autorización para agregar todo de una vez.

## Etapa 1: conversaciones claras y registro de errores

### 1.1 Adjuntos no soportados — primera entrega

Objetivo: dejar de pedirle al modelo que interprete marcadores de archivos.

- El tipo real del webhook determina si el mensaje es texto o un adjunto no soportado.
- Guardar el mensaje entrante conservando su identificador para deduplicación.
- Responder con un texto fijo breve y acorde al tipo: no puede ver imágenes o
  documentos ni escuchar audios; pedir la consulta por escrito.
- No llamar al modelo, prometer capacidades, pedir reenvíos ni escalar por ese hecho.
- Respetar firma, modo humano, límite por conversación y tratamiento de errores.
- No confundir un texto escrito por el usuario que imite el marcador con un archivo real.
- Mantener la compatibilidad con las llamadas internas y los tests existentes.

Cierre: tests de contenido, cero llamadas al proveedor, dedup, límite y modo
humano; suite pertinente y resultado documentado. La prueba real de WhatsApp
queda diferenciada de las pruebas con dobles.

### 1.2 Agrupar y ordenar mensajes consecutivos

Objetivo: responder una vez a una idea enviada en varios mensajes.

- Ventana breve configurable (propuesta inicial: 2 segundos), con espera máxima
  para que una ráfaga continua no posponga la respuesta indefinidamente.
- Conservar mensajes individuales en el historial y deduplicar cada identificador.
- Una generación activa por conversación; contactos distintos siguen independientes.
- Evitar incluir dos veces los mensajes agrupados en el contexto del modelo.
- Definir qué ocurre con mensajes que llegan durante una generación, adjuntos
  mezclados con texto, cambios de modo humano y reinicios.
- Antes de implementar, explicitar el comportamiento con varios procesos y la
  recuperación de trabajo pendiente. No introducir una cola solo en memoria
  suponiendo que sirve para múltiples procesos.
- El webhook sigue respondiendo rápido; la espera ocurre fuera del request.

Cierre: pruebas deterministas de ráfagas, contactos independientes, orden,
dedup y pausa humana; prueba manual de tres mensajes que producen una sola respuesta.

### 1.3 Marcar una respuesta problemática en el CRM

- Acción sobre mensajes del bot: motivo, comentario y respuesta esperada opcional.
- Guardar mensaje, autor de la marca y fecha; permitir consultar marcas pendientes.
- No enviar el comentario al usuario ni incorporarlo automáticamente al prompt.
- Validar sesión y CSRF; renderizar comentarios como texto seguro.
- Si cambia el esquema, entregar un procedimiento de migración y reversión
  verificable contra una base de prueba, sin tocar producción.

Cierre: crear y consultar una marca desde el panel, controles de acceso y
persistencia probados, revisión del flujo visual.

## Etapa 2: calidad del bot medible

### 2.1 Casos de evaluación

Convertir ejemplos anonimizados en casos reproducibles: alquiler genérico,
resumen, CV, fotos, adjunto seguido de "¿es gratis?", propuesta de plataforma,
"¿existe ya?", "ok" y recuperación tras una oferta incorrecta del historial.
Evaluar pertinencia, brevedad, fidelidad a datos y límites de capacidades.
Separar pruebas internas de evaluaciones con modelo real; registrar versión
del prompt, modelo y resultado. No afirmar que los mocks validan al modelo.

### 2.2 Depurar el conocimiento

Separar información de atención de notas internas y checklist históricos.
Preservar precios, condiciones, contactos vigentes y límites de conocimiento.
Comparar respuestas antes y después con los mismos casos; no incorporar
recuperación vectorial ni otra infraestructura sin una necesidad medida.

## Etapa 3: seguimiento operativo en el CRM

### 3.1 Bandeja de pendientes

Estados de atención: pendiente, en atención y resuelta, independientes del
estado del bot. Mostrar antigüedad pendiente y permitir reapertura explícita.
Definir cuándo un mensaje nuevo reabre una consulta resuelta.

### 3.2 Buscar y organizar

Búsqueda por identificador y contenido, nombre cuando exista, etiquetas de
consulta. Respetar identificadores externos verbatim y no asumir que son
siempre teléfonos. Mantener paginación y comportamiento móvil.

### 3.3 Notas y responsable

Asignación a una cuenta del equipo, notas privadas y registro de cambios.
No exponer notas al usuario ni al modelo por defecto.

## Etapa 4: atención humana completa

### 4.1 Tomar conversación y responder

Pausar al bot antes de responder, registrar autor y resultado de envío,
evitar carreras con respuestas en generación y devolver control explícitamente.
Verificar el contrato vigente de Meta antes de implementar envíos humanos,
incluidos los límites de su ventana de atención y el comportamiento fuera de ella.
No agregar plantillas ni campañas implícitamente.

### 4.2 Avisos por mail

Aviso al equipo después de persistir el escalamiento, enlace al CRM y resumen
mínimo; fallo del mail no revierte la pausa. Evitar avisos duplicados y mostrar
fallos recuperables. Destinatario y configuración real pendientes de definir.
Cerrar integración solo tras recibir un mail real en la casilla acordada.

### 4.3 Validación operativa

Persona responsable, acceso HTTPS, respaldo, prueba de ida y vuelta y
confirmación de que ninguna consulta queda esperando sin un canal atendido.
Activar escalamiento y desplegar son pasos posteriores explícitos.

## Estado de ejecución

- Roadmap: preparado.
- **1.1: implementada por Claude Code (2026-09-21), con tests, sin validar
  todavía contra WhatsApp real.** Spec de la entrega:
  `specs/spec-adjuntos-no-soportados.md`. `procesar_mensaje_entrante` decide,
  a partir del `messages[].type` real del webhook (nunca comparando texto
  contra el placeholder), entre el camino normal de texto y una respuesta
  fija que no llama al modelo ni escala por sí sola. 11 tests nuevos en
  `tests/test_adjuntos_no_soportados.py` (texto fijo por tipo —
  `image`/`document`/`audio`/genérico—, cero llamadas al proveedor, un texto
  que imita el marcador sigue yendo por el camino normal, dedup, modo
  humano, límite por hora, varios tipos en el mismo `entry`, y que un fallo
  de envío no rompe ni escala) más un test existente actualizado en
  `test_webhook_meta.py`. Suite completa: 274 tests, todos verdes, sin
  fallos preexistentes. Falta la prueba real de que un adjunto real (foto,
  PDF, nota de voz) dispara el texto correcto en un chat de WhatsApp de
  verdad — queda a cargo de quien la corra, junto con el resto de la
  validación de servicios reales de PENDIENTES.md, sección 1.
- **1.2: implementada por Claude Code (2026-09-21), con tests, sin validar
  todavía contra WhatsApp real.** Spec de la entrega:
  `specs/spec-agrupamiento-mensajes.md`. Los mensajes de texto consecutivos
  de una misma conversación se agrupan en una sola llamada al modelo:
  ventana de espera breve (`AGRUPAR_VENTANA_SEGUNDOS`, default 2s) con tope
  duro (`AGRUPAR_ESPERA_MAXIMA_SEGUNDOS`, default 8s), coordinado con una
  reserva atómica en la base (`UPDATE` condicional sobre columnas nuevas de
  `Conversacion`, no una cola en memoria — funciona igual con más de un
  proceso) y recuperación automática si el proceso que tenía la reserva se
  cae (`AGRUPAR_ABANDONO_SEGUNDOS`, default 60s). Los adjuntos no participan
  del agrupamiento, siguen la entrega 1.1 sin cambios. Esquema: tres columnas
  nuevas en `conversaciones` y una en `mensajes`, migradas con
  `scripts/migracion_agrupamiento.sql` (no corrida contra ninguna base
  real). 10 tests nuevos en `tests/test_agrupamiento.py` (ráfaga de 3
  mensajes → una sola respuesta con el texto concatenado en orden, dedup
  dentro de una ráfaga, contactos independientes, un adjunto en medio de una
  ráfaga se responde aparte, `modo_humano` durante la espera corta todo,
  reserva abandonada retomable, dos reservas simultáneas, liberación con
  token viejo no pisa una reserva nueva) más 2 en `tests/test_historial.py`
  (`construir_historial` con una lista). Dos tests de carrera de
  `tests/test_escalamiento.py` quedaron obsoletos por diseño (la carrera que
  probaban ya no puede pasar con la reserva por conversación) y se
  documentaron in situ, reemplazados por un test equivalente bajo el
  mecanismo nuevo. Suite completa: 282 tests, todos verdes, sin fallos
  preexistentes, corrida varias veces para descartar flaky de los tests con
  hilos. Falta la prueba real de que tres mensajes seguidos por WhatsApp
  real producen una sola respuesta coherente, con una demora percibida
  razonable — se suma a PENDIENTES.md, sección 1.
- 1.3 a 4.3: pendientes, en el orden anterior.

Cada entrega debe informar archivos cambiados, pruebas ejecutadas y sus
resultados, límites pendientes y próxima tarea. No marcar etapas completas
por tener código si sus criterios aún no fueron verificados.
