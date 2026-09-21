# Spec — Agrupar y ordenar mensajes consecutivos (entrega 1.2 de roadmap-bot-crm.md)

## Problema

Hoy (post 1.1) cada mensaje de texto entrante dispara su propia llamada a
`generar_respuesta` de forma independiente. Si alguien escribe una idea en
varios mensajes seguidos ("che", "quería preguntar", "¿tienen sala para 10
personas el sábado?"), el bot contesta a destiempo: puede responder al primer
mensaje suelto antes de que lleguen los siguientes, o generar una respuesta
por cada uno.

## Alcance de esta entrega

- Agrupar mensajes de **texto** consecutivos de una misma conversación en una
  sola llamada al modelo, con una ventana de espera breve y configurable y un
  tope duro para que una ráfaga continua no posponga la respuesta para
  siempre.
- Una sola generación activa por conversación a la vez; conversaciones
  distintas (identificador_externo o canal distintos) son independientes
  entre sí, nunca se bloquean una a otra.
- Los mensajes individuales se siguen guardando uno por uno, con su propio
  `wa_message_id` (dedup sin cambios).
- El texto agrupado no entra dos veces al contexto del modelo: los mensajes
  del lote se excluyen del historial que se arma para esa misma llamada.
- El mecanismo funciona con más de un proceso corriendo la app a la vez, sin
  asumir que el proceso que arrancó a esperar es el que va a terminar de
  procesar: la coordinación vive en la base (una columna con una condición en
  el `UPDATE`), no en una cola ni un lock en memoria de Python.
- El webhook (`POST /webhook`) sigue respondiendo de inmediato: la espera de
  agrupamiento ocurre enteramente dentro de la background task, como ya
  ocurría con la llamada al modelo.

## Fuera de alcance (no tocado por esta entrega)

- El CRM (1.3 en adelante) y la marca de respuestas problemáticas.
- Los adjuntos no soportados (entrega 1.1) siguen respondiéndose
  individuales e inmediatos, sin pasar por el agrupamiento — ver más abajo,
  "Adjuntos mezclados con texto".
- La deduplicación por `wa_message_id`, el límite de mensajes por hora, la
  firma del webhook y la pausa humana: se reusan tal cual, ninguno cambia de
  comportamiento propio.
- No se implementa reintento automático de un lote que falló por un bug
  propio más allá de lo que ya hacía `procesar_mensaje_entrante` (loguear y
  seguir); ver "Reinicios y recuperación" más abajo para qué sí se garantiza.

## Diseño

### Por qué en la base y no en memoria

El roadmap pide explícitamente no asumir que una cola en memoria alcanza para
correr con más de un proceso. Este proyecto además tiene precedente directo
de por qué: la dedup de mensajes entrantes ya resuelve la misma clase de
problema (dos entregas concurrentes del mismo webhook) con una constraint
única de base y un `IntegrityError` atrapado, no con un lock de Python (ver
CLAUDE.md, "la dedup es por `wa_message_id`"). El agrupamiento reusa la misma
idea: un `UPDATE ... WHERE` condicional es atómico a nivel de fila sin
importar cuántos procesos o hilos lo intenten a la vez, porque lo resuelve el
motor de base, no el proceso.

**Aclaración sobre el despliegue actual:** PENDIENTES.md, sección 5.c, ya
documentaba que el start command de producción tiene que ser un solo worker
de uvicorn (el engine y el pool de conexiones son variables de módulo, un
segundo worker los duplicaría en vez de compartirlos). Esta entrega no
cambia esa recomendación ni la vuelve innecesaria — el diseño de acá abajo
está para que el mecanismo *no rompa* si en el futuro se corre con más de un
proceso, no para habilitar hoy un despliegue multi-worker.

### Columnas nuevas

`Conversacion` suma tres campos (ver "Migración" más abajo):

- `generando_desde` (`DateTime(timezone=True)`, nullable): `NULL` significa
  "nadie está generando una respuesta para esta conversación ahora mismo".
  Un valor no nulo es la reserva: alguien la tomó en ese instante.
- `generando_token` (`String`, nullable): un identificador opaco al azar
  (`uuid.uuid4().hex`) que identifica *a quién* pertenece la reserva actual.
  Separado de `generando_desde` a propósito: comparar timestamps después de
  un viaje a la base no es seguro (SQLite pierde precisión y tzinfo en el
  round-trip, ver `app/pausa.py` sobre el mismo problema), y además una
  marca de tiempo sola no alcanza para decidir "¿la reserva que estoy por
  soltar sigue siendo la mía?" si mientras tanto alguien la retomó por
  abandono (ver más abajo). El token sí sirve para esa comparación: es
  estable y exacto.
- `ultimo_mensaje_agrupado_id` (`Integer`, nullable): el id del último
  `Mensaje` de rol `usuario` y tipo texto que ya fue incluido en algún lote
  procesado. `NULL` significa "todavía no se procesó ningún lote en esta
  conversación". Los mensajes pendientes de un nuevo lote son los que tienen
  `id > ultimo_mensaje_agrupado_id` (o todos, si es `NULL`).

`Mensaje` suma un campo:

- `tipo` (`String`, nullable): el `messages[].type` real del webhook de Meta
  para los mensajes de rol `usuario` (ver specs/spec-adjuntos-no-soportados.md).
  Sin este campo, decidir "¿este mensaje es texto y entra en el agrupamiento?"
  requeriría mirar el contenido guardado — exactamente la coincidencia
  textual con el marcador que la entrega 1.1 prohibió para decidir el tipo
  de un mensaje. `NULL` para mensajes que no vienen del webhook con esa
  semántica (bot, humano).

### Reserva atómica: `_reclamar_generacion` / `_liberar_generacion`

```python
def _reclamar_generacion(db, conversacion) -> str | None:
    ahora = datetime.now(timezone.utc)
    umbral_abandono = ahora - timedelta(seconds=config.agrupar_abandono_segundos)
    token = uuid.uuid4().hex
    filas = (
        db.query(Conversacion)
        .filter(
            Conversacion.id == conversacion.id,
            or_(Conversacion.generando_desde.is_(None), Conversacion.generando_desde < umbral_abandono),
        )
        .update({"generando_desde": ahora, "generando_token": token})
    )
    db.commit()
    return token if filas == 1 else None
```

Devuelve el token si la reserva se tomó, `None` si ya había una vigente (la
tomó otro hilo, otro proceso, o esta misma conversación ya la tenía tomada
hace menos de `AGRUPAR_ABANDONO_SEGUNDOS`). El `UPDATE ... WHERE` es la
única fuente de verdad: no hay lectura-y-después-escritura en dos pasos, así
que no hay ventana de carrera entre comprobar y reservar, sin importar
cuántos procesos compartan la base.

`_liberar_generacion(db, conversacion, token)` hace el `UPDATE` inverso,
**condicionado a que el token siga siendo el mismo**:

```python
def _liberar_generacion(db, conversacion, token) -> None:
    db.query(Conversacion).filter(
        Conversacion.id == conversacion.id, Conversacion.generando_token == token,
    ).update({"generando_desde": None, "generando_token": None})
    db.commit()
```

Si mientras tanto la reserva fue tomada por otro (porque nos consideraron
abandonados por tardar más de `AGRUPAR_ABANDONO_SEGUNDOS`), este `UPDATE`
afecta cero filas y no le pisa la reserva al nuevo dueño. Sin el token —
comparando solo que `generando_desde` no sea `NULL`, por ejemplo— liberar
"a ciegas" podría borrarle la reserva a otro proceso que la tomó de buena fe
creyendo que la anterior había quedado abandonada.

### La ventana de espera: `_esperar_ventana_de_agrupamiento`

Quien reclama la generación (el primer mensaje del lote) espera en un bucle:

```python
def _esperar_ventana_de_agrupamiento(db, conversacion) -> None:
    inicio = datetime.now(timezone.utc)
    ultimo_id_visto = _ultimo_id_pendiente(db, conversacion)
    while True:
        dormir(config.agrupar_ventana_segundos)
        ultimo_id_ahora = _ultimo_id_pendiente(db, conversacion)
        if ultimo_id_ahora == ultimo_id_visto:
            return  # pasó una ventana entera sin que llegara nada nuevo
        if (datetime.now(timezone.utc) - inicio).total_seconds() >= config.agrupar_espera_maxima_segundos:
            return  # tope duro: no se pospone más aunque siga llegando texto
        ultimo_id_visto = ultimo_id_ahora
```

`dormir` es `time.sleep` por default, pero es una referencia de módulo
reemplazable (`main.dormir`) — los tests que necesitan controlar exactamente
cuándo se intercala un segundo mensaje durante la espera lo reemplazan por
una función sincronizada con `threading.Event`, sin depender de que el reloj
real y el tiempo de ejecución del test coincidan (ver "Tests" más abajo).

Este bucle es el que resuelve **"ventana breve, con espera máxima"**: cada
vuelta duerme `AGRUPAR_VENTANA_SEGUNDOS` (propuesta inicial: 2s) y se corta
sola si nadie escribió nada nuevo en esa vuelta (ráfaga terminada) o si ya
se llegó a `AGRUPAR_ESPERA_MAXIMA_SEGUNDOS` (propuesta inicial: 8s) desde que
arrancó a esperar (ráfaga continua, corta igual).

**Consecuencia que hay que tener presente:** todo mensaje de texto, incluido
uno que llega solo sin ningún otro después, espera como mínimo una ventana
completa (`AGRUPAR_VENTANA_SEGUNDOS`) antes de que el bot empiece a generar
la respuesta. Es el costo inherente de agrupar: no hay forma de saber si un
mensaje es el único de la ráfaga sin esperar un poco a ver si llega otro.

### El ciclo completo: `agrupar_y_responder`

```python
def agrupar_y_responder(db, conversacion) -> None:
    token = _reclamar_generacion(db, conversacion)
    if token is None:
        return  # otro dueño ya está procesando esta conversación; se suma a su lote

    try:
        while True:
            _esperar_ventana_de_agrupamiento(db, conversacion)

            db.refresh(conversacion)
            if pausa_vigente(conversacion, datetime.now(timezone.utc)):
                return  # el lote pendiente queda para el próximo mensaje que llegue

            lote = _mensajes_pendientes_de_texto(db, conversacion)
            if not lote:
                return

            responder(db, conversacion, lote)

            conversacion.ultimo_mensaje_agrupado_id = lote[-1].id
            db.commit()

            db.refresh(conversacion)
            if not _mensajes_pendientes_de_texto(db, conversacion):
                return
            # Llegaron mensajes nuevos mientras se generaba: se vuelve a
            # esperar la ventana para ese lote nuevo, sin soltar la reserva.
    finally:
        _liberar_generacion(db, conversacion, token)
```

Quien no gana la reserva (`token is None`) no hace nada más: no espera, no
reintenta, simplemente vuelve. Su mensaje ya quedó guardado (eso pasa antes,
en `procesar_mensaje_entrante`, sin cambios) y va a ser recogido por
`_mensajes_pendientes_de_texto` cuando el dueño actual del lote vuelva a
mirar — sea porque todavía está en la ventana de espera (lo agrupa en el
mismo lote) o porque ya terminó de responder y el bucle de arriba encuentra
mensajes nuevos (arranca un lote nuevo, sin soltar la reserva entre uno y
otro).

**El marcador se actualiza recién después de que `responder()` vuelve**, no
antes de llamarlo. Es la decisión que hace seguro un reinicio a mitad de
camino (ver "Reinicios y recuperación").

### Armar el lote y llamar al modelo: cambios en `responder`

`responder(db, conversacion, mensajes_agrupados: list[Mensaje])` reemplaza a
la versión de un solo mensaje. El texto que ve el modelo es la concatenación
de los `contenido` del lote, en orden cronológico, separados por un salto de
línea — se tratan como si fueran un solo turno del usuario, que es
exactamente la intención de agruparlos:

```python
mensaje_nuevo = "\n".join(m.contenido for m in mensajes_agrupados)
```

`construir_historial` (app/historial.py) generaliza su segundo parámetro
para aceptar un `Mensaje` suelto (como hasta ahora, sin tocar ninguna
llamada existente) o una lista: internamente normaliza a lista y excluye
**todos** los ids del lote de la consulta de historial, y usa el mensaje más
antiguo del lote como referencia para el chequeo de antigüedad (si pasaron
más de `HISTORIAL_DIAS_VALIDEZ` días entre el mensaje anterior a la ráfaga y
el primero de la ráfaga, se descarta el historial igual que antes). Así
ningún mensaje del lote entra dos veces al contexto: ni como parte del
historial ni implícito en el string armado a mano.

### Adjuntos mezclados con texto

**Decisión: los adjuntos no participan del agrupamiento en absoluto.**
Siguen exactamente el comportamiento de la entrega 1.1: cada uno se responde
de inmediato, individual, con el texto fijo que corresponda a su tipo, sin
tocar la reserva de generación ni el `ultimo_mensaje_agrupado_id`. La razón
es doble:

1. Un adjunto ya no llama al modelo por diseño de 1.1 — no hay ninguna
   llamada que valga la pena posponer ni agrupar.
2. Mezclar "un lote de texto que se está esperando" con "un adjunto que
   necesita respuesta ya" complicaría mucho la máquina de estados (¿el
   adjunto espera a que termine el lote de texto? ¿lo corta? ¿se intercala?)
   para un beneficio que no existe: la respuesta al adjunto no depende de
   nada que el agrupamiento resuelva.

