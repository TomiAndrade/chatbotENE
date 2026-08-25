# Pendientes — Bot WhatsApp ENE IA LAB

Todo lo que queda por hacer, al 31/07/2026, con la etapa 1 cerrada y la etapa 2
implementada y con tests.

Lo importante primero: **del `spec-etapa2.md` no quedó nada sin implementar en
código**. Los 62 tests pasan. Lo que falta para poder cerrar la etapa 2 como se
cerró la 1 es la validación contra los servicios reales (punto 1 de acá abajo).

Ordenado por lo que conviene hacer antes, no por importancia absoluta.

---

## 1. Validar la etapa 2 contra servicios reales — bloqueante

La suite de `tests/` mockea Kapso y mockea el proveedor de IA. Eso confirma que
el wiring interno está bien (historial, escalamiento, límite, manejo de
errores), pero **todavía no se mandó un solo mensaje real por `openai_compat`
ni por Claude**, ni se hizo una vuelta completa por WhatsApp con IA de verdad.

De los 8 criterios de aceptación del `spec-etapa2.md`, sólo el 8 ("los tests
pasan") está verificado. Los otros siete hay que probarlos a mano, celular →
sandbox de Kapso → ngrok → servidor, igual que en la prueba end-to-end de la
etapa 1:

1. Se escribe "hola" y el bot se presenta como asistente de ENE, **no** del
   IA LAB.
2. Se pregunta el precio de la membresía individual y responde $85.000, corto y
   sin markdown.
3. Se pregunta cuánto sale la sala de reuniones y **responde** (USD 100 la
   jornada, hasta 16 personas, + IVA) **sin escalar**. Después se pide
   reservarla para una fecha y ahí sí **escala**: `modo_humano` en `true`,
   resumen guardado, y llega el aviso que corresponde al horario.
4. A partir de ahí el bot **no responde más** en esa conversación.
5. Se pregunta una receta de cocina y redirige **sin** escalar.
6. Se pregunta algo relacionado pero ausente del knowledge base y **escala** en
   vez de rechazarlo como fuera de tema. Elegir el ejemplo contra el KB del
   día: creció mucho y varios huecos viejos ya no lo son. Sirven el bicicletero
   o la acústica de la sala de podcast.
7. Se pregunta cuánto sale un seat **por día** —el único CONSULTAR de la
   sección 11— y **no inventa** un número, teniendo el precio del seat mensual
   y el de la oficina por día al lado en la misma tabla.
8. Se pregunta qué actividades hay este mes y **no escala**: manda a la web del
   IA LAB o al Instagram de ENE, sin inventar fechas.

El punto 6 es el que más suele fallar. Si el bot lo rechaza como fuera de tema,
el problema está en el prompt, no en el código. El 7 es el nuevo candidato:
tener precios cerca del hueco invita a interpolar mucho más que no tener
ninguno.

Estos criterios se revisaron en agosto de 2026, cuando la ronda de datos de ENE
llenó la sección 11 del knowledge base. Los viejos 3 y 7 se aprobaban con el
comportamiento equivocado: el 3 pedía escalar ante cualquier consulta de
alquiler, y el 7 daba por buena la respuesta si el bot **no** decía el precio de
una oficina, que ahora sabe.

Para correr la prueba hace falta:

- Un proveedor con IA de verdad en `.env`. Hay dos caminos:
  - `PROVEEDOR_IA=openai_compat` con `BASE_URL`, `MODELO` y
    `OPENAI_COMPAT_API_KEY` (los valores de `.env.example` apuntan a
    OpenRouter con el free de Nemotron — ver la sección 2 sobre su tasa de
    fallos).
  - `PROVEEDOR_IA=claude` con `ANTHROPIC_API_KEY` y `MODELO` (Haiku).

  **Ojo:** `.env.example` trae `PROVEEDOR_IA=fijo`, que es el valor correcto
  para desarrollar y para los tests, pero con `fijo` el bot contesta el texto
  de la etapa 1 y ninguno de los 7 puntos se cumple. Ya no existe un proveedor
  `gemini`: se reemplazó por `openai_compat`, que cubre cualquier endpoint con
  formato de la API de OpenAI.
- `uvicorn app.main:app --reload --port 8000` y `ngrok http 8000` (puerto 8000,
  no 3000).
- La URL de ngrok configurada como webhook en el sandbox de Kapso.

No requiere cambios de código.

---

## 1.b. Avisarle al equipo cuando el bot escala — bloqueante de producción

**Hoy nadie se entera de un escalamiento.** El bot le dice al usuario *"tu
consulta pasó a una persona del equipo, en breve te responden por acá"*, prende
`modo_humano` y escribe un `WARNING` en el log. Eso es todo. Si nadie está
mirando el log o el panel de Kapso, la conversación queda muerta con una
promesa hecha. El bot **no puede salir a producción así**: es peor que no tener
bot, porque el usuario se queda esperando.

**Decidido: se notifica por mail, no por WhatsApp.** Mandarle un WhatsApp al
equipo parece lo natural porque la plomería de envío ya está hecha, pero un
mensaje iniciado por el negocio fuera de la ventana de 24 horas necesita
plantilla aprobada por Meta y **se cobra por conversación**: sería pagar por
cada escalamiento, más el trámite de aprobación de la plantilla. El mail no
cuesta nada ni tiene plantillas que aprobar. Que quede escrito para no volver a
discutirlo.

Lo que hace falta:

- Un módulo de mail con las credenciales SMTP en `.env` (host, usuario,
  password y casilla destino), fuera del repo como el resto de las claves.
- El mail se manda **después** del commit de `modo_humano`, nunca antes, y su
  fallo **no puede romper el escalamiento** — mismo orden y mismo criterio que
  el aviso al usuario (ver "El escalamiento no es atómico y el orden importa"
  en `CLAUDE.md`). Si el SMTP falla, se loguea diciendo qué se perdió.
- Contenido mínimo del mail: el número del usuario, el resumen que dejó el
  modelo, y la hora. Quien atiende tiene que poder ir al panel de Kapso y
  encontrar la conversación sin leerla entera.
- **No se da por terminado con tests.** Es una integración externa: hasta que no
  llegue un mail de verdad a la casilla, está mockeado nomás.

Esto agranda el alcance: `spec-etapa2.md` lista "notificaciones al equipo cuando
se escala" como explícitamente fuera de alcance. Se decidió sumarlo en agosto de
2026 al ver que sin esto el escalamiento no existe en la práctica.

Sigue abierto **quién** es esa persona y con qué casilla. El horario ya no:
es 8–18 de lunes a viernes, unificado con el del edificio (`app/mensajes.py`).

---

## 2. Hallazgos del spike de tool calling contra OpenRouter (31/07/2026)

Antes de sumar un proveedor nuevo se corrió un **spike descartable, fuera de
este repo** (`~/spike`, no versionado), que pega a la API de OpenRouter con
`nvidia/nemotron-3-ultra-550b-a55b:free` en formato OpenAI, declarando la
herramienta `escalar_a_humano`. Se hicieron tres tandas: `tool_choice` forzado,
`tool_choice: "auto"` con un system prompt corto de prueba, y `auto` con el
**system prompt real del bot** (armado con el propio `app/prompt.py`, no una
copia).

**Ya está todo implementado en `app/proveedor_openai_compat.py`.** Esta sección
queda como registro de dónde salió cada decisión: lo que sigue describe lo que
el spike encontró y cómo se resolvió, no trabajo pendiente.

### Lo que quedó validado

- **El contrato de tool calling en formato OpenAI funciona.** Llega
  `tool_calls`, `function.name` es `escalar_a_humano` y `function.arguments`
  trae la clave `resumen`.
- **`function.arguments` viene como string con JSON adentro, no como objeto** —
  requiere un `json.loads` del lado del proveedor. Es distinto de Claude, donde
  el SDK ya entrega un dict (`bloque.input`).
- **Con el prompt real, el escalamiento se dispara** en el criterio de
  aceptación 3 (alquiler de sala). La respuesta vino con `content: None` —o sea
  que respetó *"No anuncies que vas a escalar ni escribas un mensaje de
  despedida"*— y el resumen reflejaba la regla 10 del prompt: *"El polo ofrece
  este servicio pero el bot no tiene esos datos"*.

  **Ojo al releer esto:** el spike corrió contra el KB de julio, cuando el bot
  no tenía ningún precio de espacios y escalar era la única respuesta posible.
  Con el KB de agosto ese mismo mensaje **no debe escalar**: tiene que
  responder el precio. El criterio 3 se reescribió en `spec-etapa2.md` y ahora
  distingue preguntar el precio (responde) de pedir una reserva (escala). Lo
  que este hallazgo sigue validando es el contrato de tool calling, no el
  comportamiento esperado ante esa pregunta.

### Cambios que hubo que hacer para sumar el proveedor OpenAI-compatible

- **OpenRouter devuelve errores adentro de un HTTP 200.** Un fallo llegó como
  `200 OK` con body `{"error":{"message":"Internal error...","code":502}}`.
  Era un agujero real en el manejo de errores: `con_un_reintento`
  (`app/respuesta.py`) reintenta ante **cualquier excepción**, y un 200 no
  levanta ninguna en un cliente HTTP — así que no había reintento, el parseo no
  encontraba `choices` y terminaba como respuesta vacía. El chequeo tenía que
  ser sobre la **clave `error` del body**, no sobre el código HTTP.
  > Ojo con el matiz al leer el código: el reintento por status (429 y 5xx) es
  > `_conviene_reintentar` en `app/kapso.py` y aplica a los **envíos a
  > Kapso**, no a la llamada al modelo. Son dos mecanismos distintos.

  **Resuelto:** `ProveedorOpenAICompat._llamar` chequea la clave `error` del
  body y levanta `ErrorTransitorioProveedor`. La sección "Manejo de errores"
  de `spec-etapa2.md` ya lo refleja, y está cubierto por
  `tests/test_proveedor_openai_compat.py::test_error_adentro_de_un_200_levanta_error_transitorio`.
- **El `resumen` puede venir mal formado.** Si el tool call llegó pero
  `arguments` no parsea como JSON, o parsea pero no trae `resumen`, **igual hay
  que escalar** (`escalar=True`, `resumen=None`): un `JSONDecodeError` no puede
  tumbar el request. Que el resumen se pierda es peor para quien atienda, pero
  no perder el escalamiento es lo que importa.

  **Resuelto:** `_interpretar_respuesta` atrapa el `JSONDecodeError` y escala
  con `resumen=None`. Cubierto por
  `tests/test_proveedor_openai_compat.py::test_arguments_mal_formado_escala_igual_con_resumen_none`.

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
- **Distinguir el error transitorio del proveedor del resto, al menos en
  desarrollo.** Un error transitorio debe responder "problema técnico, probá de
  nuevo" y **no** prender `modo_humano`. En producción con un proveedor pago,
  escalar sigue siendo lo correcto — la regla original no estaba mal, le
  faltaba el caso de desarrollo.

  **Resuelto:** `ErrorTransitorioProveedor` (`app/respuesta.py`) más la rama
  por `DEBUG` en `main.responder`. Cubierto por
  `tests/test_error_transitorio.py`.
- **Hace falta un script de reset de `modo_humano` por teléfono.** No es un
  nice-to-have: los criterios de aceptación 5, 6 y 7 **no se pueden probar sin
  él**, porque el criterio 4 (el bot deja de responder tras escalar) corta la
  conversación. Con la tasa de fallos de arriba se va a usar seguido.

  **Resuelto:** `scripts/resetear_modo_humano.py <telefono>`.

### Costo del prompt y elección de proveedor

El system prompt real (`system-prompt.md` + `knowledge-base.md`) eran **37.517
caracteres = 10.371 tokens**, con 36 bloques `[PENDIENTE]`. **Veníamos
estimando ~3.000 tokens**, o sea más del triple. Eso reordena las opciones:

| Proveedor | Efecto |
|---|---|
| Groq | **Queda descartado.** Con un TPD de 100–200K son 10 a 20 llamadas por día. |
| DeepSeek | El bono de 5M tokens rinde **~470 llamadas, no ~1.600**. |
| OpenRouter | **No se ve afectado**: su límite es por request, no por token. |

Y siguió creciendo, como estaba previsto. Tras la ronda de agosto de 2026 (los
precios de espacios, las condiciones comerciales y todo lo de la sección 3) el
prompt está en **56.079 caracteres**: 18.562 más, un **+50%**. La medición de
tokens de arriba fue real y ésta no — aplicando el mismo ratio de aquella
(3,617 caracteres por token) da **~15.500 tokens**, y el bono de DeepSeek
bajaría de ~470 llamadas a **~320**. Hay que volver a medirlo de verdad contra
el proveedor que se termine usando, no arrastrar la regla de tres.

Completar los `[PENDIENTE]` que quedan lo agranda todavía más. Sigue siendo
prefijo cacheable, así que el crecimiento es barato **si el caché funciona** —
lo que refuerza la prioridad de medir la tasa real de cache hit, no darla por
sentada.

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
reconfirmar contra el proveedor que se termine usando y la vuelta completa por
WhatsApp. El spike corrió fuera del repo, contra un script suelto; que ahora
exista `PROVEEDOR_IA=openai_compat` (que sí cubre Nemotron vía OpenRouter, y
cualquier otro endpoint con formato OpenAI) no reemplaza esa prueba: es código
distinto del que se spikeó.

---

## 3. Completar los bloques `[PENDIENTE]` del knowledge base — con el equipo del polo

`prompts/knowledge-base.md` tiene los huecos marcados a propósito: le dicen al
modelo qué no sabe, que es exactamente lo que necesita para escalar en vez de
inventar. Están bien así para desarrollar, pero **antes de producción hay que
completarlos con el equipo**, porque cada uno es una consulta que hoy termina en
un humano.

**Ronda de agosto 2026 — una parte importante ya se completó.** El equipo de ENE
respondió por mail y de ahí salieron: horario real (8–18, sin feriados), acceso
por FACE ID, la tabla completa de precios y equipamiento de los ocho espacios
(sección 11), las condiciones comerciales y de facturación (sección 12 —
sección nueva), domicilio postal, política de logos, el circuito para organizar
eventos y la cafetería. La lista de decisiones que se aplicó vivía en
`cambios-kb-y-prompt.md`, ya borrado; lo que sobrevive de ese documento está en
el KB: los pendientes en su checklist final y, al pie, la tabla de **datos que
quedan deliberadamente afuera** (estructura societaria, link del grupo de
WhatsApp, mails de comprobantes, representante legal, nombres de las
plataformas de firma y facturación). Esa tabla existe para que nadie los agregue
más adelante "completando huecos": son decisiones, no olvidos.

Un bloque sigue vacío:

- **Agenda de eventos del laboratorio** (sección 8) — no hay fuente. Existe un
  Google Calendar público; falta definir si el bot lo lee (integración, fuera de
  alcance por ahora) o si se carga a mano, y quién avisa cuando hay un evento
  nuevo. La otra mitad de la sección 8 —organizar un evento en ENE— quedó
  resuelta: el bot informa espacios y deriva a coordinacionenepctnqn@gmail.com.

Y hay huecos puntuales en el resto:

| Tema | Qué falta confirmar |
|---|---|
| Identidad | Cómo debe nombrarse el bot al presentarse ("ENE", "Polo Tecnológico Neuquén", otra forma) |
| Institucional | Misión o descripción de ENE como polo (hoy sólo está la del laboratorio); si hay otras iniciativas además del IA LAB; si la estructura societaria es información pública o la explica una persona |
| Acceso | Si piden DNI en recepción |
| Membresías | Si se puede dar de baja en cualquier momento o el compromiso es por el ciclo completo; si las 3 personas de la Corporativa son fijas o rotan; si al ingresar a mitad de ciclo se paga desde el mes que entra o hay ajuste |
| Pagos | Datos bancarios / CBU o alias (ver la nota de abajo) |
| Precios en pesos | **Quién actualiza** los `$85.000` / `$150.000` de las membresías. Se decidió que el bot los diga a secas, sin aclarar vigencia —es la respuesta más útil y la más natural para WhatsApp—, y el costo aceptado es que el día del aumento el bot siga dando el viejo con total seguridad hasta que alguien edite el KB. No puede detectarlo solo: no tiene reloj. Falta la persona a cargo. Los precios en dólares no tienen este problema (se facturan al dólar BNA del día). |
| Postulación | Plazo aproximado de respuesta tras enviar el formulario |
| Espacios | Valor del **seat por día** (figura como CONSULTAR); si la **sala de podcast** tiene tratamiento acústico o algo que la diferencie de la de reuniones —es la pregunta natural de quien compara USD 140 por 6 personas contra USD 100 por 16—; equipamiento real de la **oficina privada por día** (el documento fuente parece tener un copy-paste); costo del **domicilio postal** |
| Cowork | Si el horario es 8–18 como el resto del edificio (el KB lo unificó por decisión; el 9–17 anterior venía de la web y nadie lo confirmó para el cowork); si se reserva con anticipación; si las 2 veces por semana son días fijos o los elige el miembro; si el miembro de IA LAB accede a todos los servicios del cowork o sólo a escritorio y wifi |
| Actividades | Cuáles son las actividades abiertas al público y cómo enterarse |
| Cafetería | Teléfono de contacto (el bot escala hasta tenerlo); si **The Coffee Store** y **Work Café** son el mismo lugar o dos cosas distintas —el KB usa los dos nombres— |
| Mails | `info@eneneuquen.com.ar` quedó **obsoleto** pero sigue publicado en la web de ENE. El bot ya no lo da (usa `recepcion.ene.pctnqn@gmail.com`), pero la gente lo va a seguir usando y esos mensajes no los lee nadie. Conviene que ENE lo baje de la web o lo redirija. |
| Referentes | Si el bot puede entregar el mail del referente de una vertical. **Requiere decisión de ENE, no un dato**: el bot no puede verificar que alguien sea miembro, así que entregar mails de personas nombradas a terceros no verificados choca con la regla 4 y con la Ley 25.326. Las tres opciones planteadas están en la sección 14 del KB. Hasta que se resuelva, no se dan contactos de referentes. |
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

## 7. Endurecer el chequeo de respuesta vacía — resuelto

**Aplicado.** `app/main.py` chequeaba `resultado.texto is None`; un texto de
sólo espacios pasaba ese chequeo y cala hasta el `if resultado.texto:` de más
abajo sin escalar. La condición ahora es
`if (not resultado.texto or not resultado.texto.strip()) and not resultado.escalar`,
que cierra el caso. Cubierto por
`tests/test_fallo_modelo.py::test_respuesta_de_solo_espacios_sin_escalar_tambien_se_trata_como_error`.

---

## 8. Deuda técnica menor

Anotada, no implementada. Ninguna rompe nada hoy; están acá para que no haya
que redescubrirlas.

- **Feriados.** Se tratan como día hábil, así que un 25 de mayo a las 11 el bot
  promete que responden "en breve". Está declarado como deuda conocida en el
  spec y anotado en `app/mensajes.py`.
- **`@app.on_event` está deprecado** en la versión de FastAPI del proyecto
  (`app/main.py`); la suite lo muestra como `DeprecationWarning` en cada
  corrida. Lo que corresponde es un handler de `lifespan`. Funciona igual, pero
  el warning va a seguir apareciendo y en algún momento se va a romper.
- **El camino público de `openai_compat` no está cubierto por tests.**
  `tests/test_proveedor_openai_compat.py` ataca `_interpretar_respuesta` y
  `_llamar` por separado, nunca `generar_respuesta`. Queda sin verificar que el
  mensaje `system` con el `SYSTEM_PROMPT` efectivamente se mande, que `tools`
  viaje en el payload, y que `_a_mensaje_openai` mapee bien los roles —
  incluido el prefijo `[Respuesta de una persona del equipo]`. Si alguien borra
  la línea del system prompt, la suite queda verde y el bot pierde todo el
  knowledge base. `tests/test_proveedor_claude.py` sí cubre el equivalente del
  lado de Claude y sirve de modelo para escribirlo.
- **`ProveedorClaude` nunca levanta `ErrorTransitorioProveedor`.** Sólo
  `openai_compat` clasifica 429/5xx como transitorios. Con
  `PROVEEDOR_IA=claude` y `DEBUG=true`, un `overloaded_error` de Anthropic
  escala a humano en vez de pedir que se reintente. En producción no cambia
  nada (ahí escalar es lo correcto, y es lo que hace), así que sólo molesta si
  se desarrolla contra Claude.
- **`con_un_reintento` reintenta fallos deterministas.** Reintenta ante
  **cualquier** excepción, incluido un 401 por API key inválida, que va a
  fallar igual las dos veces. `app/kapso.py` sí distingue con
  `_conviene_reintentar`; la capa del modelo no. Cumple el spec ("no reintentar
  más de una vez") pero gasta una request y duplica la latencia en fallos que
  no pueden salir distinto.
- **Se guarda en la base el texto sin truncar.** `KapsoClient` trunca a 4096
  caracteres antes de enviar (`app/kapso.py`), pero `enviar_y_guardar` persiste
  el `texto` completo. La base —y por lo tanto el historial que vuelve al
  modelo— contiene texto que el usuario nunca vio. Con el prompt pidiendo
  respuestas cortas es improbable, pero la divergencia está.
- **El aviso de límite depende de una igualdad exacta.**
  `main.procesar_mensaje_entrante` dispara el aviso sólo cuando
  `conteo_ultima_hora == config.limite_mensajes_hora + 1`. Si dos mensajes
  cruzan el límite concurrentemente y ambos cuentan lo mismo, nadie ve el `+1`
  exacto y **el aviso no se manda nunca**: silencio total sin explicación,
  contra lo que pide el spec ("Responder una vez avisando"). Con SQLite no se
  reproduce porque serializa las escrituras; **con Postgres en producción el
  escenario se abre**. La forma robusta sería un `>=` con un flag persistido de
  "ya avisé en esta ventana".

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
