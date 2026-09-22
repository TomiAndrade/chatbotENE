# Spec: bloqueo duro mensual de gasto de WhatsApp/Meta — etapa 2.1

Continúa specs/spec-costo-whatsapp-meta.md (etapa 1: medir, sin bloquear).
Esta entrega agrega el mecanismo de bloqueo — **solo el mecanismo**, apagado
por default. Las alertas al 70%/90% y cualquier cambio visual del CRM quedan
para entregas posteriores, explícitamente fuera de alcance acá.

## Objetivo de esta entrega

Bloqueo duro mensual seguro ante concurrencia: que múltiples envíos
concurrentes (distintas conversaciones, procesadas en paralelo por el
threadpool de background tasks, ver app/main.py) nunca puedan hacer que el
gasto comprometido del mes supere el presupuesto configurado.

**No** implementa todavía:
- Alertas al 70%/90%.
- Ningún cambio en `/crm/metricas` ni en el endpoint `GET /crm/api/metricas`.
- Conciliación `sent`/`delivered`/`read`/`failed`.
- Recuperación automática de reservas huérfanas (ver "Limitación conocida"
  más abajo).
- Colas de reintento para un mensaje bloqueado.

## Por qué no "SUM -> comprobar saldo -> enviar -> registrar"

Sumar `EnvioWhatsapp.costo_estimado_ars` (lo que ya hace
`app.costo_meta.consumo_mensual` para el dashboard) es una lectura
consistente en un instante dado, pero no una reserva: dos hilos pueden sumar
el mismo "hay lugar" y los dos insertar, superando el presupuesto agregado.
El check y la reserva tienen que ser la misma operación atómica — no dos
pasos separados.

## Mecanismo

### Modelo de datos

Tabla nueva `presupuesto_meta_mensual` (`PresupuestoMetaMensual` en
`app/models.py`), una fila por mes calendario:

- `mes` (`YYYY-MM`, `UNIQUE`, ver "Identificación del mes" abajo).
- `presupuesto_ars`: congelado con `META_PRESUPUESTO_MENSUAL_ARS` vigente
  al crear la fila — mismo criterio que `EnvioWhatsapp.tarifa_ars`, un
  cambio de config a mitad de mes no reescribe retroactivamente el tope de
  un mes ya empezado.
- `costo_comprometido_ars`: el contador que se reserva atómicamente antes de
  cada envío (ver abajo). **Es la fuente de verdad para bloquear, no un
  espejo de `SUM(EnvioWhatsapp.costo_estimado_ars)` del mismo mes** — ver
  "costo_comprometido_ars puede diferir del detalle" más abajo.
- `creado_en`.

Migración: `scripts/migracion_tope_duro_meta.sql` (UP/DOWN, no corrida
contra ninguna base real), independiente de
`scripts/migracion_costo_whatsapp.sql` de la etapa 1 — no la toca ni la
reemplaza. Tabla nueva, `create_all` la crea sola.

### Identificación del mes

`app.costo_meta.mes_actual(ahora)` — un único punto que decide "qué mes es
ahora" (`YYYY-MM` en `TIMEZONE`), reusado tanto por `consumo_mensual` (la
medición de la etapa 1, para su rango y su etiqueta) como por
`reservar_gasto`/`asegurar_fila_mensual` (para la clave de la fila de
control). Antes de esta entrega `consumo_mensual` calculaba esa etiqueta
con su propia lógica inline; se refactorizó para compartir la función — así
lo que se mide y lo que bloquea no pueden desincronizarse por un criterio
de fecha distinto.

### Creación perezosa de la fila mensual, segura ante concurrencia

`app.costo_meta.asegurar_fila_mensual(db, mes)`: `SELECT` primero (camino
rápido, la fila ya existe casi siempre); si no está, `INSERT`; si otro
proceso/hilo ganó la carrera entre el `SELECT` y el `INSERT`, la `UNIQUE`
de `mes` tira `IntegrityError` — se hace `rollback` y se relee. Mismo
patrón que la dedup de `wa_message_id` en `procesar_mensaje_entrante` (ver
CLAUDE.md, "La dedup es por wa_message_id"). Nunca dos filas para el mismo
mes.

### Reserva atómica

`app.costo_meta.reservar_gasto(db, mes)`, una única sentencia:

