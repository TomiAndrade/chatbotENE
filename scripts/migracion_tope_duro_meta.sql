-- Migración de esquema para el bloqueo duro mensual de gasto de WhatsApp/Meta,
-- etapa 2.1 (specs/spec-tope-duro-meta.md).
--
-- Tabla nueva, no un ALTER TABLE sobre algo existente: `create_all`
-- (app/db.py:init_db) la crea sola en cualquier base, incluida una que ya
-- tiene el resto del esquema — no hace falta correrla para que la app
-- arranque. Se deja igual, por consistencia con
-- scripts/migracion_costo_whatsapp.sql y scripts/migracion_metricas_ia.sql,
-- y para tener un DOWN documentado que no dependa de recordar el nombre
-- exacto de la tabla.
--
-- Esta migración es independiente de scripts/migracion_costo_whatsapp.sql
-- (etapa 1, tabla envios_whatsapp) — no la reemplaza ni la modifica.
--
-- No se corrió contra ninguna base real como parte de esta entrega. El
-- mecanismo que crea esta tabla solo se ejercita con
-- META_TOPE_DURO_HABILITADO=true, que además sigue apagado por default.
--
-- Uso: psql "$DATABASE_URL" -f scripts/migracion_tope_duro_meta.sql

-- UP
CREATE TABLE IF NOT EXISTS presupuesto_meta_mensual (
    id SERIAL PRIMARY KEY,
    mes VARCHAR NOT NULL UNIQUE,
    presupuesto_ars NUMERIC(12, 4) NOT NULL,
    costo_comprometido_ars NUMERIC(12, 4) NOT NULL DEFAULT 0,
    creado_en TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Sin CREATE INDEX para mes: la UNIQUE ya crea su propio índice en
-- Postgres, uno aparte sería redundante (mismo criterio que
-- scripts/migracion_costo_whatsapp.sql aplicó a wa_message_id).

-- DOWN (revertir; correr siempre junto con un revert del código, nunca
-- solo — con META_TOPE_DURO_HABILITADO=true el código de esta entrega no
-- arranca sin esta tabla en cuanto enviar_y_guardar intente reservar)
-- DROP TABLE IF EXISTS presupuesto_meta_mensual;