Concretamente: si alguien manda "che" (texto), una foto (adjunto), "¿me
pasás el precio?" (texto) en rápida sucesión, el bot va a mandar el texto
fijo de la foto de inmediato (como si el agrupamiento no existiera) y una
sola respuesta agrupando "che" + "¿me pasás el precio?" — dos mensajes del
bot en total, no tres ni uno. `_mensajes_pendientes_de_texto` filtra
`Mensaje.tipo == "text"`, así que un adjunto en el medio de una ráfaga de
texto ni siquiera aparece en la consulta.

### Mensajes que llegan durante una generación

Ya está resuelto por el diseño de `agrupar_y_responder` de arriba: un
mensaje que llega mientras el dueño actual está generando (dentro de
`responder()`, incluido el presupuesto de hasta 20s de la llamada al
modelo) se guarda igual que siempre, no logra tomar la reserva (está
tomada), y queda pendiente. Cuando el dueño termina de responder el lote en
curso, vuelve a mirar si hay pendientes — los encuentra, y arranca
inmediatamente un lote nuevo con ellos (con su propia ventana de espera, por
si siguen llegando más). No hace falta que nadie más "empuje" ese
procesamiento: el propio dueño lo recoge al terminar.

### `modo_humano` durante el agrupamiento

Se chequea (`pausa_vigente`) **antes** de armar el lote, en cada vuelta del
bucle — si la conversación pasó a modo humano mientras se esperaba (una
intervención manual, o un escalamiento disparado por otra vía), no se llama
al modelo para nada: se suelta la reserva sin tocar
`ultimo_mensaje_agrupado_id`. Los mensajes quedan pendientes tal cual —
cuando alguien reactive el bot (botón del CRM o el script) y llegue un
mensaje nuevo, ese mensaje nuevo va a reclamar la reserva y su lote va a
incluir tanto los mensajes viejos que quedaron sin responder como el nuevo.
Nada se pierde en silencio; a lo sumo se responde tarde. Es la misma
filosofía que ya regía antes de esta entrega para un mensaje entrante único
durante una pausa (`procesar_mensaje_entrante` ya cortaba ahí sin generar
nada) — el agrupamiento no le agrega nada nuevo a esa regla, solo hace falta
repetir el chequeo en cada vuelta porque ahora hay un bucle en el medio.