```sql
UPDATE presupuesto_meta_mensual
SET costo_comprometido_ars = costo_comprometido_ars + :tarifa
WHERE mes = :mes AND costo_comprometido_ars + :tarifa <= presupuesto_ars
```

Check ("¿entra?") y reserva ("sumalo") son la misma operación — el motor
serializa el `UPDATE` a nivel de fila, así que dos reservas concurrentes no
pueden juntas superar el presupuesto. Mismo idioma que
`_reclamar_generacion` en `app/main.py` (`UPDATE` condicional, el
`rowcount` dice si ganaste), aplicado acá a una suma en vez de a un token.
`rowcount == 1` → reservó; `rowcount == 0` → no hay lugar.

**No se usa `SELECT ... FOR UPDATE`.** Sería correcto también, pero
obligaría a mantener una transacción abierta (con el lock de fila tomado)
desde el `SELECT` hasta el `UPDATE`; si esa transacción se extendiera para
cubrir también la llamada HTTP a Meta, el lock quedaría tomado durante todo
el tiempo que tarde Meta (con reintentos y backoff, varios segundos),
serializando globalmente todos los envíos del bot mientras dure. Comitear
el `SELECT FOR UPDATE` + `UPDATE` antes de llamar a Meta (para soltar el
lock rápido) da exactamente el mismo resultado que el `UPDATE` condicional
de una sola sentencia, con más código para el mismo comportamiento.

### Liberación

`app.costo_meta.liberar_reserva(db, mes)`: resta la tarifa,
incondicionalmente, del `costo_comprometido_ars` de ese mes. Se llama
**solo** si Meta rechazó o falló el envío.

### `costo_comprometido_ars` puede diferir del detalle

Si Meta **acepta** el envío pero después falla el guardado de
`Mensaje`/`EnvioWhatsapp` (motivo de base, no de Meta — ver
specs/spec-costo-whatsapp-meta.md, "Flujo inspeccionado"), **la reserva NO
se libera**. Es intencional y conservador: el gasto ya ocurrió del lado de
Meta aunque no quede el detalle guardado localmente, y liberar la reserva
dejaría que el tope real se corra hacia arriba con cada falla de ese tipo —
justo lo que un tope duro no puede permitirse.

Consecuencia: `presupuesto_meta_mensual.costo_comprometido_ars` de un mes
puede terminar **por encima** de
`SUM(envios_whatsapp.costo_estimado_ars)` del mismo mes — nunca por debajo.
Son dos fuentes con propósitos distintos: `costo_comprometido_ars` es la
que bloquea (conservadora a propósito, nunca subestima lo comprometido);
`SUM(EnvioWhatsapp)`, la que muestra el CRM hoy (detalle por envío, puede
subestimar el gasto real ante esa misma falla — ver
specs/spec-costo-whatsapp-meta.md). Esta entrega no las concilia ni lo
intenta.

### Dónde entra en `enviar_y_guardar` (`app/main.py`)

Con `META_TOPE_DURO_HABILITADO=false` (el default), el comportamiento es
idéntico al de antes de esta entrega: no se reserva nada, no se libera
nada, `reservar_gasto`/`liberar_reserva` no se llaman nunca.

Con el flag prendido, antes de `meta_client.enviar_mensaje_texto(...)`:

1. `mes_actual(datetime.now(timezone.utc))`.
2. `reservar_gasto(db, mes)` — internamente asegura la fila del mes.
3. Si no reservó: `return ResultadoEnvio.BLOQUEADO_PRESUPUESTO`, **sin
   llamar a Meta**.

Si Meta rechaza/falla (excepción de `enviar_mensaje_texto`):
`liberar_reserva(db, mes)`, luego `return ResultadoEnvio.FALLO_META`.

Si Meta acepta: la reserva sigue su curso normal — no hay ningún código que
la libere a partir de acá. Si el `db.commit()` final (que guarda `Mensaje`
+ `EnvioWhatsapp`) falla, la excepción se escapa sin capturar como ya hacía
antes de esta entrega; la reserva, comiteada en su propia transacción
*antes* de llamar a Meta, queda intacta.

### `ResultadoEnvio`: distinguir motivos de "no se pudo enviar"

