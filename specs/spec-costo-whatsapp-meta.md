# Spec: control preventivo de gasto de WhatsApp/Meta — etapa 1

No es parte de `specs/roadmap-bot-crm.md` (ese roadmap lo coordina Codex sobre
mejoras de producto del bot/CRM); esta entrega vino con su propio encargo,
directo sobre el flujo de envío y el costo. Se documenta acá con el mismo
criterio que el resto del repo: antes de cerrar una entrega, su contrato
queda en un spec propio.

## Objetivo de esta etapa

Medir, no todavía alertar ni bloquear. Concretamente:

1. Registrar correctamente los mensajes salientes aceptados por Meta.
2. Persistir el `wa_message_id` real que devuelve Meta.
3. Registrar un costo estimado por envío.
4. Poder calcular el consumo mensual estimado.
5. Exponerlo en el CRM.

**Todavía NO** hay alertas al 70%/90%, tope duro al 100% ni bloqueo de
envíos — eso es una etapa 2 posterior, condicionada a validar primero que
esta medición es correcta.

**El costo es una ESTIMACIÓN preventiva.** El sistema hoy no concilia contra
la factura real de Meta ni persiste los estados `sent`/`delivered`/`read`/
`failed` de `statuses[]` del webhook — ver "Fuera de alcance" más abajo.

## Flujo inspeccionado antes de implementar

- La única función que hace el POST a Meta es `MetaClient.enviar_mensaje_texto`
  (`app/meta.py`), y el único punto del código que la llama es
  `enviar_y_guardar` (`app/main.py`) — el mismo choke point para la respuesta
  del modelo, el aviso de escalamiento, el de límite alcanzado y el de error.
  No hay ningún otro lugar que mande texto por WhatsApp.
- Meta responde `{"messages": [{"id": "wamid...."}], ...}` en un 2xx (ver
  AGENTS.md/CLAUDE.md, "Contrato con la Cloud API de Meta"). Antes de esta
  entrega, `enviar_y_guardar` descartaba ese valor de retorno: el `Mensaje`
  del bot se guardaba con `wa_message_id=None` (pendiente conocido y
  documentado en CLAUDE.md, no un olvido).
- Si Meta responde error (4xx no reintentable, o los tres reintentos de
  429/5xx/red agotados), `enviar_mensaje_texto` relanza la excepción;
  `enviar_y_guardar` la atrapa, loguea y devuelve `False` sin guardar ningún
  `Mensaje` ni tocar la base. No hay ningún envío "fantasma" que contabilizar.
- Si Meta acepta pero el `db.commit()` posterior falla (motivo de DB, no de
  Meta): no había manejo especial antes de esta entrega — la excepción se
  escapa de `enviar_y_guardar` sin capturar, hacia el `except Exception`
  general de `responder()`/`agrupar_y_responder`, que la loguea como error
  inesperado. Esta entrega no cambia ese comportamiento (vino explícitamente
  fuera de alcance): sigue siendo un mensaje que Meta mandó de verdad pero
  que el sistema no llegó a registrar ni a contabilizar. Es una falla
  conservadora — subestima el costo, nunca lo infla — y queda anotada en
  PENDIENTES.md junto con el resto de la deuda conocida de esta clase (ver
  también la nota de `procesar_mensaje_entrante` en CLAUDE.md sobre el mismo
  tipo de gap del lado entrante).

## Condición exacta de "contabilizado"

Un envío se contabiliza si y solo si:

1. `meta_client.enviar_mensaje_texto(...)` no lanzó excepción (Meta aceptó el
   POST), **y**
2. la respuesta trae `messages[0].id` (`app.meta.extraer_wa_message_id`).

Si (1) falla, no se contabiliza nada — se corta en el `except` de
`enviar_y_guardar`, sin guardar `Mensaje` ni fila de costo, mismo
comportamiento que ya tenía el código. Si (1) pasa pero (2) no trae id (no
debería pasar según el contrato, pero no se confía a ciegas), se guarda el
`Mensaje` igual que antes de esta entrega (sin `wa_message_id`) pero **no**
se contabiliza costo — sin id no hay con qué correlacionar el envío, y
"evidencia suficiente" es justamente ese id.

