# Spec — Migración a PostgreSQL

**Objetivo:** correr sobre PostgreSQL en Render sin cambiar la lógica de la aplicación.

**Contexto:** en Render el filesystem es efímero. El archivo `bot.db` se borra en cada deploy, así que SQLite no es viable en producción. La base arranca vacía: no hay datos que migrar.

**Alcance:** `app/db.py`, `app/models.py`, `app/config.py`, `.env.example`, dependencias. No se toca la lógica de negocio.

---

## 1. Driver y esquema de la URL ⚠️

Render entrega la connection string con el prefijo **`postgres://`**. SQLAlchemy dejó de aceptar ese esquema y espera **`postgresql://`**. Si se pasa tal cual, falla al arrancar con `Can't load plugin: sqlalchemy.dialects:postgres`.

En `app/db.py`, normalizar antes de crear el engine:

```python
url = config.database_url
if url.startswith("postgres://"):
    url = url.replace("postgres://", "postgresql://", 1)
```

Hacerlo en el código y no a mano en la variable de entorno: Render regenera esa URL cuando rota credenciales, y volvería a venir con el prefijo viejo.

Agregar `psycopg[binary]` a las dependencias y usar `postgresql+psycopg://` como dialecto explícito.

---

## 2. Configuración del engine

```python
engine = create_engine(
    url,
    connect_args=connect_args,
    pool_pre_ping=True,
    pool_recycle=300,
)
```

**`pool_pre_ping`** es necesario: el servicio pasa horas sin tráfico y Render corta las conexiones ociosas. Sin esto, el primer webhook después de un rato de silencio falla con la conexión muerta y el mensaje se pierde.

`connect_args` con `check_same_thread` sigue aplicando **solo** a SQLite. Mantener el condicional actual.

---

## 3. Fechas con zona horaria ⚠️ — el punto crítico

Revisar **todas** las columnas de fecha en `app/models.py`:

- `Conversacion.modo_humano_desde`
- `Conversacion.creada_en`
- `Conversacion.ultimo_mensaje_en`
- `Conversacion.escalada_en`
- `Mensaje.creado_en`

Todas tienen que ser **`DateTime(timezone=True)`**.

SQLite ignora la distinción, así que esto viene funcionando por accidente. Postgres no: con `DateTime` a secas guarda `TIMESTAMP WITHOUT TIME ZONE`, y al leer devuelve un datetime naive. Comparar eso contra el `datetime.now(timezone.utc)` que usa `_pausa_vigente` tira `TypeError: can't compare offset-naive and offset-aware datetimes`.

Eso rompe la pausa por intervención humana y el chequeo de escalamiento, en runtime y no en tests.

**Verificación obligatoria:** después del cambio, guardar una conversación con `modo_humano_desde`, releerla desde Postgres y confirmar que `tzinfo` no es `None`.

---

## 4. Enums

`RolMensaje` y el enum de `motivo_pausa` se van a crear como tipos nativos de Postgres. `create_all` los genera bien la primera vez.

**Consecuencia a documentar:** agregar un valor nuevo a cualquiera de esos enums más adelante va a requerir un `ALTER TYPE` a mano, porque `create_all` no modifica tipos existentes. No es un problema hoy; es un problema el día que se agregue un rol o un motivo.

Anotarlo en `PENDIENTES.md`.

---

## 5. Sin Alembic, por ahora

Para este deploy `create_all` alcanza: la base arranca vacía y el esquema no cambia.

**No agregar Alembic en este cambio.** Es scope creep con la fecha encima.

Pero anotarlo en `PENDIENTES.md` como prioritario post-lanzamiento: desde el momento en que haya conversaciones reales guardadas, cualquier cambio de esquema pasa a ser manual y riesgoso.

---

## 6. Tests

Los tests siguen corriendo contra SQLite en memoria. No cambiarlos.

Pero hay que validar a mano contra Postgres antes del deploy, porque las diferencias de esta migración son justamente las que SQLite no reproduce:

1. Levantar la base de Render y apuntar `DATABASE_URL` desde la máquina local.
2. Arrancar la app y confirmar que `init_db()` crea las tablas sin error.
3. Guardar una conversación y un mensaje.
4. Releer y confirmar que las fechas vuelven con `tzinfo`.
5. Insertar dos mensajes con el mismo `wa_message_id` y confirmar que el segundo tira `IntegrityError` y que el `rollback` deja la sesión usable.

El punto 5 importa: Postgres aborta la transacción entera ante una constraint violada, más estricto que SQLite. Hay que confirmar que el camino de idempotencia en `procesar_mensaje_entrante` sigue funcionando igual.

---

## 7. Config

`DATABASE_URL` ya existe y no cambia de nombre. Mantener el default de SQLite para desarrollo local.

En `.env.example`, documentar el formato de Postgres y aclarar que en Render la variable se toma de la base administrada, no se escribe a mano.

---

## Verificación

- [ ] La app arranca con `DATABASE_URL` de Postgres y crea las tablas
- [ ] Una URL con prefijo `postgres://` se normaliza y funciona
- [ ] Las fechas vuelven de la base con `tzinfo` no nulo
- [ ] `_pausa_vigente` funciona contra una conversación leída de Postgres
- [ ] Dos mensajes con el mismo `wa_message_id`: el segundo se descarta limpio
- [ ] Los 107 tests siguen pasando en SQLite

---

## Nota posterior — 2026-09-08

**La sección 1 quedó sin efecto.** Se verificó contra la base real de Render
(`ene-bot-db`) que la connection string viene con el esquema
**`postgresql://`**, no `postgres://`. La premisa de esa sección era
defensiva: se escribió sin haber visto una URL de Render de verdad —el
servicio todavía no existía— y ningún log ni incidente la respaldó nunca.

En consecuencia se borró `normalizar_url` de `app/db.py` junto con sus tests
(`tests/test_db_url.py`) y las menciones al prefijo viejo en `README.md`,
`.env.example` y `PENDIENTES.md`. La URL se usa tal cual viene, y
`validar_config()` (spec-validacion-config-arranque.md) exige `postgresql://`
sobre el valor crudo, sin normalización de por medio.

El resto del documento queda como está, como registro de lo que se decidió en
la migración.
