-- Tarea 3A: autoría de respuestas humanas enviadas desde el CRM.
--
-- OBLIGATORIO antes de desplegar el código: `create_all()` no agrega
-- columnas a la tabla `mensajes`, que ya existe en producción.
--
-- No se ejecutó contra ninguna base como parte de esta tarea.
-- Uso recomendado:
--
--   psql -v ON_ERROR_STOP=1 "$DATABASE_URL" -f scripts/migracion_autor_mensajes.sql
--
-- La columna es nullable para no modificar mensajes BOT/USUARIO, mensajes
-- humanos históricos ni intervenciones cuyo autor CRM no puede conocerse.

ALTER TABLE mensajes
    ADD COLUMN IF NOT EXISTS autor_crm_id INTEGER NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'fk_mensajes_autor_crm_id'
          AND conrelid = 'mensajes'::regclass
    ) THEN
        ALTER TABLE mensajes
            ADD CONSTRAINT fk_mensajes_autor_crm_id
            FOREIGN KEY (autor_crm_id) REFERENCES crm_usuarios (id);
    END IF;
END
$$;

-- Verificación de solo lectura:
--
--   SELECT column_name, is_nullable
--   FROM information_schema.columns
--   WHERE table_name = 'mensajes' AND column_name = 'autor_crm_id';
--
--   SELECT conname, pg_get_constraintdef(oid)
--   FROM pg_constraint
--   WHERE conrelid = 'mensajes'::regclass
--     AND conname = 'fk_mensajes_autor_crm_id';

-- DOWN (solo si se abandona la funcionalidad; elimina la autoría guardada):
--
--   ALTER TABLE mensajes DROP CONSTRAINT IF EXISTS fk_mensajes_autor_crm_id;
--   ALTER TABLE mensajes DROP COLUMN IF EXISTS autor_crm_id;