Todo envío exitoso por `enviar_y_guardar` cuenta igual, sea la respuesta del
modelo o cualquiera de los avisos fijos (escalamiento, límite, error): los
cuatro pasan por el mismo chequeo (Meta aceptó el POST y devolvió un
`wa_message_id`) y se contabilizan de la misma forma preventiva. Es una
ESTIMACIÓN de gasto a partir de la aceptación del envío, no evidencia de
facturación efectiva — el sistema no concilia todavía contra la factura real
de Meta.

## Modelo de datos

**Se extiende `Mensaje`** (columna `wa_message_id` que ya existía, sin usar
para el bot) y **se agrega una tabla nueva**, `EnvioWhatsapp` /
`envios_whatsapp` (`app/models.py`), en vez de sumar más columnas de costo a
`Mensaje` — mismo criterio que ya usa este repo para costos/métricas
(`LlamadaIA`, ver specs/spec-dashboard-metricas.md): un evento de costo es
un concepto distinto del contenido del mensaje, y separarlo deja `Mensaje`
liviano y a `EnvioWhatsapp` libre para crecer en la etapa de `statuses[]`
sin tocar el historial de conversación.

Campos de `EnvioWhatsapp`: `conversacion_id`, `mensaje_id` (FK a `Mensaje`,
`UNIQUE` — un envío contabilizado por mensaje saliente), `wa_message_id`
(`UNIQUE`, ver "Duplicación" abajo), `categoria` (`"service"`, fija en esta
etapa), `tarifa_ars`, `costo_estimado_ars` (los dos grabados en la fila, no
recalculados después contra la config vigente — si `META_TARIFA_SERVICE_ARS`
cambia, el histórico tiene que seguir mostrando la tarifa que regía cuando
se mandó cada mensaje) y `creado_en`.

**Sin columna de estado.** Que la fila exista ya significa "Meta aceptó el
POST" — no hace falta un booleano `aceptado_por_meta` para decir lo mismo dos
veces. Se evitó a propósito nombrar nada `sent`/`enviado` que sugiera una
confirmación de entrega que el sistema no tiene: es "aceptado", no "entregado".

Migración: `scripts/migracion_costo_whatsapp.sql` (UP/DOWN, no corrida contra
ninguna base real). Es una tabla nueva — `create_all` (`app/db.py:init_db`)
la crea sola en cualquier base, no hace falta correr la migración para que
la app arranque — pero se documenta igual, mismo criterio que
`scripts/migracion_metricas_ia.sql`.

## Duplicación

Sin tope duro todavía, no hace falta reserva atómica de presupuesto (fuera
de alcance explícito de esta etapa). Pero el registro de envíos sí evita
duplicaciones obvias: `wa_message_id` es `UNIQUE` en `EnvioWhatsapp` (además
de en `Mensaje`, que ya lo era), así que un mismo id de Meta procesado dos
veces no puede generar dos filas de costo.

**Lo que esta entrega no resuelve** (documentado, no un olvido): si un
proceso muere a mitad de generar una respuesta y otro la retoma por
abandono (ver specs/spec-agrupamiento-mensajes.md, "Reinicios"), el proceso
viejo puede llegar a enviar la respuesta *después* de que el nuevo dueño ya
la reprocesó y la mandó también — dos POST aceptados por Meta, con dos
`wa_message_id` distintos y genuinos, y por lo tanto dos filas contabilizadas
en la estimación. Eso no es un `wa_message_id` duplicado (son dos ids
reales, dos envíos reales aceptados): la constraint `UNIQUE` no lo detecta
ni tiene por qué, y resolverlo de fondo excede el alcance de esta etapa ("no
inventar una solución compleja si el flujo actual ya evita duplicados" — el
flujo actual evita el caso simple, no este). Queda anotado como limitación
conocida.

## Configuración

