# chatbot-polo — Claude Context

Bot de WhatsApp para **ENE IA LAB**, laboratorio de IA aplicada en el Polo
Tecnológico Neuquén. Atiende consultas sobre membresías, eventos y alquiler de
espacios, y escala a un humano cuando no puede resolver.

## Repositorio y flujo git

- **Remote:** https://github.com/TomiAndrade/chatbotENE.git — repo propio, no
  parte de `TomiAndrade/monorepo`. Vive como carpeta suelta dentro del
  monorepo (mismo patrón que `IALAB-WEB`, sin `.gitmodules`): `git status` en
  la raíz del monorepo lo ve como un gitlink.
- **Rama de trabajo:** `develop`. **Rama de producción:** `main`, solo recibe
  merges vía PR desde `develop` — nunca push directo. No hay deploy atado a
  `main` todavía; es la convención elegida para este repo, no una necesidad
  técnica como en IALAB-WEB (que sí tiene deploy automático a Netlify).

```bash
git checkout develop          # siempre trabajar acá
git commit -m "descripción"
git push origin develop
# cuando esté listo para producción:
gh pr create                  # PR de develop → main, nunca merge directo
```

El proyecto está planificado en etapas. `spec-etapa1.md` y `spec-etapa2.md` son
los specs completos de lo implementado y mandan sobre este archivo si algo se
contradice.

## Estado

**Etapa 1 (plomería: WhatsApp → servidor → base → respuesta fija, sin IA):
cerrada.** Validada con la prueba end-to-end real — celular → sandbox de
Kapso → ngrok → servidor → respuesta en el WhatsApp.

**Etapa 2 (conectar el modelo, armar historial, escalamiento a humano):
código completo y con tests, pero sin validar contra servicios reales.** La
suite de `tests/` mockea tanto Kapso como el proveedor de IA — confirma que el
wiring interno es correcto (historial, escalamiento, límite, manejo de
errores), pero **todavía no se probó un mensaje real contra `openai_compat` ni
contra Claude, ni una vuelta completa por WhatsApp con IA real**. Antes de dar la
etapa por cerrada como la 1, hace falta esa prueba real con al menos un
proveedor.

El servidor corre en el puerto **8000** (uvicorn), no 3000 — así lo espera
`ngrok http 8000` y así está documentado en el README.

Pendiente conocido, decidido explícitamente y no un olvido: **la respuesta del
bot se guarda con `wa_message_id = None`**. Kapso devuelve el id en
`messages[0].id` al enviar, y el campo del modelo existe justo para eso, pero
todavía no se persiste.

**`PENDIENTES.md` tiene la lista completa de lo que falta**, ordenada por
prioridad: la validación real de la etapa 2, los `[PENDIENTE]` del knowledge
base a completar con el equipo, el checklist de deploy y la deuda técnica menor.
Ese archivo es la fuente de verdad de los pendientes; acá abajo sólo están los
que hacen falta para entender el diseño.

## Stack

- Python 3.11+, FastAPI, SQLAlchemy (ORM obligatorio, nada de SQL crudo)
- SQLite en desarrollo, Postgres en producción — migrar es cambiar
  `DATABASE_URL`, nada más
- httpx para las llamadas a Kapso y al proveedor `openai_compat`, python-dotenv
  para la config
