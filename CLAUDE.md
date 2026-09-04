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

El proyecto está planificado en etapas. `spec-etapa1.md`, `spec-etapa2.md` y
`spec-meta-cloud-api.md` son los specs completos de lo implementado y mandan
sobre este archivo si algo se contradice.

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

**Migración de Kapso a la Cloud API de Meta (spec-meta-cloud-api.md):
código completo y con tests, sin validar contra el panel real de Meta.**
`app/meta.py` reemplaza a `app/kapso.py` como cliente activo; `app/kapso.py`
y sus tests quedan en el repo, sin usarse, hasta esa validación. Falta
probar contra developers.facebook.com: el GET de verificación del webhook,
un mensaje real de ida y vuelta, y la firma `X-Hub-Signature-256` con un App
Secret real.

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
- **Cloud API de Meta** (developers.facebook.com/docs/whatsapp), conexión
  directa desde `app/meta.py` — sin intermediario. Hasta agosto 2026 el bot
  usaba **Kapso** (https://docs.kapso.ai) como capa sobre esa misma API;
  `app/kapso.py` y sus tests siguen en el repo, sin usarse, hasta validar
  Meta en producción (ver spec-meta-cloud-api.md y la sección de abajo).
- **anthropic** (Claude, producción) y **`openai_compat`** (desarrollo: un solo
  proveedor parametrizado por `BASE_URL`, sirve para cualquier endpoint con
  formato de la API de OpenAI — OpenRouter, DeepSeek, el free de NVIDIA, un
  modelo local) como proveedores de IA intercambiables por `PROVEEDOR_IA`
- **tzdata**: necesario en Windows para que `zoneinfo` resuelva
  `America/Argentina/Buenos_Aires` (no hay base de tz del sistema operativo)

## Contrato con la Cloud API de Meta (verificado contra la doc, no asumido)

Vigente desde la migración de Kapso (spec-meta-cloud-api.md). `app/meta.py`
es la implementación; `app/kapso.py` queda con el contrato viejo, documentado
más abajo, sin usarse.

- Enviar texto: `POST https://graph.facebook.com/{META_API_VERSION}/{phone_number_id}/messages`,
  header `Authorization: Bearer {token}`, payload
  `messaging_product`/`recipient_type`/`to`/`type`/`text.body` — idéntico al
  de Kapso. Responde con `messages[0].id`.
- Webhook entrante: payload con envelope `entry[].changes[].value`. Un solo
  POST puede traer varios `entry`/`changes`, cada uno con varios `messages[]`
  (de personas distintas incluso) — hay que iterar todo, no asumir un solo
  mensaje. Los campos son `messages[].id`, `messages[].from`,
  `messages[].type`, `messages[].text.body`. Cuando el evento es de entrega
  en vez de mensaje, `value` trae `statuses[]` en lugar de `messages[]`: se
  descarta explícitamente (chequeo positivo de que `messages` está, no por
  descarte) y se responde 200 igual — un 4xx/5xx hace que Meta reintente y,
  si se repite, que desuscriba el webhook.
- Verificación de la URL del webhook: Meta manda un `GET /webhook` con
  `hub.mode`, `hub.verify_token` y `hub.challenge` antes de empezar a mandar
  eventos. Si `hub.mode == "subscribe"` y el token coincide con
  `META_VERIFY_TOKEN`, hay que devolver `hub.challenge` como texto plano
  (`verificar_webhook` en `app/main.py`).
- Firma: header `X-Hub-Signature-256`, valor `sha256=<hex>` — HMAC-SHA256 del
  body crudo con `META_APP_SECRET` (el App Secret de la app de Meta, no un
  secreto que se configure aparte). Hay que sacar el prefijo `sha256=` antes
  de comparar (`verificar_firma_webhook` en `app/meta.py`).
- **Números argentinos: no normalizar.** `messages[].from` viene con el "9"
  presente o no, según cómo Meta lo resuelva internamente — no siempre
  coincide con cómo la persona tiene guardado el número. Se guarda y se
  responde con ese valor exacto, verbatim; normalizarlo duplica contactos o
  rompe el envío.

### Contrato con Kapso (histórico, `app/kapso.py` sin usarse)

- Enviar texto: `POST https://api.kapso.ai/meta/whatsapp/v24.0/{phone_number_id}/messages`,
  header `X-API-Key`, mismo payload que arriba. Responde con `messages[0].id`.
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
- **El endpoint `POST /webhook` no toca la base ni la red.** Verifica firma,
  parsea y encola; dedup, guardado y respuesta corren en la background task
  `procesar_mensaje_entrante`, que Starlette ejecuta en threadpool después de
  haber mandado el 200. Consecuencia: los fallos de procesamiento se ven en el
  log, no como error en el panel de Meta. `GET /webhook` (challenge de
  verificación) es la excepción: responde en el mismo request, no hay nada
  que encolar.
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
  Meta son normales, no un error.
- **Se reintenta solo lo que puede salir distinto**: errores de red, 429 y 5xx,
  con backoff 1s → 2s. Los demás 4xx fallan al primer intento y loguean el body
  de la respuesta de Meta, que es donde viene el motivo real. Misma lógica
  duplicada en `app/meta.py` y `app/kapso.py` (histórico) — el spec de la
  migración pidió copiar y adaptar, no compartir código entre los dos.
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
  `ESCALADO A HUMANO`, y recién al final el aviso al usuario. Si Meta está
  caído el escalamiento ya ocurrió igual, así que tiene que quedar en el log
  sí o sí; el fallo del aviso se loguea aparte y diciendo qué se perdió, no
  como un error de envío genérico.
- **La pausa por intervención manual (`procesar_mensaje_saliente`,
  `registrar_intervencion_humana`) quedó dormida con la migración a Meta.**
  Dependía de que Kapso mandara `whatsapp.message.sent` con
  `message.kapso.direction`/`origin`, algo que solo existía en modo
  coexistencia de Kapso — la Cloud API de Meta no tiene ese evento ni ese
  campo (spec-meta-cloud-api.md, sección 5). El filtro que decidía si un
  evento así venía de la secretaría o del propio bot vivía inline en el
  `POST /webhook` viejo y se borró junto con el resto del ruteo por
  `X-Webhook-Event` — no tiene sentido bajo el payload de Meta. Las dos
  funciones siguen enteras y sus tests las llaman directo
  (`tests/test_pausa_humana.py`), por si más adelante Meta habilita
  Coexistence u otra bandeja dispara este flujo de nuevo.
- **Un bug propio no se puede tragar un mensaje en silencio, ni en el
  webhook ni en la background task.** Dos puntos separados, los dos en
  `app/main.py`:
  - `POST /webhook` distingue payload raro de bug propio. Un `json.JSONDecodeError`
    o una excepción de forma (`AttributeError`/`TypeError`/`KeyError` al
    navegar `entry`/`changes`/`value`) es un dato de entrada inesperado, no
    un bug: `WARNING` y 200. Cualquier otra excepción es sospechosa de ser
    un bug propio: `ERROR` con `exc_info=True` (traceback completo) y el
    body crudo recortado a 2000 caracteres (`_cuerpo_para_loguear`), 200
    igual — Meta no puede saber que algo salió mal del lado del servidor.
  - `procesar_mensaje_entrante` tiene un `except Exception` propio, además
    del `except IntegrityError` puntual del commit. Starlette corre las
    background tasks *después* de mandar la respuesta HTTP
    (`Response.__call__`: `send` del 200, recién después `await
    background()`), así que para cuando algo revienta acá ya no hay
    respuesta que cambiar — y sin este catch, la excepción se escapa hacia
    el runner de background tasks de Starlette y termina en el logger de
    uvicorn, no en `logger("bot")`: sin `identificador_externo` ni
    `wa_message_id`, sin la garantía de que alguien lo esté mirando. Se
    verificó leyendo el código de Starlette 1.3.1
    (`starlette.background.BackgroundTasks.__call__`,
    `starlette.middleware.errors.ServerErrorMiddleware.__call__`), no
    asumido.
- **`prompts/system-prompt.md` y `prompts/knowledge-base.md` se leen una sola
  vez al importar `app/prompt.py`** (no en cada mensaje). Los bloques
  `[PENDIENTE]` del knowledge base se dejan tal cual a propósito: le indican al
  modelo qué no sabe, que es lo que necesita para escalar en vez de inventar.
- **El knowledge base ya no es un esqueleto con huecos.** En agosto de 2026
  ENE respondió una ronda de consultas y se completó casi todo: precios,
  capacidad y equipamiento de los ocho espacios, condiciones comerciales y de
  facturación, acceso por FACE ID, horario real. Quedan 10 bloques
  `[PENDIENTE]` (eran 42) y el prompt pasó de 37.517 a ~66.000 caracteres.
  **Consecuencia que rompe supuestos viejos: el bot ahora informa precios de
  espacios y sólo escala para reservar.** Cualquier nota anterior que diga
  "alquiler de espacios → escala siempre" está desactualizada. Lo mismo con
  la agenda de eventos: ya no escala, deriva a la web del IA LAB o al
  Instagram de ENE.
- **El horario de atención es 8–18, de lunes a viernes, en un solo lugar.**
  `app/mensajes.py` lo define en `HORARIO_ATENCION_DESDE`/`HASTA` y el texto
  del aviso fuera de horario lo interpola desde ahí. Estuvo hardcodeado como
  "de 9 a 17" en el mensaje y quedó desincronizado del knowledge base cuando
  el horario real cambió: el bot decía un horario si se lo preguntaban y otro
  al escalar. No volver a escribirlo a mano.

## Reglas del proyecto

- **El bot nunca envía datos bancarios, CBU ni alias.** En ninguna etapa. Es una
  regla del proyecto, no una omisión temporal.
- `.env` y `bot.db` van en `.gitignore`. Ninguna clave hardcodeada, tampoco en
  comentarios ni en tests.
- Sin `META_APP_SECRET`, el webhook queda abierto a cualquiera que conozca
  la URL. Por eso solo se deja pasar con `DEBUG=true`; con `DEBUG=false` se
  rechaza todo con 401. Ojo con el default — ver el checklist de deploy.
- El código lo lee y mantiene un estudiante de Ciencias de la Computación con
  base en Java y Python básico: **legible y explicable por sobre ingenioso**,
  claridad antes que concisión.

## Checklist de deploy

El deploy está fuera de alcance por ahora, pero esto hay que resolverlo antes:

- **Poner `DEBUG=false` y `META_APP_SECRET` con valor**, además de
  `META_PHONE_NUMBER_ID`, `META_ACCESS_TOKEN` (el token permanente del System
  User, no el temporal de 24hs) y `META_VERIFY_TOKEN`. El default de
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
- **Que alguien se entere cuando el bot escala.** Hoy no pasa nada: se prende
  `modo_humano` y se escribe un `WARNING` que nadie mira, mientras al usuario
  se le prometió que "en breve te responden". Decidido que el aviso va **por
  mail, no por WhatsApp**: un mensaje iniciado por el negocio fuera de la
  ventana de 24 horas necesita plantilla aprobada por Meta y se cobra por
  conversación, o sea que sería pagar por cada escalamiento. Ver PENDIENTES.md
  sección 1.b.

## Etapas siguientes

Con la etapa 2 (LLM, historial, escalamiento) y la migración a la Cloud API
de Meta (spec-meta-cloud-api.md) implementadas, no queda un "etapa 3"
definida en un spec propio — lo que sigue es validar contra servicios reales
(ver "Estado" arriba: la migración a Meta tampoco se probó todavía contra el
panel real, solo con la suite de tests) y, más adelante, lo que ya estaba
fuera de alcance:

Fuera de alcance hasta que se diga lo contrario: feriados (se tratan como día
hábil), deploy, transcripción de audios, mensajes con botones/listas/
plantillas, y devolver una conversación de humano a bot automáticamente (se
desmarca `modo_humano` a mano en la base).

## Tests

`tests/` con pytest, 103 tests. No pegan a ninguna API real: Meta se mockea
(`meta_enviados`, fixture en `tests/conftest.py`) y el proveedor de IA se
mockea por test parcheando `app.main.generar_respuesta` (`fijo` no necesita
mock); los dos proveedores con IA se prueban con dobles (`httpx.MockTransport`
para `openai_compat`, un cliente falso para `claude`). Usan una base SQLite en
un directorio temporal, no `bot.db`. Cubren: flujo completo del webhook,
matriz de firma y challenge de Meta, statuses[] descartado, varios mensajes
en un mismo entry, reintentos de Meta, carrera de entregas concurrentes con
hilos, armado de historial y corte por antigüedad, mensaje de escalamiento
según horario, escalamiento por tool calling, fallo del modelo, error
transitorio del proveedor, límite por número, parseo de tool calls de los
dos proveedores, y `app/kapso.py` (histórico, sin usarse) con su propia
matriz de firma y reintentos.

Dos cosas al escribir tests acá, aprendidas de una revisión en la que los
tests pasaban por un vacío:

- **Afirmar el contenido de lo que se envió, no sólo cuántos mensajes
  salieron.** Un `assert len(meta_enviados) == 2` pasa igual si el aviso de
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