Además, `enviar_y_guardar` sigue re-chequeando `modo_humano` inmediatamente
antes de mandar el texto (sin cambios): si la pausa se activó *durante* la
llamada al modelo dentro de `responder()`, la respuesta ya generada tampoco
se manda. Las dos capas se complementan: una evita arrancar a generar de
más, la otra evita mandar algo generado de más.

### Reinicios y recuperación (sin cola en memoria)

Si el proceso muere mientras `agrupar_y_responder` tiene la reserva tomada
(entre `_reclamar_generacion` y el `finally`), `generando_desde` queda con
un valor viejo para siempre — no hay ningún hilo que vaya a liberarlo. La
recuperación es automática y no requiere intervención manual:
`_reclamar_generacion` trata cualquier reserva con más de
`AGRUPAR_ABANDONO_SEGUNDOS` de antigüedad como abandonada y la vuelve a
tomar. El próximo mensaje de esa conversación (o, si no llega ninguno, nada
— no hay un cron que reprocese solo) dispara la recuperación.

`AGRUPAR_ABANDONO_SEGUNDOS` tiene que ser mayor al peor caso de
procesamiento legítimo, para no robarle la reserva a un proceso que
simplemente está tardando. El peor caso conocido es
`AGRUPAR_ESPERA_MAXIMA_SEGUNDOS` (esperando el lote) +
`PRESUPUESTO_TOTAL_SEGUNDOS` de `app/respuesta.py` (20s, la llamada al
modelo con su reintento) + los reintentos de envío de `app/meta.py`
(backoff 1s→2s, ~3s). Con los defaults propuestos (8 + 20 + 3 = 31s) un
default de 60s deja margen cómodo. Configurable igual, por si el modelo o
el proveedor elegido en producción son más lentos. `validar_config()`
(`app/validacion_config.py:minimo_seguro_agrupar_abandono_segundos`) calcula
este piso a partir de las constantes reales en vez de confiar en que el
número de acá arriba se mantenga sincronizado a mano, y corta el arranque si
`AGRUPAR_ABANDONO_SEGUNDOS` queda por debajo.

