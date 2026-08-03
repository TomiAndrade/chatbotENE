# Pendientes — Bot WhatsApp ENE IA LAB

Todo lo que queda por hacer, al 31/07/2026, con la etapa 1 cerrada y la etapa 2
implementada y con tests.

Lo importante primero: **del `spec-etapa2.md` no quedó nada sin implementar en
código**. Los 35 tests pasan. Lo que falta para poder cerrar la etapa 2 como se
cerró la 1 es la validación contra los servicios reales (punto 1 de acá abajo).

Ordenado por lo que conviene hacer antes, no por importancia absoluta.

---

## 1. Validar la etapa 2 contra servicios reales — bloqueante

La suite de `tests/` mockea Kapso y mockea el proveedor de IA. Eso confirma que
el wiring interno está bien (historial, escalamiento, límite, manejo de
errores), pero **todavía no se mandó un solo mensaje real a Gemini ni a
Claude**, ni se hizo una vuelta completa por WhatsApp con IA de verdad.

De los 8 criterios de aceptación del `spec-etapa2.md`, sólo el 8 ("los tests
pasan") está verificado. Los otros siete hay que probarlos a mano, celular →
sandbox de Kapso → ngrok → servidor, igual que en la prueba end-to-end de la
etapa 1:

1. Se escribe "hola" y el bot se presenta como asistente de ENE.
2. Se pregunta el precio de la membresía individual y responde $85.000, corto y
   sin markdown.
3. Se pregunta por alquiler de una sala y **escala**: `modo_humano` en `true`,
   resumen guardado, y llega el aviso que corresponde al horario.
4. A partir de ahí el bot **no responde más** en esa conversación.
5. Se pregunta una receta de cocina y redirige **sin** escalar.
6. Se pregunta algo relacionado pero ausente del knowledge base (si hay
   bicicletero, por ejemplo) y **escala** en vez de rechazarlo como fuera de
   tema.
7. Se pregunta el precio de una oficina y **no inventa** un número.

El punto 6 es el que más suele fallar. Si el bot lo rechaza como fuera de tema,
el problema está en el prompt, no en el código.

Para correr la prueba hace falta:

- `GEMINI_API_KEY` en `.env` (free tier, sin tarjeta) y `PROVEEDOR_IA=gemini`.
  **Ojo:** `.env.example` trae `PROVEEDOR_IA=fijo`, que es el valor correcto
  para desarrollar y para los tests, pero con `fijo` el bot contesta el texto
  de la etapa 1 y ninguno de los 7 puntos se cumple.
- `uvicorn app.main:app --reload --port 8000` y `ngrok http 8000` (puerto 8000,
  no 3000).
- La URL de ngrok configurada como webhook en el sandbox de Kapso.

No requiere cambios de código.

---

## 2. Hallazgos del spike de tool calling contra OpenRouter (31/07/2026)

Antes de sumar un proveedor nuevo se corrió un **spike descartable, fuera de
este repo** (`~/spike`, no versionado), que pega a la API de OpenRouter con
`nvidia/nemotron-3-ultra-550b-a55b:free` en formato OpenAI, declarando la
herramienta `escalar_a_humano`. Se hicieron tres tandas: `tool_choice` forzado,
`tool_choice: "auto"` con un system prompt corto de prueba, y `auto` con el
**system prompt real del bot** (armado con el propio `app/prompt.py`, no una
copia).

Nada de esto está implementado: es documentación de lo que se aprendió, y las
consecuencias para cuando se decida el proveedor definitivo.

### Lo que quedó validado

- **El contrato de tool calling en formato OpenAI funciona.** Llega
  `tool_calls`, `function.name` es `escalar_a_humano` y `function.arguments`
  trae la clave `resumen`.
- **`function.arguments` viene como string con JSON adentro, no como objeto** —
  requiere un `json.loads` del lado del proveedor. Es distinto de los dos
  proveedores actuales, donde el SDK ya entrega un dict
  (`bloque.input` en Claude, `llamada.args` en Gemini).
- **Con el prompt real, el escalamiento se dispara** en el criterio de
  aceptación 3 (alquiler de sala). La respuesta vino con `content: None` —o sea
  que respetó *"No anuncies que vas a escalar ni escribas un mensaje de
  despedida"*— y el resumen reflejaba la regla 10 del prompt: *"El polo ofrece
  este servicio pero el bot no tiene esos datos"*.

### Cambios necesarios si se suma un proveedor OpenAI-compatible

- **OpenRouter devuelve errores adentro de un HTTP 200.** Un fallo llegó como
  `200 OK` con body `{"error":{"message":"Internal error...","code":502}}`.
  Esto es un agujero real en el manejo de errores: `con_un_reintento`
  (`app/respuesta.py:50`) reintenta ante **cualquier excepción**, y un 200 no
  levanta ninguna en un cliente HTTP — así que no habría reintento, el parseo no
  encontraría `choices` y terminaría como respuesta vacía. El chequeo tiene que
  ser sobre la **clave `error` del body**, no sobre el código HTTP.
  > Ojo con el matiz al leer el código: el reintento por status (429 y 5xx) es
  > `_conviene_reintentar` en `app/kapso.py:25` y aplica a los **envíos a
  > Kapso**, no a la llamada al modelo. Son dos mecanismos distintos.

  **Esto también hay que reflejarlo en la sección "Manejo de errores" de
  `spec-etapa2.md`**, que hoy enumera los fallos como "timeout, rate limit,
  error de la API" asumiendo que todos se manifiestan como excepción.
- **El `resumen` puede venir mal formado.** Si el tool call llegó pero
  `arguments` no parsea como JSON, o parsea pero no trae `resumen`, **igual hay
  que escalar** (`escalar=True`, `resumen=None`): un `JSONDecodeError` no puede
  tumbar el request. Que el resumen se pierda es peor para quien atienda, pero
  no perder el escalamiento es lo que importa.

### Confiabilidad del free tier y sus consecuencias

- **El free de Nemotron vía OpenRouter es poco confiable: 5 de 12 requests
  fallaron** con `Upstream error from Nvidia: ResourceExhausted: Worker local
  total request limit reached (32/32)`. Es saturación transitoria del tier
  gratuito, **sin relación con el tamaño del payload** (se descartó bisecando:
  28k chars pasaba, 32k fallaba y 35k/37k volvían a pasar — el rango no cierra
  por tamaño, y el propio mensaje de error dice que es límite de workers).
- **Consecuencia práctica sobre la regla "error → escalar":** con esa tasa de
  fallos, cerca de una de cada dos conversaciones de prueba va a quedar trabada
  en `modo_humano` sin que haya pasado nada malo con el bot.
- **Decisión pendiente: distinguir el error transitorio del proveedor del resto,
  al menos en desarrollo.** Un error transitorio debería responder "problema
  técnico, probá de nuevo" y **no** prender `modo_humano`. En producción con un
  proveedor pago, escalar sigue siendo lo correcto — la regla actual no está
  mal, le falta el caso de desarrollo.
- **Hace falta un script de reset de `modo_humano` por teléfono.** No es un
  nice-to-have: los criterios de aceptación 5, 6 y 7 **no se pueden probar sin
  él**, porque el criterio 4 (el bot deja de responder tras escalar) corta la
  conversación. Hoy `modo_humano` se desmarca a mano en la base. Con la tasa de
  fallos de arriba se va a usar seguido.

### Costo del prompt y elección de proveedor

El system prompt real (`system-prompt.md` + `knowledge-base.md`) son **37.517
caracteres = 10.371 tokens**, con 36 bloques `[PENDIENTE]`. **Veníamos
estimando ~3.000 tokens**, o sea más del triple. Eso reordena las opciones:

| Proveedor | Efecto |
|---|---|
| Groq | **Queda descartado.** Con un TPD de 100–200K son 10 a 20 llamadas por día. |
| DeepSeek | El bono de 5M tokens rinde **~470 llamadas, no ~1.600**. |
| OpenRouter | **No se ve afectado**: su límite es por request, no por token. |

Y va a seguir creciendo: completar los `[PENDIENTE]` de la sección 3 agranda el
knowledge base, no lo achica.

### Cómo testear esto (aprendizajes de método)

- **El escalamiento es no determinista.** Con el prompt corto, dos corridas
  idénticas dieron resultados distintos: una escaló y la otra no. Con el prompt
  real escalaron 5 de 5. **Implicación directa para validar la etapa 2**: no
  sacar conclusiones de una sola muestra en ningún criterio que dependa del
  juicio del modelo (3, 5, 6 y 7 de la sección 1).
- **Fallo silencioso a vigilar:** el modelo puede prometer la derivación en el
  texto ("te derivo al área correspondiente") **sin llamar a la herramienta**.
  El usuario lee que lo van a derivar, `modo_humano` nunca se prende y nadie se
  entera. La regla del system prompt que lo prohíbe funcionó en las corridas con
  prompt real (`content: None`), pero hay que reconfirmarlo contra el modelo que
  se use en producción.
- **`enable_thinking: False` no tiene efecto en Nemotron**: la respuesta trajo
  `reasoning_tokens: 210` igual. Si la latencia importa, ese parámetro no es la
  palanca.

### Alcance — qué NO valida este spike

Valida el contrato de tool calling y que el prompt real induce el escalamiento
en el criterio 3. **No valida el bot.** Sigue faltando todo lo de la sección 1:
reconfirmar contra el proveedor que se termine usando (hoy `PROVEEDOR_IA` solo
conoce `fijo`/`gemini`/`claude` — Nemotron vía OpenRouter no es ninguno de
esos) y la vuelta completa por WhatsApp.

---

## 3. Completar los bloques `[PENDIENTE]` del knowledge base — con el equipo del polo

`prompts/knowledge-base.md` tiene los huecos marcados a propósito: le dicen al
modelo qué no sabe, que es exactamente lo que necesita para escalar en vez de
inventar. Están bien así para desarrollar, pero **antes de producción hay que
completarlos con el equipo**, porque cada uno es una consulta que hoy termina en
un humano.

Dos bloques están vacíos enteros:

- **Eventos** (sección 8) — no hay nada. Falta definir si existe una agenda
  publicada, si el bot la lee de una fuente automática (Google Calendar, una
  página) o se carga a mano, y los datos de cada evento: nombre, fecha, hora,
  lugar, si es abierto o sólo para miembros, si requiere inscripción y el link.
- **Alquiler de espacios del polo** (sección 11) — faltan los datos completos de
  oficinas y de sala de presentación por separado: precios (hora / día / mes) y
  capacidad de cada espacio.

Y hay huecos puntuales en el resto:

| Tema | Qué falta confirmar |
|---|---|
| Identidad | Cómo debe nombrarse el bot al presentarse ("ENE", "Polo Tecnológico Neuquén", otra forma) |
| Institucional | Misión o descripción de ENE como polo (hoy sólo está la del laboratorio); si hay otras iniciativas además del IA LAB |
| Horarios | Que el horario 9–17 sea de lunes a viernes (está asumido, no confirmado) |
| Acceso | Si piden DNI en recepción |
| Membresías | Si se puede dar de baja en cualquier momento o el compromiso es por el ciclo completo; si las 3 personas de la Corporativa son fijas o rotan; si al ingresar a mitad de ciclo se paga desde el mes que entra o hay ajuste |
| Pagos | Datos bancarios / CBU o alias (ver la nota de abajo) |
| Postulación | Plazo aproximado de respuesta tras enviar el formulario |
| Cowork | Precios de seats y oficinas (no publicados); si se reserva con anticipación; si las 2 veces por semana son días fijos o los elige el miembro; si el miembro de IA LAB accede a todos los servicios del cowork o sólo a escritorio y wifi |
| Actividades | Cuáles son las actividades abiertas al público y cómo enterarse |
| Work Café | Horarios y si es de acceso público o sólo para usuarios del cowork |
| Proyectos a pedido | Si se toman o no (el trabajo se organiza por verticales); hoy el bot escala esta consulta |

**Regla del proyecto que no cambia:** el bot **nunca** envía datos bancarios,
CBU ni alias, en ninguna etapa. Que el dato falte en el knowledge base no es el
motivo; aunque se complete, no se envía. Lo que hay que definir con el equipo es
quién los manda y cuándo, no si los manda el bot.

---

## 4. Que falte el secreto del webhook falle fuerte, no en silencio

Hoy, si en producción alguien deja `DEBUG=false` pero se olvida de poner
`KAPSO_WEBHOOK_SECRET`, `verificar_firma_webhook` rechaza todo con 401
(`app/kapso.py:110`), que es el comportamiento seguro. El problema es el caso
inverso, el que sí es silencioso: **`DEBUG=true` con el secreto vacío deja el
webhook abierto** a cualquiera que conozca la URL, y nada falla — el server
levanta bien, los mensajes llegan, el bot responde. Lo único que avisa es un
`WARNING` en el log que nadie está mirando.

Lo que falta: un chequeo al arrancar que corte el boot si el secreto está vacío
y no estamos claramente en desarrollo. Que el server no levante es la única
forma de que esto se note.

---

## 5. Configuración de producción

Cuando se vaya a deployar (fuera de alcance por ahora, ver punto 9):

- `DEBUG=false` y `KAPSO_WEBHOOK_SECRET` con el valor del dashboard de Kapso.
- `PROVEEDOR_IA=claude` con `ANTHROPIC_API_KEY`.
- `MODELO` apuntando a un modelo chico y rápido (Haiku), **no** al más grande:
  las respuestas son cortas y el volumen es de WhatsApp.
- `DATABASE_URL` a Postgres. Migrar es cambiar esa variable y nada más.

---

## 6. Guardar el `wa_message_id` de la respuesta del bot

Decidido explícitamente, no un olvido: hoy la respuesta del bot se guarda con
`wa_message_id = None` (`app/main.py:102`). Kapso devuelve el id en
`messages[0].id` al enviar y el campo del modelo existe justo para eso
(`app/models.py:46`), pero todavía no se persiste.

No rompe nada — la dedup es sólo sobre mensajes entrantes — pero sin el id no se
puede cruzar un mensaje de la base con lo que muestra el panel de Kapso, que es
lo primero que se quiere hacer cuando algo sale raro en producción.

---

## 7. Endurecer el chequeo de respuesta vacía

`app/main.py:151` chequea `resultado.texto is None`. Un texto de sólo espacios
pasa ese chequeo y después cae en el `if resultado.texto:` de más abajo, que lo
trata como vacío pero ya no escala: el usuario se queda sin respuesta, que es
justo lo que el spec pide evitar.

Hoy no puede pasar, porque los dos proveedores filtran los bloques de texto
vacíos antes de acumularlos. Cambiar la condición a
`if not resultado.texto or not resultado.texto.strip()` lo cierra del todo. Es
endurecimiento preventivo, no un incumplimiento del spec.

---

## 8. Deuda técnica menor

- **Feriados.** Se tratan como día hábil, así que un 25 de mayo a las 11 el bot
  promete que responden "en breve". Está declarado como deuda conocida en el
  spec y anotado en `app/mensajes.py:35`.
- **`@app.on_event` está deprecado** en la versión de FastAPI del proyecto
  (`app/main.py:54,59`); la suite lo muestra como `DeprecationWarning` en cada
  corrida. Lo que corresponde es un handler de `lifespan`. Funciona igual, pero
  el warning va a seguir apareciendo y en algún momento se va a romper.

---

## 9. Fuera de alcance hasta que se diga lo contrario

Esto **no** es deuda: son decisiones tomadas. Están acá para que no se
redescubran como si fueran olvidos.

- Deploy.
- Transcripción de audios (hoy un audio entra como
  `[mensaje de tipo 'audio' no soportado en esta etapa]`).
- Mensajes con botones, listas o plantillas.
- Devolver una conversación de humano a bot automáticamente — `modo_humano` se
  desmarca a mano en la base.
- Notificar al equipo cuando se escala. Hoy el escalamiento queda como un
  `WARNING` en el log y como `resumen_escalamiento` + `escalada_en` en la tabla
  `conversaciones`; nadie recibe un aviso activo.
