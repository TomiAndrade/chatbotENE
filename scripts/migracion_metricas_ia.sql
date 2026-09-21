-- Migración de esquema para el dashboard de costos y actividad del CRM
-- (specs/spec-dashboard-metricas.md).
--
-- Tabla nueva, no un ALTER TABLE sobre algo existente: `create_all`
-- (app/db.py:init_db) la crea sola en cualquier base, incluida una que ya
-- tiene `conversaciones`/`mensajes` — a diferencia de
-- scripts/migracion_agrupamiento.sql, esto no hace falta correrlo para que
-- la app arranque. Se deja igual, por consistencia con el resto del repo y
-- para tener un DOWN documentado que no dependa de recordar el nombre exacto
-- de la tabla y sus dos enums.
--
-- No se corrió contra ninguna base real como parte de esta entrega.
--
-- Uso: psql "$DATABASE_URL" -f scripts/migracion_metricas_ia.sql

-- UP
CREATE TYPE resultado_llamada_ia AS ENUM ('ok', 'error_transitorio', 'error', 'vacio');

CREATE TABLE IF NOT EXISTS llamadas_ia (
    id SERIAL PRIMARY KEY,
    conversacion_id INTEGER NOT NULL REFERENCES conversaciones(id),
    proveedor VARCHAR NOT NULL,
    modelo VARCHAR,
    creado_en TIMESTAMPTZ NOT NULL DEFAULT now(),
    duracion_ms INTEGER,
    resultado resultado_llamada_ia NOT NULL,
    escalo BOOLEAN NOT NULL DEFAULT false,
    tokens_entrada INTEGER,
    tokens_salida INTEGER
);

CREATE INDEX IF NOT EXISTS ix_llamadas_ia_conversacion_id ON llamadas_ia (conversacion_id);
CREATE INDEX IF NOT EXISTS ix_llamadas_ia_proveedor ON llamadas_ia (proveedor);
CREATE INDEX IF NOT EXISTS ix_llamadas_ia_modelo ON llamadas_ia (modelo);
CREATE INDEX IF NOT EXISTS ix_llamadas_ia_creado_en ON llamadas_ia (creado_en);

-- DOWN (revertir; correr siempre junto con un revert del código, nunca
-- solo — el código de esta entrega no arranca sin esta tabla en cuanto
-- responder() intente escribir en ella)
-- DROP TABLE IF EXISTS llamadas_ia;
-- DROP TYPE IF EXISTS resultado_llamada_ia;