**Recuperación automática al arranque, no solo con el próximo mensaje.**
`al_iniciar()` (`app/main.py`) llama a `_recuperar_lotes_pendientes()`
después de `init_db()`: busca conversaciones con mensajes de texto que
todavía no entraron a ningún lote y dispara `agrupar_y_responder` para cada
una, en su propio hilo (no bloquea el arranque del server esperando hasta
`AGRUPAR_ESPERA_MAXIMA_SEGUNDOS + PRESUPUESTO_TOTAL_SEGUNDOS` por
conversación). No asume que el proceso que arranca es el único corriendo:
sigue pasando por `_reclamar_generacion`, así que una conversación con una
reserva todavía vigente (otro proceso la está usando de verdad) se deja
pasar sin tocarla. Sin este paso, un lote que quedó pendiente por un proceso
que murió esperaba en silencio hasta que la persona escribiera de nuevo —
podían pasar horas, o no pasar nunca si esa persona no vuelve a escribir.

**Esto no cambia el riesgo de duplicado ya documentado, lo hace más
alcanzable.** Si el proceso murió justo durante la llamada al modelo,
después de haber armado el lote pero antes de que `responder()` volviera (y
alcanzó a mandar la respuesta por WhatsApp antes de morir), la recuperación
al arranque va a reprocesar ese mismo lote y puede generar una respuesta
duplicada — exactamente el escenario que ya describe el párrafo de abajo,
solo que antes dependía de que llegara un mensaje nuevo para dispararse, y
ahora se dispara solo, en cada arranque, mientras la reserva siga sin
liberar. Sigue siendo el mismo trade-off aceptado: no perder el mensaje vale
más que evitar un duplicado raro y acotado.

