-- Migración de esquema para la atención humana en el CRM, Entrega 1 / Tarea 1
-- (atenciones, tomar y resolver; ver app/atencion.py).
--
-- Dos partes, y NO son iguales en cuanto a necesidad:
--
-- 1. ALTER TYPE motivo_pausa: OBLIGATORIO en cualquier Postgres existente
--    antes de desplegar este código. `motivo_pausa` es un tipo enum nativo y
--    `create_all` no le agrega valores (ver PENDIENTES.md, "Los enums son
--    tipos nativos de Postgres"). Sin esto, "Iniciar atención" desde el CRM
--    falla en runtime con InvalidTextRepresentation — el arranque no lo
--    detecta.
--
-- 2. CREATE TABLE / CREATE INDEX de atenciones: `create_all`
--    (app/db.py:init_db) los crea solos al arrancar, con exactamente la
--    misma definición y los mismos nombres (verificado compilando el modelo
--    contra el dialecto de Postgres). Correrlos acá es opcional; si se
--    corren, lo que cree el que llegue segundo se saltea por IF NOT EXISTS.
--    Necesitan que ya existan `conversaciones` y `crm_usuarios`.
--
-- No se corrió contra ninguna base real como parte de esta entrega.
--
-- Uso (cada sentencia en su propia transacción, cortando en el primer error):
--
--     psql -v ON_ERROR_STOP=1 "$DATABASE_URL" -f scripts/migracion_atenciones.sql
--
-- - NO usar `-1` / `--single-transaction`. En Postgres < 12,
--   `ALTER TYPE ... ADD VALUE` no puede correr dentro de un bloque de
--   transacción. En >= 12 sí puede, pero el valor nuevo no se puede usar
--   hasta el commit. Este script nunca usa 'atencion_crm' como dato (el
--   índice parcial filtra por `estado`, no por `motivo_pausa`), así que con
--   el comando de arriba no hay problema en ninguna versión.
-- - Sin ON_ERROR_STOP, psql sigue después de un error y termina con código 0:
--   un fallo a mitad de camino pasaría desapercibido.
--
-- Se puede correr más de una vez: las tres sentencias llevan IF NOT EXISTS
-- (ADD VALUE IF NOT EXISTS existe desde Postgres 9.3) y la segunda corrida
-- solo emite NOTICEs. Ojo: IF NOT EXISTS en un índice compara solo el
-- nombre, no la definición.

-- UP
ALTER TYPE motivo_pausa ADD VALUE IF NOT EXISTS 'atencion_crm';

CREATE TABLE IF NOT EXISTS atenciones (
    id SERIAL PRIMARY KEY,
    conversacion_id INTEGER NOT NULL REFERENCES conversaciones (id),
    estado VARCHAR NOT NULL,
    motivo VARCHAR NOT NULL,
    resumen TEXT,
    iniciada_por_id INTEGER REFERENCES crm_usuarios (id),
    responsable_id INTEGER REFERENCES crm_usuarios (id),
    resuelta_por_id INTEGER REFERENCES crm_usuarios (id),
    -- Sin DEFAULT a propósito: el modelo pone la fecha desde Python
    -- (`ahora_utc`), igual que el resto de las tablas. Con un default acá,
    -- el esquema dependería de quién creó la tabla, el script o create_all.
    creada_en TIMESTAMPTZ NOT NULL,
    tomada_en TIMESTAMPTZ,
    resuelta_en TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS ix_atenciones_conversacion_id ON atenciones (conversacion_id);

-- Una sola atención abierta por conversación (ver `Atencion` en app/models.py).
CREATE UNIQUE INDEX IF NOT EXISTS uq_atenciones_una_abierta_por_conversacion
    ON atenciones (conversacion_id)
    WHERE estado <> 'resuelta';

-- Verificación (solo lectura), después de correr lo de arriba:
--
--     SELECT enum_range(NULL::motivo_pausa);
--         -- tiene que incluir atencion_crm
--     SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'atenciones';
--         -- atenciones_pkey, ix_atenciones_conversacion_id y
--         -- uq_atenciones_una_abierta_por_conversacion (con su WHERE)

-- DOWN — leer entero antes de revertir nada.
--
-- Qué es irreversible y qué no:
--
-- - El valor 'atencion_crm' de motivo_pausa NO se puede quitar con una
--   sentencia: Postgres no tiene ALTER TYPE ... DROP VALUE. Quitarlo exige
--   recrear el tipo (crear uno nuevo sin el valor, pasar
--   conversaciones.motivo_pausa al tipo nuevo con ALTER COLUMN ... TYPE ...
--   USING, borrar el viejo y renombrar). Eso bloquea `conversaciones`
--   mientras dura, y falla si alguna fila todavía usa el valor. No hace
--   falta hacerlo: el valor sin usar es inofensivo para el código anterior.
--
-- - Lo que SÍ rompe al código anterior es una fila que lo use. SQLAlchemy no
--   sabe leer un valor que su enum no conoce: el bot fallaría al procesar
--   mensajes de esa conversación. Por eso, antes de revertir el código,
--   ninguna conversación puede quedar con motivo_pausa = 'atencion_crm'.
--   Chequeo (solo lectura):
--
--       SELECT id, identificador_externo FROM conversaciones
--       WHERE motivo_pausa = 'atencion_crm';
--
--   y para cada una, resolver la atención desde el panel o correr
--   scripts/resetear_modo_humano.py <identificador>, con el código NUEVO
--   todavía desplegado.
--
-- - Chequeo aparte, y OBLIGATORIO además del de arriba: el código viejo no
--   conoce la tabla `atenciones` en absoluto, así que nunca la resuelve. Si
--   se revierte con una atención abierta cuyo motivo_pausa es 'escalamiento'
--   (la abrió el bot al derivar, no el CRM — no aparece en el chequeo de
--   arriba, que solo mira 'atencion_crm'), el código viejo sí sabe apagar
--   modo_humano ("Reactivar bot" viejo, o el script), pero la fila de
--   `atenciones` se queda pendiente o en_atención para siempre: el panel
--   mostraría una tarjeta abierta sobre una conversación que el bot ya está
--   contestando, y un escalamiento nuevo de esa conversación no abriría
--   ninguna tarjeta (`abrir_por_escalamiento` no crea una segunda si ya hay
--   una abierta). Antes de revertir el código, ninguna atención puede
--   quedar sin resolver, sea cual sea su motivo. Chequeo (solo lectura):
--
--       SELECT id, conversacion_id, estado, motivo FROM atenciones
--       WHERE estado <> 'resuelta';
--
--   y para cada una, resolverla desde el panel o con
--   scripts/resetear_modo_humano.py <identificador>, con el código NUEVO
--   todavía desplegado — antes de bajar a una versión que no sabe leer la
--   tabla. El primer chequeo (motivo_pausa = 'atencion_crm') queda como
--   caso particular de este: toda fila que aparezca ahí también tiene que
--   aparecer acá, nunca al revés.
--
-- - La tabla sí se puede borrar, pero se lleva todo el historial de
--   atenciones (quién tomó y resolvió cada una, con motivo y resumen). El
--   esquema es reversible; esos datos no. Solo si de verdad se abandona la
--   funcionalidad:
--
--       DROP TABLE IF EXISTS atenciones;
--
--   Si solo se revierte el código por un rato, conviene NO borrarla: el
--   código anterior no la mira, y al volver a desplegar sigue ahí.
