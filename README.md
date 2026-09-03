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

No hace falta nada manual: la primera vez que arranca el servidor crea
`bot.db` (SQLite) y las tablas solas.

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
2. Confirmar que quedó guardado: `sqlite3 bot.db "select * from mensajes;"`.
3. Debe llegar una respuesta fija al WhatsApp del celular.
4. Marcar modo humano a mano para un número. `motivo_pausa` se setea
   explícito a `'escalamiento'` para que no dependa de quedar `NULL` por
   default — con `NULL` la pausa igual no expira (`_pausa_vigente` en
   `app/main.py` lo trata como si no expirara), pero dejarlo así en una
   fila creada a mano es un dato indefinido, no una decisión:
   ```sql
   sqlite3 bot.db "update conversaciones set modo_humano = 1, motivo_pausa = 'escalamiento' where identificador_externo = '<numero>';"
   ```
   El bot deja de responder a ese número. Para desmarcarlo, usar
   `scripts/resetear_modo_humano.py <numero>` en vez de otro UPDATE a mano —
   limpia motivo_pausa, modo_humano_desde, resumen_escalamiento y
   escalada_en juntos.
5. Reenviar el mismo evento desde el panel de ngrok (Replay) no debe crear un
   mensaje duplicado — se descarta por `wa_message_id` repetido.

## Cambiar de SQLite a Postgres

Cambiar `DATABASE_URL` en `.env` (ej.
`postgresql://usuario:password@host:5432/nombre_db`). No hay que tocar código.

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
directorio temporal, no `bot.db`.