`META_TARIFA_SERVICE_ARS` (Decimal, default `37.6798`) y
`META_PRESUPUESTO_MENSUAL_ARS` (Decimal, default `37679.80`) — `Decimal`, no
`float`: son montos en pesos que se suman fila por fila, y el error de
redondeo de un float se nota al acumular muchos envíos (ver `app/config.py`).
`validar_config()` exige que las dos sean mayores a 0, mismo criterio que ya
usa con `AGRUPAR_*` (positivo o corta el arranque, ver
`app/validacion_config.py`).

Todavía no se agregan `META_ALERTA_1_PORCENTAJE`, `META_ALERTA_2_PORCENTAJE`
ni `META_TOPE_DURO` — quedan para la etapa 2, condicionados a validar que el
número de esta etapa 1 es razonable primero.

## Cálculo del costo

Etapa 1: una sola categoría (`categoria = "service"`), una sola tarifa
(`META_TARIFA_SERVICE_ARS`), costo por mensaje aceptado = la tarifa
configurada. Sin multiplicadores ni distinción de
Marketing/Utility/Authentication (fuera de alcance explícito).

## Consumo mensual

`app.costo_meta.consumo_mensual(db, ahora)`: para el mes calendario de
`ahora` en `TIMEZONE` (`America/Argentina/Buenos_Aires`, mismo criterio que
`app.crm.metricas.rango_utc`), devuelve mensajes contabilizados, costo
acumulado, presupuesto configurado, porcentaje utilizado y saldo restante.
El cambio de mes ocurre solo por el rango de fechas de la consulta — nunca
se borra ni se resetea una fila, así que el histórico queda intacto para
siempre. No hay cron: cada consulta recalcula contra `envios_whatsapp` en el
momento.

## CRM

Sección nueva "WhatsApp / Meta" en `/crm/metricas`
(`app/crm/paginas/metricas.html`, `app/crm/estaticos/metricas.js`), reusando
el mismo endpoint `GET /crm/api/metricas` (agrega la clave `whatsapp_meta` al
`resumen()` de `app/crm/metricas.py`) — no un endpoint aparte. Muestra
mensajes contabilizados este mes, costo estimado acumulado, presupuesto
mensual, porcentaje utilizado, saldo restante, una barra de progreso simple y
el texto aclaratorio pedido: "Costo estimado. La conciliación exacta con
Meta requiere estados de entrega y facturación." Es un snapshot del mes
actual, no algo que se recorte por el `desde`/`hasta` del resto del
dashboard — mismo criterio que ya usa `estado_actual` en ese mismo endpoint.

Sin colores de alerta 70/90/100: la barra usa el degradé de marca
(`--degrade-ene`) siempre, porque esta etapa no define esos umbrales
todavía (ver `app/crm/estaticos/crm.css`).

## Fuera de alcance de esta etapa (explícito)

- Alertas al 70%/90%, tope duro al 100%, bloqueo de envíos.
- Procesar `statuses[]` completo del webhook. El modelo queda preparado para
  correlacionar por `wa_message_id` en una etapa futura (`EnvioWhatsapp`
  guarda ese id y el `mensaje_id`), pero no se persiste `delivered`/`read`/
  `failed` todavía — no hay ninguna columna ni fila para eso.
- Cálculo de Marketing/Utility/Authentication.
- Conciliación exacta con la factura de Meta.
- Emails, agrupamiento, exportación de conversaciones, prompt: sin tocar.

## Tests

`tests/test_costo_whatsapp.py`: envío exitoso persiste `wa_message_id` y
contabiliza una vez con tarifa/costo; error de Meta no contabiliza; dos
envíos exitosos suman correcto; cambio de mes contabiliza solo el mes
consultado; porcentaje utilizado correcto contra el presupuesto; sin
imprecisión evidente en ARS (Decimal); `wa_message_id` duplicado no genera
doble fila de costo; y que `GET /crm/api/metricas` devuelva los campos
nuevos de `whatsapp_meta`. Sin llamadas reales a Meta ni a producción, mismo
criterio que el resto de la suite (`meta_enviados` en `tests/conftest.py`,
ahora con un `wa_message_id` distinto por llamada — ver el comentario ahí
sobre por qué hacía falta).
