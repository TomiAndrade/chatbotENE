-- Migración de esquema para el control preventivo de gasto de WhatsApp/Meta,
-- etapa 1 (specs/spec-costo-whatsapp-meta.md).
--
-- Tabla nueva, no un ALTER TABLE sobre algo existente: `create_all`
-- (app/db.py:init_db) la crea sola en cualquier base, incluida una que ya
-- tiene `conversaciones`/`mensajes` — a diferencia de
-- scripts/migracion_agrupamiento.sql, esto no hace falta correrlo para que
-- la app arranque. Se deja igual, por consistencia con
-- scripts/migracion_metricas_ia.sql y para tener un DOWN documentado que no
-- dependa de recordar el nombre exacto de la tabla.
--
-- No se corrió contra ninguna base real como parte de esta entrega.
--
-- Uso: psql "$DATABASE_URL" -f scripts/migracion_costo_whatsapp.sql

-- UP
CREATE TABLE IF NOT EXISTS envios_whatsapp (
    id SERIAL PRIMARY KEY,
    conversacion_id INTEGER NOT NULL REFERENCES conversaciones(id),
    mensaje_id INTEGER NOT NULL UNIQUE REFERENCES mensajes(id),
    wa_message_id VARCHAR NOT NULL UNIQUE,
    categoria VARCHAR NOT NULL,
    tarifa_ars NUMERIC(12, 4) NOT NULL,
    costo_estimado_ars NUMERIC(12, 4) NOT NULL,
    creado_en TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_envios_whatsapp_conversacion_id ON envios_whatsapp (conversacion_id);
-- Sin CREATE INDEX para wa_message_id: el UNIQUE de la columna ya crea su
-- propio índice en Postgres, uno más acá sería redundante (mismo motivo por
-- el que mensaje_id, arriba, tampoco tiene un CREATE INDEX propio).
CREATE INDEX IF NOT EXISTS ix_envios_whatsapp_creado_en ON envios_whatsapp (creado_en);

-- DOWN (revertir; correr siempre junto con un revert del código, nunca
-- solo — el código de esta entrega no arranca sin esta tabla en cuanto
-- enviar_y_guardar intente escribir en ella tras un envío aceptado por Meta)
-- DROP TABLE IF EXISTS envios_whatsapp;