- ngrok para exponer el webhook en desarrollo (externo, no es parte del código)
- **Kapso** (https://docs.kapso.ai) como capa sobre la WhatsApp Cloud API de Meta
- **anthropic** (Claude, producción) y **`openai_compat`** (desarrollo: un solo
  proveedor parametrizado por `BASE_URL`, sirve para cualquier endpoint con
  formato de la API de OpenAI — OpenRouter, DeepSeek, el free de NVIDIA, un
  modelo local) como proveedores de IA intercambiables por `PROVEEDOR_IA`
- **tzdata**: necesario en Windows para que `zoneinfo` resuelva
  `America/Argentina/Buenos_Aires` (no hay base de tz del sistema operativo)

## Contrato con Kapso (verificado contra la doc, no asumido)

- Enviar texto: `POST https://api.kapso.ai/meta/whatsapp/v24.0/{phone_number_id}/messages`,
  header `X-API-Key`, payload `messaging_product`/`recipient_type`/`to`/`type`/`text.body`.
  Responde con `messages[0].id`.
- Webhook entrante: payload **sin envelope**, con `message` y `conversation` en
  la raíz. Los campos son `message.id`, `message.from`, `message.type`,
  `message.text.body`.
- Headers que manda Kapso: `X-Webhook-Event` (el tipo de evento, p. ej.
  `whatsapp.message.received`), `X-Webhook-Signature` (HMAC-SHA256 hex del body
  crudo, sin prefijo ni timestamp) y `X-Idempotency-Key`.

## Decisiones de diseño

- **`Conversacion` está preparada para múltiples canales, aunque hoy solo existe
  WhatsApp.** En vez de `telefono`, el modelo tiene `canal` (default `whatsapp`)
  e `identificador_externo` (el número de teléfono, para WhatsApp; un id de
  sesión, para un futuro chat web). La unicidad es el par `(canal,
  identificador_externo)`, no el identificador solo — nada garantiza que un id
  de sesión de otro canal no coincida con un número de teléfono. Todo lo que
  antes filtraba por `telefono` (`buscar_o_crear_conversacion`,
  `mensajes_ultima_hora`, `scripts/resetear_modo_humano.py`) ahora recibe los
  dos campos; `procesar_mensaje_entrante` sigue tomando un solo identificador
  porque el webhook solo atiende WhatsApp, y hardcodea `canal=whatsapp` al
  buscar o crear la conversación. `enmascarar_identificador` (antes
  `enmascarar_telefono`) sigue tapando el medio de la cadena sin asumir formato
  de teléfono, así que sirve igual para un id de otro canal. No se implementó
  nada del canal web todavía — es solo el modelo de datos.
- **El endpoint `/webhook` no toca la base ni la red.** Verifica firma, parsea y
  encola; dedup, guardado y respuesta corren en la background task
  `procesar_mensaje_entrante`, que Starlette ejecuta en threadpool después de
  haber mandado el 200. Consecuencia: los fallos de procesamiento se ven en el
  log, no como 500 en el panel de Kapso.
- **Toda la generación de respuestas vive detrás de
  `generar_respuesta(historial, mensaje_nuevo) -> RespuestaGenerada`**
  (`respuesta.py`), seleccionable con `PROVEEDOR_IA` (`fijo`/`openai_compat`/`claude`).
  Agregar un proveedor nuevo es una clase en `app/proveedor_<nombre>.py` que
  implemente `ProveedorRespuesta`, sumada a `_FABRICAS_PROVEEDORES`; no se toca
  nada más. `RespuestaGenerada` trae `texto`, `escalar` y `resumen` porque con
  tool calling ya no alcanza con devolver un string.
- **`historial` es `list[Mensaje]`, no `list[str]`.** El rol (`usuario`/`bot`/
  `humano`) tiene que viajar: los mensajes escritos por una persona del polo son
  contexto válido para el modelo pero **no** ejemplos de cómo debe responder el
  bot (`app/historial.py:mapear_mensaje`, prefijo `[Respuesta de una persona
  del equipo]` para `humano`). `construir_historial` trae los últimos
  `HISTORIAL_MAX_MENSAJES` y descarta todo si pasaron más de
  `HISTORIAL_DIAS_VALIDEZ` días desde el mensaje anterior.
- **No se implementa el bucle completo de tool calling.** Si el modelo llama a
  `escalar_a_humano`, se corta ahí: el servidor escribe el aviso, no se le
  devuelve el resultado de la herramienta para que siga generando. Simplifica
  mucho y el spec es explícito en que no se pierde nada con eso.
- **El aviso de escalamiento lo escribe el código, no el modelo** (`app/
  mensajes.py`), porque depende de la hora (`TIMEZONE`, configurable, default
  `America/Argentina/Buenos_Aires`) y el modelo no tiene reloj.
- **Si falla la llamada al modelo, o vuelve vacía sin escalar, se trata igual:
  disculpa genérica + escalar.** Una conversación en manos de una persona es
  mejor que una conversación muerta. **Presupuesto de 20s en total, no por
  intento**: con un solo reintento son 10s cada llamada
  (`PRESUPUESTO_TOTAL_SEGUNDOS / MAX_INTENTOS` en `respuesta.py`). Lo que
  importa es cuánto espera el usuario del otro lado de WhatsApp, y 20s por
  intento hacían 40s de espera. Ver `con_un_reintento`, compartido entre
  proveedores.
- **Límite de 30 mensajes de usuario por hora por conversación** (`app/
  limite.py`, ventana deslizante contada contra la base, no en memoria —
  sobrevive a un reinicio del server), contado por el par `(canal,
  identificador_externo)`, no solo por identificador. Solo el mensaje que
  cruza el límite dispara el aviso; los siguientes, mientras siga por encima,
  quedan en silencio total.
- **La dedup es por `wa_message_id`**, con el `SELECT` previo y además
  `IntegrityError` atrapado: entregas concurrentes del mismo webhook pasan las
  dos el chequeo, y la constraint única es la que decide. Los reintentos de
  Kapso son normales, no un error.
- **Se reintenta solo lo que puede salir distinto**: errores de red, 429 y 5xx,
  con backoff 1s → 2s. Los demás 4xx fallan al primer intento y loguean el body
  de la respuesta de Kapso, que es donde viene el motivo real.
- **`modo_humano` se re-lee de la base justo antes de enviar**
  (`esta_en_modo_humano` en `main.py`), no sólo al empezar a procesar el
  mensaje. Si el bot escribe encima de un humano, la experiencia se rompe — y
  entre el chequeo inicial y el envío hay hasta 20s de llamada al modelo, en
  los que otra entrega concurrente del mismo número puede haber escalado.
  Quien no quiere ese chequeo lo pide explícito
  (`aunque_este_en_modo_humano=True`), y el único que lo hace es el aviso de
  escalamiento, que sale justo después de prender el flag. `escalar_a_humano`
  hace la misma relectura y no vuelve a escalar si ya estaba escalada: pisar
  el resumen del primer escalamiento le saca contexto a quien atienda.
- **El escalamiento no es atómico y el orden importa.** Primero el commit de
  `modo_humano`/`resumen`/`escalada_en`, después el `WARNING` de
  `ESCALADO A HUMANO`, y recién al final el aviso al usuario. Si Kapso está
  caído el escalamiento ya ocurrió igual, así que tiene que quedar en el log
  sí o sí; el fallo del aviso se loguea aparte y diciendo qué se perdió, no
  como un error de envío genérico.
- **`prompts/system-prompt.md` y `prompts/knowledge-base.md` se leen una sola
  vez al importar `app/prompt.py`** (no en cada mensaje). Los bloques
  `[PENDIENTE]` del knowledge base se dejan tal cual a propósito: le indican al
  modelo qué no sabe, que es lo que necesita para escalar en vez de inventar.

## Reglas del proyecto

- **El bot nunca envía datos bancarios, CBU ni alias.** En ninguna etapa. Es una
  regla del proyecto, no una omisión temporal.
- `.env` y `bot.db` van en `.gitignore`. Ninguna clave hardcodeada, tampoco en
  comentarios ni en tests.
- Sin `KAPSO_WEBHOOK_SECRET`, el webhook queda abierto a cualquiera que conozca
  la URL. Por eso solo se deja pasar con `DEBUG=true`; con `DEBUG=false` se
  rechaza todo con 401. Ojo con el default — ver el checklist de deploy.
- El código lo lee y mantiene un estudiante de Ciencias de la Computación con
  base en Java y Python básico: **legible y explicable por sobre ingenioso**,
  claridad antes que concisión.

## Checklist de deploy

El deploy está fuera de alcance por ahora, pero esto hay que resolverlo antes:

- **Poner `DEBUG=false` y `KAPSO_WEBHOOK_SECRET` con valor.** El default de
  `.env.example` es `DEBUG=true` con el secreto vacío, que es lo correcto para
  desarrollar pero deja el webhook abierto. El problema es que **si alguien se
  olvida de cambiarlo en producción, nada falla ruidosamente**: el server
  levanta bien, los mensajes llegan y el bot responde, y lo único que avisa es
  un `WARNING` en el log que nadie está mirando. No hay error, no hay 500, no
  hay síntoma visible — solo un webhook público. Un chequeo al arrancar que
  corte el boot si `DEBUG=false` y falta el secreto sería la forma de que esto
  falle fuerte en vez de en silencio.
- **Para producción, además:** `PROVEEDOR_IA=claude` con `ANTHROPIC_API_KEY` y
  `MODELO` apuntando a un modelo chico y rápido (Haiku), no al más grande.

## Etapas siguientes

Con la etapa 2 (LLM, historial, escalamiento) implementada, no queda un
"etapa 3" definida en un spec propio — lo que sigue es validar etapa 2 contra
servicios reales (ver "Estado" arriba) y, más adelante, lo que ya estaba fuera
de alcance:

Fuera de alcance hasta que se diga lo contrario: feriados (se tratan como día
hábil), deploy, transcripción de audios, mensajes con botones/listas/
plantillas, y devolver una conversación de humano a bot automáticamente (se
desmarca `modo_humano` a mano en la base).

## Tests

`tests/` con pytest, 62 tests. No pegan a ninguna API real: Kapso se mockea
(`kapso_enviados`, fixture en `tests/conftest.py`) y el proveedor de IA se
mockea por test parcheando `app.main.generar_respuesta` (`fijo` no necesita
mock); los dos proveedores con IA se prueban con dobles (`httpx.MockTransport`
para `openai_compat`, un cliente falso para `claude`). Usan una base SQLite en
un directorio temporal, no `bot.db`. Cubren: flujo completo del webhook,
matriz de firma, reintentos de Kapso, carrera de entregas concurrentes con
hilos, armado de historial y corte por antigüedad, mensaje de escalamiento
según horario, escalamiento por tool calling, fallo del modelo, error
transitorio del proveedor, límite por número, y parseo de tool calls de los
dos proveedores.

Dos cosas al escribir tests acá, aprendidas de una revisión en la que los
tests pasaban por un vacío:

- **Afirmar el contenido de lo que se envió, no sólo cuántos mensajes
  salieron.** Un `assert len(kapso_enviados) == 2` pasa igual si el aviso de
  escalamiento fuera cualquier texto. Los asserts del aviso van contra las
  constantes de `app.mensajes` (o contra `AVISOS_DE_ESCALAMIENTO` de
  `tests/helpers.py`), nunca contra el resultado de llamar a
  `mensaje_escalamiento()` — comparar contra la propia función hace que los
  dos lados cambien juntos y el test no detecte nada.
- **`RelojFijo` (`tests/helpers.py`) para todo lo que dependa de la hora**, con
  instantes elegidos para que UTC y Buenos Aires no coincidan en qué rama
  corresponde. Así un `datetime.now()` sin convertir falla el test en vez de
  pasar de casualidad.

Correr con `pytest` desde `chatbot-polo/`.