**Qué se garantiza y qué no:** ningún mensaje se pierde en silencio — en el
peor caso (el proceso muere justo durante la llamada al modelo, después de
haber armado el lote pero antes de que `responder()` vuelva) el
`ultimo_mensaje_agrupado_id` **no** se llegó a mover (se actualiza recién
después de que `responder()` termina, a propósito), así que el próximo
dueño va a reprocesar ese mismo lote entero, incluida una nueva llamada al
modelo. Si el proceso viejo alcanzó a mandar una respuesta por WhatsApp
antes de morir, el usuario puede llegar a recibir una respuesta duplicada
(o parecida). Se prefiere ese riesgo, acotado y raro, a perder el mensaje
para siempre — es la misma lógica que ya rige los reintentos de Meta
("son normales, no un error") y la dedup por `wa_message_id`: el sistema
tolera reprocesar antes que descartar.

### Multiplicidad de procesos

Cubierto por construcción: `_reclamar_generacion`/`_liberar_generacion` son
`UPDATE`s condicionales contra la base, no estructuras de Python en memoria
de un proceso. Dos procesos (o dos hilos del mismo proceso, que es lo que
efectivamente corre en desarrollo/tests) que intenten reclamar la misma
conversación al mismo tiempo: la base resuelve cuál de los dos `UPDATE`
afecta la fila primero (con un lock de fila estándar del motor), el otro ve
0 filas afectadas y no llega a hacer nada más. No hace falta ningún lock
explícito de aplicación.