`enviar_y_guardar` devolvía `bool`. Un `False` no alcanzaba para distinguir
"la conversación ya está en modo humano" de "Meta rechazó el envío" de "no
se llegó a intentar, bloqueado por presupuesto" — motivos con implicancias
distintas para quien llama. Pasa a devolver `app.main.ResultadoEnvio`
(`str, enum.Enum`): `EXITOSO`, `MODO_HUMANO`, `FALLO_META`,
`BLOQUEADO_PRESUPUESTO`.

De los ocho llamadores de `enviar_y_guardar` en `app/main.py`, solo uno
inspeccionaba el valor de retorno — el aviso de escalamiento, en
`escalar_a_humano` (antes `if not enviar_y_guardar(...)`, ahora
`if resultado_aviso != ResultadoEnvio.EXITOSO`, con el motivo agregado al
log). Los otros siete llaman a `enviar_y_guardar` sin mirar qué devuelve
(la respuesta del modelo, los avisos de límite/error/transitorio, el de
adjunto no soportado): no necesitaron ningún cambio, un enum en vez de un
bool no les afecta si no leen el valor.

### Cuando el envío queda bloqueado

No se llama a Meta, no se crea ningún `EnvioWhatsapp`, no se encola nada
para reintentar. Queda un `WARNING` en el log con el identificador
enmascarado y el mes. **No se manda ningún WhatsApp anunciando el bloqueo**
— el aviso también pasaría por `enviar_y_guardar` y también quedaría
bloqueado por el mismo tope, así que sería un intento inútil además de
gastar otra reserva innecesariamente.

**Pregunta abierta, sin resolver en esta entrega**: si el envío bloqueado
es la respuesta normal del modelo (`responder()`, `app/main.py`, la rama
`if resultado.texto: enviar_y_guardar(...)`), hoy no pasa nada más — la
conversación queda sin respuesta y sin escalar, y el próximo mensaje de la
misma persona va a chocar con el mismo bloqueo hasta que cambie el mes.
Técnicamente, ese punto podría llamar a `escalar_a_humano(...)` (que es
puro `UPDATE`/`commit`, sin depender de Meta, para el estado de
`modo_humano`) igual que ya hacen las otras ramas de `responder()` cuando
falla el modelo — pero decidir que **todo** bloqueo escale automáticamente
a modo humano es una decisión de producto (potencialmente escala en masa
todas las conversaciones nuevas del resto del mes una vez que se gasta el
presupuesto), no solo de mecanismo, y queda deliberadamente sin resolver
acá.

## Limitación conocida (no resuelta en esta entrega)

Si un proceso muere entre `reservar_gasto` (que ya comiteó) y la llamada a
Meta, o entre la llamada a Meta y el `db.commit()` final, la reserva queda
"pegada": contada como gasto comprometido, sin que haya un mensaje real
enviado (o sin el detalle guardado). No hay recuperación automática en
esta entrega — a propósito, está fuera de alcance. El costo de una reserva
huérfana es subutilizar presupuesto (conservador, nunca sobregasto). Si en
producción esto se vuelve un problema real, el mismo patrón de abandono +
recuperación que ya existe para el agrupamiento
(`AGRUPAR_ABANDONO_SEGUNDOS`, `_recuperar_lotes_pendientes` en
`app/main.py`) sería la extensión natural — no implementado acá.

## Configuración

`META_TOPE_DURO_HABILITADO` (bool, default `false`) — mismo criterio que
`ESCALAMIENTO_HABILITADO`/`CRM_HABILITADO`: todo el mecanismo puede vivir
en el código sin cambiar ningún comportamiento hasta que se prenda a
propósito. Sin validación adicional en `validar_config()` — es un flag
booleano simple, mismo criterio que esos dos.

## Tests

`tests/test_tope_duro_meta.py`: flag apagado conserva el comportamiento de
la etapa 1 (webhook real); una reserva dentro del presupuesto permite
enviar; un mensaje que superaría el presupuesto no llega a llamar a Meta
(`meta_enviados` no crece); reservas concurrentes (hilos reales, cada uno
con su propia sesión) no superan juntas el presupuesto agregado; Meta
rechaza/falla libera la reserva; Meta acepta la deja comprometida; Meta
acepta pero falla la persistencia posterior también la deja comprometida
(simulado forzando el `db.commit()` final a fallar); creación concurrente
de la fila mensual (hilos reales) no genera dos filas; un mes nuevo usa una
fila independiente sin tocar la del mes anterior.
