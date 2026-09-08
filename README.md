# Bot WhatsApp — ENE IA LAB

Bot de WhatsApp (Cloud API de Meta, conexión directa) → servidor FastAPI →
base de datos → respuesta. Etapa 1 (plomería), etapa 2 (IA, historial y
escalamiento) y la migración de Kapso a Meta implementadas — ver
`spec-etapa1.md`, `spec-etapa2.md` y `spec-meta-cloud-api.md` para el detalle
completo. `app/kapso.py` y sus tests siguen en el repo, sin usarse, hasta
validar Meta en producción.

## Setup

### 1. Instalar dependencias

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
```

### 2. Variables de entorno

```bash
copy .env.example .env
```

Completar en `.env`:

- `META_PHONE_NUMBER_ID`: el ID del número, del panel de developers.facebook.com
  (app de WhatsApp Business → API Setup).
- `META_ACCESS_TOKEN`: token permanente del System User (no el temporal de 24hs
  que da la consola de pruebas).
- `META_APP_SECRET`: App Secret de la app, en Configuración básica del panel.
  Se puede dejar vacío para levantar el server antes de tenerlo, pero entonces
  **no se verifica la firma de los webhooks entrantes** y cualquiera que
  conozca la URL puede inyectar mensajes falsos. Por eso el server solo lo
  permite con `DEBUG=true`: con `DEBUG=false` y el secreto vacío, todos los
  webhooks se rechazan con 401.
- `META_VERIFY_TOKEN`: lo inventás vos (cualquier string) y lo cargás igual
  acá y en el panel al configurar la URL del webhook (paso 5).

### 3. Base de datos

Hace falta una instancia de Postgres, **incluso para desarrollar local** —
no hay modo SQLite (spec-validacion-config-arranque.md: "sin modo dev", la
validación al arrancar exige Postgres en todos los entornos, sin ninguna
variable que lo relaje). Completar `DATABASE_URL` en `.env`:

```
DATABASE_URL=postgresql://usuario:password@host:5432/nombre_db
```

Puede ser una instancia local o una compartida — lo único que importa es que
sea Postgres real. Sin `DATABASE_URL`, o con cualquier otro esquema (SQLite
incluido), el server no levanta: `validar_config()` corta el arranque y
loguea qué falta, antes de tocar la base.

No hace falta nada manual más allá de eso: la primera vez que arranca el
servidor, `init_db()` crea las tablas solo.

### 4. Levantar el servidor

```bash
uvicorn app.main:app --reload
```

Verificar que responde: `GET http://localhost:8000/health` → `{"status": "ok"}`.

### 5. Levantar ngrok y configurar el webhook en el panel de Meta

```bash
ngrok http 8000
```

Copiar la URL `https://...ngrok...` que muestra ngrok. En
developers.facebook.com, en la app de WhatsApp Business → Configuration,
configurar el webhook con:

- Callback URL: `https://<tu-url-de-ngrok>/webhook`
- Verify token: el mismo valor que `META_VERIFY_TOKEN` en `.env`
- Suscribirse al campo `messages`

Meta valida la URL con un `GET /webhook` antes de guardarla — si
`META_VERIFY_TOKEN` no coincide, el panel muestra error y no la acepta.

**Nota sobre `whatsapp.message.sent` (pausa por intervención manual):** con
Kapso, un evento de ese tipo permitía detectar cuándo la secretaría
respondía desde la app de WhatsApp Business y pausar al bot (ver
spec-pausa-por-intervencion-humana.md). La Cloud API de Meta no tiene ese
disparador — no hay app de negocio, los mensajes salen por API o no salen
(spec-meta-cloud-api.md, sección 5). Esa función (`procesar_mensaje_saliente`)
sigue en el código, dormida, por si más adelante Meta habilita Coexistence.

El panel de ngrok en `http://localhost:4040` muestra cada request entrante —
útil para ver si Meta está pegándole al webhook y con qué payload, si algo
no anda.

## Probar el flujo

1. Escribir al número sandbox desde un celular — el mensaje debe aparecer en
   la consola del servidor.