## Migración de esquema

Sin Alembic (ver PENDIENTES.md, sección 5.b): `create_all` crea las columnas
nuevas solo en una base sin la tabla todavía (tests, un deploy nuevo). Una
base Postgres existente con `conversaciones`/`mensajes` ya creadas necesita
un `ALTER TABLE` a mano — `scripts/migracion_agrupamiento.sql`, con secciones
`-- UP` y `-- DOWN` explícitas, nunca corrido contra ninguna base real como
parte de esta entrega:

```sql
-- UP
ALTER TABLE conversaciones ADD COLUMN IF NOT EXISTS generando_desde TIMESTAMPTZ;
ALTER TABLE conversaciones ADD COLUMN IF NOT EXISTS generando_token VARCHAR;
ALTER TABLE conversaciones ADD COLUMN IF NOT EXISTS ultimo_mensaje_agrupado_id INTEGER;
ALTER TABLE mensajes ADD COLUMN IF NOT EXISTS tipo VARCHAR;

-- DOWN
ALTER TABLE mensajes DROP COLUMN IF EXISTS tipo;
ALTER TABLE conversaciones DROP COLUMN IF EXISTS ultimo_mensaje_agrupado_id;
ALTER TABLE conversaciones DROP COLUMN IF EXISTS generando_token;
ALTER TABLE conversaciones DROP COLUMN IF EXISTS generando_desde;
```

Las cuatro son nullable y sin default obligatorio: agregarlas no requiere
tocar filas existentes ni bloquea la tabla más que el `ALTER TABLE` en sí
(rápido en Postgres moderno para un `ADD COLUMN` nullable sin default). El
rollback (`DOWN`) es seguro porque nada de afuera de esta entrega lee esas
columnas — al sacarlas, el código de esta entrega dejaría de arrancar (columnas
que el ORM espera y no están), así que el `DOWN` se corre siempre junto con
un revert del código, nunca solo.

## Configuración nueva

- `AGRUPAR_VENTANA_SEGUNDOS` (float, default `2`): cuánto se espera en
  silencio antes de dar por terminada una ráfaga.
- `AGRUPAR_ESPERA_MAXIMA_SEGUNDOS` (float, default `8`): tope duro desde que
  arranca la espera, sin importar que sigan llegando mensajes.
- `AGRUPAR_ABANDONO_SEGUNDOS` (float, default `60`): a partir de cuándo una
  reserva se considera abandonada y se puede retomar.

Sin validación obligatoria en `validar_config()` (como `HISTORIAL_MAX_MENSAJES`
o `LIMITE_MENSAJES_HORA`, que tampoco la tienen): son parámetros de
comportamiento con un default razonable, no secretos ni datos cuya ausencia
deba tumbar el arranque.

## Tests: cómo se evitan los sleeps reales largos

- `main.dormir` es una referencia de módulo reemplazable. La suite fija
  `AGRUPAR_VENTANA_SEGUNDOS`/`AGRUPAR_ESPERA_MAXIMA_SEGUNDOS` a valores chicos
  por default (`tests/conftest.py`) para que cualquier test que mande un
  mensaje de texto y espere una respuesta (la gran mayoría de la suite
  existente) no note una demora perceptible.
- Los tests que necesitan controlar con precisión cuándo se intercala un
  segundo (o tercer) mensaje durante la ventana de espera reemplazan
  `main.dormir` por una función sincronizada con `threading.Event`
  (arrancan un hilo para el mensaje que va a reclamar la reserva, y el hilo
  principal del test inyecta el resto de los mensajes de la ráfaga
  exactamente cuando el `dormir` falso lo indica) — mismo patrón que ya usa
  `tests/test_escalamiento.py` para las carreras entre una respuesta lenta y
  un escalamiento, aplicado acá al punto de sincronización nuevo.
- No hay ningún test que dependa de que el reloj real avance una cantidad
  exacta de segundos: donde hace falta simular que pasó más tiempo que
  `AGRUPAR_ESPERA_MAXIMA_SEGUNDOS` o que `AGRUPAR_ABANDONO_SEGUNDOS`, el test
  baja esos valores de configuración en vez de dormir de verdad, o escribe
  directamente en la base un `generando_desde` viejo.

