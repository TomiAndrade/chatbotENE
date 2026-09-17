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

## CRM — panel de conversaciones

El panel para leer lo que el bot viene conversando y reactivarlo cuando quedó
pausado (ver `specs/spec-crm-conversaciones.md`). Corre dentro de la misma app:
no hay un segundo servicio que levantar ni un build que correr.

**El acceso es con usuario y contraseña propios**, uno por persona. Las
cuentas viven en la base (`crm_usuarios`) y se crean **desde la consola**: no
hay registro público, ni alta desde el panel, ni ninguna contraseña en el
`.env`.

**Viene apagado.** Con `CRM_HABILITADO=false` (el default) no se registra
ninguna ruta y `/crm` responde 404 — el servidor está publicado en internet
para que le pegue Meta, así que el panel existe solo si alguien lo prende.

### 1. Configurar el servidor

En `.env` (ver `.env.example` para el detalle de cada una):

```
CRM_HABILITADO=true
CRM_BASE_URL=https://TU-DOMINIO   # sin barra final ni ruta
```

Y nada más: **no hay ninguna credencial del panel en la configuración**.

`CRM_BASE_URL` decide una sola cosa: si la cookie de sesión sale con el flag
`Secure`. Por eso **tiene que ser https en producción** — con http fuera de
localhost el servidor no arranca y dice por qué. Para probar en
`http://localhost:8000` hace falta además `DEBUG=true`.

Si algo falta o es incoherente, el server **no levanta** (misma regla que el
resto de la config, `spec-validacion-config-arranque.md`).

### 2. Crear la primera cuenta

Con el `.env` cargado y desde `chatbot-polo/`:

```bash
python scripts/crm_usuario.py crear NOMBREDEUSUARIO
```

El comando pide la contraseña **dos veces y sin mostrarla**, y guarda solo su
hash Argon2id. **La contraseña no se pasa como argumento** (quedaría en el
historial de la consola y en la lista de procesos), no se imprime y no se
loguea. Mínimo 12 caracteres.

El mismo comando crea el resto de las cuentas del equipo, una por persona.
Nada de cuentas compartidas.

Si el panel arranca sin ninguna cuenta, el log lo avisa con un WARNING: nadie
va a poder entrar, y el login no dice por qué a propósito.

### 3. Dar y quitar acceso

```bash
python scripts/crm_usuario.py listar                      # qué cuentas hay
python scripts/crm_usuario.py crear NOMBREDEUSUARIO       # dar acceso
python scripts/crm_usuario.py cambiar-password NOMBREDEUSUARIO
python scripts/crm_usuario.py desactivar NOMBREDEUSUARIO  # quitar acceso
python scripts/crm_usuario.py activar NOMBREDEUSUARIO     # devolverlo
```

**Cambiar la contraseña y desactivar una cuenta cierran las sesiones abiertas
de esa persona en el acto**, sin reiniciar el servidor: la que tenga la
cookie puesta queda afuera en su próximo click.

Para cortar un acceso a mano desde la base (por ejemplo, si no hay consola de
Python a mano):

```sql
update crm_usuarios set activo = false where usuario = 'NOMBREDEUSUARIO';
update crm_sesiones set revocada_en = now()
  where usuario_id = (select id from crm_usuarios where usuario = 'NOMBREDEUSUARIO');
```

No hay recuperación de contraseña por mail: si alguien la olvida, se la
cambia con `cambiar-password`.

### 4. Respaldo

Las cuentas viven en la **misma base que las conversaciones**, así que el
respaldo es el de siempre:

```bash
pg_dump "$DATABASE_URL" > respaldo-$(date +%F).sql
```

Conviene hacerlo **antes** de prender el panel la primera vez y antes de
cualquier cambio de esquema. El dump incluye `crm_usuarios`: son hashes, no
contraseñas, pero es material para un ataque de diccionario offline — se
guarda con el mismo cuidado que el resto del dump.

### 5. Abrirlo y probar el acceso

Con el server levantado (`uvicorn app.main:app --reload`):

```
http://localhost:8000/crm
```

Lleva a la pantalla de entrar. La sesión dura **12 horas** y no se renueva
con el uso: al vencer hay que volver a entrar.

Vale la pena probar esto a mano la primera vez, en ese orden:

1. Entrar con la cuenta recién creada.
2. Abrir una conversación y leer el historial.
3. Si hay una pausada, apretar **Reactivar bot** (no manda ningún mensaje por
   WhatsApp: del otro lado no pasa nada).
4. Apretar **Salir** y confirmar que volver atrás en el navegador ya no
   muestra el panel.
5. Escribir mal la contraseña cinco veces: al sexto intento el panel contesta
   "demasiados intentos" durante unos minutos, incluso con la contraseña
   correcta.

### 6. Tablas nuevas

El panel agrega tres tablas: `crm_usuarios` (las cuentas), `crm_sesiones`
(las sesiones abiertas) y `crm_intentos_login` (el límite de intentos). Las
crea `init_db()` al arrancar, igual que las del bot: `create_all` agrega lo
que falta y **no toca `conversaciones` ni `mensajes`**. Las dos últimas son
descartables — vaciarlas solo obliga a volver a loguearse.

**Si esa base tuvo alguna vez el CRM con Auth0**, el servidor no arranca y
dice qué borrar: este proyecto no tiene migraciones, así que una
`crm_sesiones` de aquella época quedaría con su esquema y sus filas viejas.

```sql
DROP TABLE IF EXISTS crm_transacciones_oidc;
DROP TABLE IF EXISTS crm_sesiones;
```

### Qué se puede hacer en el panel

- Ver la lista de conversaciones, ordenada por actividad más reciente, y
  filtrar entre todas y las pausadas.
- Leer el historial completo, con los mensajes del usuario, del bot y de una
  persona del equipo diferenciados.
- Ver por qué está pausada una conversación y, si el bot la escaló, el resumen
  que dejó el modelo.
- **Reactivar bot**: levanta la pausa. No manda ningún mensaje ni reprocesa lo
  que llegó durante la pausa — el bot responde recién con el próximo mensaje
  entrante. Es lo mismo que hace `scripts/resetear_modo_humano.py`, con la
  misma función por debajo.

No se puede responder desde el panel: para eso está WhatsApp.

### Lo que falta probar

El login está probado con la suite de tests y, además, contra un **Postgres
real** en un contenedor de prueba aislado: creación de las tablas, alta de la
primera cuenta con el comando, login correcto e incorrecto, bloqueo por
intentos, lectura y reactivación de una conversación, y logout con y sin
token CSRF.

Falta probarlo **servido por https con un dominio real** y abrir las
pantallas a mano en un navegador de escritorio y de celular. El checklist
completo para ponerlo en producción está en `PENDIENTES.md`, sección 1.c.

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
