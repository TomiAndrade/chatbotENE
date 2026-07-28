# Bot WhatsApp — ENE IA LAB

Bot de WhatsApp (Kapso) → servidor FastAPI → base de datos → respuesta.
Etapa 1 (plomería) y etapa 2 (IA, historial y escalamiento) implementadas —
ver `spec-etapa1.md` y `spec-etapa2.md` para el detalle completo.

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

- `KAPSO_API_KEY` y `KAPSO_PHONE_NUMBER_ID`: los del proyecto/número sandbox en
  el dashboard de Kapso.
- `KAPSO_WEBHOOK_SECRET`: se genera al configurar el webhook (paso 5). Se
  puede dejar vacío para levantar el server antes de tenerlo, pero entonces
  **no se verifica la firma de los webhooks entrantes** y cualquiera que
  conozca la URL puede inyectar mensajes falsos. Por eso el server solo lo
  permite con `DEBUG=true`: con `DEBUG=false` y el secreto vacío, todos los
  webhooks se rechazan con 401.

### 3. Base de datos

No hace falta nada manual: la primera vez que arranca el servidor crea
`bot.db` (SQLite) y las tablas solas.

### 4. Levantar el servidor

```bash
uvicorn app.main:app --reload
```

Verificar que responde: `GET http://localhost:8000/health` → `{"status": "ok"}`.

### 5. Levantar ngrok y configurar el webhook en Kapso

```bash
ngrok http 8000
```

Copiar la URL `https://...ngrok...` que muestra ngrok. En el dashboard de
Kapso, en el número sandbox, configurar el webhook destination con:

- URL: `https://<tu-url-de-ngrok>/webhook`
- Evento: `whatsapp.message.received`

Copiar el secreto que Kapso genera para ese webhook a `KAPSO_WEBHOOK_SECRET`
en `.env` y reiniciar el servidor.

El panel de ngrok en `http://localhost:4040` muestra cada request entrante —
útil para ver si Kapso está pegándole al webhook y con qué payload, si algo
no anda.

## Probar el flujo

1. Escribir al número sandbox desde un celular — el mensaje debe aparecer en
   la consola del servidor.
2. Confirmar que quedó guardado: `sqlite3 bot.db "select * from mensajes;"`.
3. Debe llegar una respuesta fija al WhatsApp del celular.
4. Marcar modo humano a mano para un número:
   ```sql
   sqlite3 bot.db "update conversaciones set modo_humano = 1 where telefono = '<numero>';"
   ```
   El bot deja de responder a ese número.
5. Reenviar el mismo evento desde el panel de ngrok (Replay) no debe crear un
   mensaje duplicado — se descarta por `wa_message_id` repetido.

## Cambiar de SQLite a Postgres

Cambiar `DATABASE_URL` en `.env` (ej.
`postgresql://usuario:password@host:5432/nombre_db`). No hay que tocar código.

## Cambiar el proveedor de respuestas

`PROVEEDOR_IA` selecciona la implementación: `fijo` (sin IA, para tests),
`gemini` (desarrollo, free tier) o `claude` (producción). Completar
`ANTHROPIC_API_KEY` o `GEMINI_API_KEY` según corresponda, y `MODELO` con el
nombre del modelo de esa API. Agregar un proveedor nuevo es sumar una clase
en `app/proveedor_<nombre>.py` y registrarla en `app/respuesta.py` — el resto
del proyecto no cambia.

## Correr los tests

```bash
pytest
```

No pegan a ninguna API real: Kapso se mockea (`kapso_enviados` en
`tests/conftest.py`) y el proveedor de IA se mockea por test cuando hace
falta (`fijo` no necesita mock). Usan una base SQLite aparte, en un
directorio temporal, no `bot.db`.