## Tests existentes que esta entrega vuelve obsoletos

`tests/test_escalamiento.py::test_el_bot_no_escribe_encima_de_un_humano` y
`::test_una_carrera_no_pisa_el_resumen_del_primer_escalamiento` probaban una
carrera que ya no puede pasar tal como estaba escrita: dos mensajes
concurrentes a la misma conversación generando **dos** respuestas
independientes del modelo. Con el agrupamiento, el segundo mensaje no
alcanza a tomar la reserva mientras el primero la tiene — se suma al lote
del primero (si todavía está en la ventana de espera) o queda pendiente para
el lote siguiente (si el primero ya está generando). Ya no hay dos llamadas
independientes a `generar_respuesta` para la misma conversación al mismo
tiempo, así que la carrera que estos dos tests ejercitaban no existe más.

Se reemplazan por un test nuevo que cubre la propiedad de seguridad
equivalente bajo el diseño nuevo: si `modo_humano` se activa **mientras** el
lote está en la ventana de espera (una intervención manual concurrente, por
ejemplo), el bot no genera ni manda nada para ese lote —
`test_no_se_genera_respuesta_si_modo_humano_se_activa_durante_la_espera` en
`tests/test_agrupamiento.py`. El caso de "la respuesta ya generada no se
manda encima de un humano" lo sigue cubriendo, sin cambios,
`tests/test_pausa_humana.py::test_secretaria_responde_mientras_el_modelo_genera_el_bot_no_escribe_encima`
(esa carrera involucra una sola llamada a `procesar_mensaje_entrante`, no
dos, así que el mecanismo de reserva no la afecta).

## Cierre / criterios de aceptación

- Tests deterministas (ver arriba) de: ráfaga de 2-3 mensajes → una sola
  llamada al modelo con el texto concatenado en orden, tanto armada a mano
  (`procesar_mensaje_entrante` en hilos) como con un solo POST HTTP real con
  varios `messages[]` del mismo contacto; dos conversaciones (identificador o
  canal distinto) procesándose sin bloquearse entre sí, incluida una prueba
  con `threading.Event` por contacto que exige que las dos estén esperando
  la ventana al mismo tiempo; orden preservado dentro del lote; dedup dentro
  de una ráfaga (mismo `wa_message_id` repetido); los mensajes del lote no
  aparecen duplicados en el historial; un adjunto en medio de una ráfaga de
  texto no la interrumpe ni se agrupa con ella; un mensaje que llega durante
  la llamada al modelo (no durante la espera) no se mezcla con el lote en
  curso, forma uno posterior propio; `modo_humano` activado durante la
  espera corta el lote sin generar nada; un mensaje que queda fuera de lote
  por el límite de mensajes por hora avanza `ultimo_mensaje_agrupado_id` de
  todos modos (monótono, sin pisar el avance de un lote real — ver
  `_avanzar_marca_de_agrupado` en `app/main.py`), para que la tarea de
  agrupamiento que igual se encola no lo procese por encima del límite; una
  reserva vieja (más que `AGRUPAR_ABANDONO_SEGUNDOS`) se puede retomar,
  tanto por un mensaje nuevo como por `_recuperar_lotes_pendientes()` al
  arranque, sin pisar una reserva vigente de otro proceso; dos intentos de
  reserva simultáneos sobre la misma conversación — solo uno gana; y que
  `AGRUPAR_VENTANA_SEGUNDOS`/`AGRUPAR_ESPERA_MAXIMA_SEGUNDOS`/
  `AGRUPAR_ABANDONO_SEGUNDOS` fuera de rango (no positivos, o el abandono por
  debajo del piso seguro calculado) corte el arranque.
- Suite completa corrida con `.venv/Scripts/python.exe -m pytest`, sin
  fallos nuevos.
- **Prueba manual pendiente, fuera de esta entrega con dobles:** mandar tres
  mensajes seguidos por WhatsApp real (celular → Meta → servidor) y
  confirmar que llega una sola respuesta, coherente con los tres, y que la
  demora percibida es razonable (del orden de `AGRUPAR_VENTANA_SEGUNDOS`,
  no de `AGRUPAR_ESPERA_MAXIMA_SEGUNDOS`). Se suma a la lista de validación
  real de PENDIENTES.md, sección 1.