2. Confirmar que quedó guardado: `psql "$DATABASE_URL" -c "select * from mensajes;"`.
3. Debe llegar una respuesta fija al WhatsApp del celular.
4. Marcar modo humano a mano para un número. `motivo_pausa` se setea
   explícito a `'escalamiento'` para que no dependa de quedar `NULL` por
   default — con `NULL` la pausa igual no expira (`_pausa_vigente` en
   `app/main.py` lo trata como si no expirara), pero dejarlo así en una
   fila creada a mano es un dato indefinido, no una decisión:
   ```sql
   psql "$DATABASE_URL" -c "update conversaciones set modo_humano = 1, motivo_pausa = 'escalamiento' where identificador_externo = '<numero>';"
   ```
   El bot deja de responder a ese número. Para desmarcarlo, usar
   `scripts/resetear_modo_humano.py <numero>` en vez de otro UPDATE a mano —
   limpia motivo_pausa, modo_humano_desde, resumen_escalamiento y
   escalada_en juntos.
5. Reenviar el mismo evento desde el panel de ngrok (Replay) no debe crear un
   mensaje duplicado — se descarta por `wa_message_id` repetido.

## Postgres — notas y checklist manual antes de deployar

`DATABASE_URL` siempre apunta a Postgres, en local y en producción (ver
"Base de datos" arriba — no hay modo SQLite). En Render la variable se
enlaza desde la base administrada, no se escribe a mano. Render la entrega
con el esquema `postgresql://`, que es el que SQLAlchemy espera y el que
`validar_config()` exige: se usa tal cual viene, sin reescribir nada
(verificado contra la base real, `ene-bot-db`).

Las tablas las crea `init_db()` al levantar la app. **No hay migraciones**:
`create_all` crea lo que falta pero no modifica nada existente, así que
cualquier cambio de esquema —incluido agregar un valor a `RolMensaje` o a
`motivo_pausa`, que en Postgres son tipos nativos— hay que aplicarlo a mano
con SQL. Ver PENDIENTES.md.

Antes de deployar conviene validar a mano contra la base de Render, porque
hay diferencias de comportamiento entre motores que ningún test cubre — la
suite corre contra SQLite (ver "Correr los tests" abajo), a propósito, y no
contra la base real:

1. Apuntar `DATABASE_URL` a la base de Render desde la máquina local.
2. Arrancar la app y confirmar que `init_db()` crea las tablas sin error.
3. Guardar una conversación (con `modo_humano_desde`) y un mensaje.
4. Releerlos y confirmar que las fechas vuelven con `tzinfo` **no nulo**.
5. Insertar dos mensajes con el mismo `wa_message_id`: el segundo tiene que
   tirar `IntegrityError` y el `rollback` dejar la sesión usable. Postgres
   aborta la transacción entera ante una constraint violada, más estricto que
   SQLite — hay que confirmar que el camino de idempotencia de
   `procesar_mensaje_entrante` sigue funcionando igual.

`scripts/verificar_postgres.py` automatiza este checklist contra una base
real (no se corre solo ni en CI).

## Cambiar el proveedor de respuestas

`PROVEEDOR_IA` selecciona la implementación: `fijo` (sin IA, para tests),
`openai_compat` (desarrollo, cualquier endpoint con formato de la API de
OpenAI — OpenRouter, DeepSeek, el free de NVIDIA, un modelo local) o `claude`
(producción). Con `openai_compat` completar `BASE_URL`, `OPENAI_COMPAT_API_KEY`
y `MODELO`; con `claude`, `ANTHROPIC_API_KEY` y `MODELO`. Agregar un proveedor
nuevo es sumar una clase en `app/proveedor_<nombre>.py` y registrarla en
`app/respuesta.py` — el resto del proyecto no cambia.

## Correr los tests

```bash
pytest
```

No pegan a ninguna API real: Meta se mockea (`meta_enviados` en
`tests/conftest.py`) y el proveedor de IA se mockea por test cuando hace
falta (`fijo` no necesita mock). Usan una base SQLite aparte, en un
directorio temporal — es la única excepción a "Postgres siempre" de arriba:
la suite fija `DATABASE_URL` antes de importar `app.config` y no pasa por
`al_iniciar()` (ver `tests/conftest.py`), así que nunca llega a pasar por
`validar_config()`.
