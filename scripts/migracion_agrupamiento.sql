-- Migración de esquema para la entrega 1.2 de specs/roadmap-bot-crm.md
-- (agrupar y ordenar mensajes consecutivos). Ver specs/spec-agrupamiento-mensajes.md.
--
-- `create_all` (app/db.py:init_db) solo crea columnas en una tabla que
-- todavía no existe: una base Postgres que ya tiene `conversaciones` y
-- `mensajes` (cualquier despliegue real, no un test) necesita este ALTER
-- TABLE a mano antes de correr el código de esta entrega. No se corrió
-- contra ninguna base real como parte de esta entrega.
--
-- Las cuatro columnas son nullable y sin default obligatorio: agregarlas no
-- toca ninguna fila existente.
--
-- Uso: psql "$DATABASE_URL" -f scripts/migracion_agrupamiento.sql

-- UP
ALTER TABLE conversaciones ADD COLUMN IF NOT EXISTS generando_desde TIMESTAMPTZ;
ALTER TABLE conversaciones ADD COLUMN IF NOT EXISTS generando_token VARCHAR;
ALTER TABLE conversaciones ADD COLUMN IF NOT EXISTS ultimo_mensaje_agrupado_id INTEGER;
ALTER TABLE mensajes ADD COLUMN IF NOT EXISTS tipo VARCHAR;

-- DOWN (revertir; correr siempre junto con un revert del código, nunca
-- solo — el código de esta entrega no arranca sin estas columnas)
-- ALTER TABLE mensajes DROP COLUMN IF EXISTS tipo;
-- ALTER TABLE conversaciones DROP COLUMN IF EXISTS ultimo_mensaje_agrupado_id;
-- ALTER TABLE conversaciones DROP COLUMN IF EXISTS generando_token;
-- ALTER TABLE conversaciones DROP COLUMN IF EXISTS generando_desde;
