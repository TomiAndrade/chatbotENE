# Spec — CRM de conversaciones (primera versión)

**Contexto:** hoy la única forma de ver qué viene conversando el bot es abrir
la base con `psql`, y la única forma de despausar una conversación es correr
`scripts/resetear_modo_humano.py` desde una consola con el `.env` cargado. El
equipo de ENE no tiene ni una cosa ni la otra. Este panel es esa ventana:
**leer conversaciones y reactivar el bot**, nada más.

Implementado en la rama `feat/crm-conversaciones`.

---

## 1. Alcance

Lo que el CRM hace:

- Listar conversaciones, de la actividad más reciente a la más vieja.
- Identificar cada contacto por su `identificador_externo` **verbatim**, tal
  como está guardado (el "9" de los números argentinos puede estar o no; ver
  spec-meta-cloud-api.md, sección 3).
- Mostrar el historial con los tres roles diferenciados —usuario, bot y
  persona del equipo— con fecha y hora.
- Distinguir bot activo de bot pausado, con el motivo y el resumen del
  escalamiento cuando existan.
- Filtrar entre todas las conversaciones y las pausadas.
- Reactivar el bot en una conversación pausada.

Lo que **no** hace, y no es un olvido:

- No manda mensajes. No hay campo para escribir: si alguien del equipo quiere
  responder, responde desde WhatsApp.
- No edita contactos, no tiene campañas, embudos, etiquetas ni métricas.
- No cambia el prompt del bot ni prende el escalamiento automático
  (`ESCALAMIENTO_HABILITADO` sigue como estaba).

## 2. Por qué no es un proyecto aparte

El panel vive dentro de la misma app FastAPI, en `app/crm/`, y usa los mismos
modelos y la misma base por SQLAlchemy. No hay build ni framework de frontend:
son tres páginas HTML, una hoja de estilos y dos archivos de JavaScript
servidos por el mismo proceso que atiende el webhook, más endpoints JSON.

Dependencia nueva, una sola y por el login: **argon2-cffi**, el binding
mantenido de la implementación de referencia de Argon2. No se escribe
criptografía a mano, que era la alternativa inaceptable. (La primera versión
del login usó Auth0 y traía `authlib` e `itsdangerous`; las dos se fueron con
él.)

La otra alternativa (un frontend aparte, con su build y su deploy) agregaría
un servicio más para mantener y una superficie más para asegurar, a cambio de
nada que este panel necesite. Si algún día el CRM crece a algo que un
JavaScript a mano no aguante, se separa; hoy no.

## 3. Autenticación: login propio con cuentas individuales

El servidor está publicado en internet para que le pegue Meta, así que el
panel **no puede quedar abierto**.

Cada persona entra con **su propio usuario y su propia contraseña**, contra
una tabla de la misma base (`crm_usuarios`). Las cuentas se crean **solo
desde la consola**: no hay registro público ni alta desde el panel.

### 3.1 Lo que se descartó, y por qué

- **Una contraseña compartida en una variable de entorno** (la primera
  versión del panel). No dice quién hizo qué, no se le puede sacar el acceso
  a una sola persona y circula por WhatsApp hasta que alguien la filtra. No
  quedó ningún resto: el endpoint que la aceptaba se borró y
  `CRM_USUARIO`/`CRM_PASSWORD` no existen en la config.
- **Auth0** (septiembre 2026, implementado y después reemplazado). Resolvía
  bien el problema, pero metía un proveedor externo, dos dependencias y un
  tenant que mantener para cinco personas de una oficina. Se fue entero:
  `app/crm/auth0.py`, `app/crm/almacen_oidc.py`, la pantalla de acceso
  denegado, `authlib`, `itsdangerous`, las variables `CRM_AUTH0_*` y
  `CRM_SECRET_KEY`, y la tabla `crm_transacciones_oidc`.

**Una cookie del mecanismo anterior no da acceso**, por dos caminos
independientes: el hash del token de sesión lleva ahora un separador de
dominio (`sesiones.SEPARADOR_DE_DOMINIO`), así que un `token_hash` calculado
por el código viejo no coincide con ninguna búsqueda; y el arranque **corta
con error** si encuentra las tablas de aquella época, diciendo qué borrar
(`modelos.verificar_esquema`, llamado desde `al_iniciar()`). Este proyecto no
tiene migraciones, así que sin ese chequeo una `crm_sesiones` vieja quedaría
con su esquema y sus filas.

### 3.2 Las contraseñas

**Argon2id**, con `argon2-cffi` (el binding mantenido de la implementación de
referencia; es lo que recomienda OWASP para contraseñas). Parámetros
explícitos en `app/crm/passwords.py`: 64 MiB, 3 pasadas, 4 hilos — la segunda
opción recomendada por la RFC 9106, unos 0,1 s por verificación.

- De la contraseña **solo se guarda el hash**, que incluye su propia sal
  aleatoria. En claro existe nada más que en el momento en que alguien la
  tipea.
- **Mínimo 12 caracteres** y nada más: las reglas de "una mayúscula y un
  símbolo" empujan a contraseñas cortas y previsibles.
- Después de un login correcto se rehashea si los parámetros cambiaron
  (`check_needs_rehash`): es el único momento en que la contraseña está a
  mano.

### 3.3 El comando de consola

`scripts/crm_usuario.py` es **la única puerta de alta**, tanto para la
primera cuenta (la del administrador) como para las del resto del equipo:

```
python scripts/crm_usuario.py crear UNUSUARIO
python scripts/crm_usuario.py cambiar-password UNUSUARIO
python scripts/crm_usuario.py desactivar UNUSUARIO
python scripts/crm_usuario.py activar UNUSUARIO
python scripts/crm_usuario.py listar
```

**La contraseña nunca es un argumento.** Se pide con `getpass` (no se ve
mientras se tipea) y se pide dos veces. Un argumento quedaría en el historial
de la consola, en la lista de procesos y en cualquier log de auditoría del
sistema. Tampoco se imprime ni se loguea, y `listar` no muestra ningún hash.

**Cambiar la contraseña y desactivar una cuenta cierran las sesiones abiertas
de esa persona**, por dos caminos a la vez: se revocan las filas de
`crm_sesiones` y se mueve `credenciales_cambiadas_en`, que
`buscar_sesion_valida` compara contra la fecha de cada sesión. La marca de
tiempo cubre una sesión que hubiera creado otra instancia del servidor entre
el cambio y la revocación; la revocación deja el motivo visible en la base.

### 3.4 Límite de intentos

Contado **contra la base** (`crm_intentos_login`), no en memoria del proceso:
es lo que hace que funcione igual con varias instancias del servidor detrás
de un balanceador, y que sobreviva a un deploy. Misma decisión que
`app/limite.py` para el límite de mensajes por hora.

- **5 intentos fallidos por cuenta** en una ventana deslizante de 15 minutos,
  y **20 por IP** (más alto: en una oficina entra todo el equipo desde la
  misma salida a internet).
- El chequeo va **antes** de mirar la contraseña: con la cuenta bloqueada, la
  contraseña correcta tampoco entra. Si entrara, el bloqueo no frenaría a
  quien acierta en el intento número seis.
- Un login correcto **borra los intentos fallidos de esa cuenta**.
- La IP sale de `request.client.host` y **no** de `X-Forwarded-For`: ese
  header lo pone quien manda el request, y confiar en él permite inventarse
  una IP distinta en cada intento.

### 3.5 Sin enumeración de cuentas

Los tres fallos posibles —la cuenta no existe, está desactivada, la
contraseña está mal— devuelven **el mismo 401 con el mismo texto**. Además:

- cuando la cuenta no existe se verifica igual contra un **hash de relleno**
  (`passwords.quemar_tiempo`), así el login tarda lo mismo y el reloj no dice
  lo que el mensaje calla;
- el límite de intentos se cuenta **por el nombre que se intentó, exista o
  no**, así que ver quién se bloquea y quién no tampoco delata qué cuentas
  hay.

### 3.6 Sesiones del panel

Sesiones **del servidor, en Postgres** (`crm_sesiones`), no un token firmado
en la cookie. Esto no cambió con el reemplazo del login:

- La cookie lleva **solo un identificador aleatorio** (`secrets.token_urlsafe(32)`).
  En la base se guarda su **SHA-256** (con el separador de dominio de 3.1):
  lo que está guardado no sirve para entrar, así que un dump o un backup no
  es una llave.
- **Vencimiento absoluto** de 12 horas, fijado al crearla y sin renovación
  por uso.
- **Sesión nueva después de autenticar** y revocación de la que viniera en la
  cookie: nadie puede plantar una cookie y esperar a que alguien se loguee
  con ella (fijación).
- **Logout**: revoca la fila (la cookie deja de servir en el acto, aunque
  alguien tenga una copia) y borra la cookie. Es `POST` y con token CSRF — un
  logout por `GET` lo dispara cualquier página ajena con una imagen. Ya no
  hay un segundo paso: con Auth0 había que cerrar además la sesión del
  navegador con el proveedor, y esa sesión no existe más.
- **Cookie `HttpOnly`, `Secure` salvo en el http local permitido, `path=/crm`**
  y `SameSite=Lax`. Lax manda la cookie en las navegaciones de arriba por GET
  (entrar desde un favorito o un link compartido) y **no** en un POST que
  nazca en otro sitio, que es lo que importa para CSRF. Con `Strict` la
  persona vería la pantalla de entrar teniendo la sesión abierta: se gana
  poco y se pierde eso.
- **CSRF**: las dos acciones que escriben (reactivar y salir) exigen repetir
  en el header `X-CRM-CSRF` un token que vive en la fila de la sesión y que
  el panel pide por `/crm/api/sesion`.
- **El POST del login es JSON, no un formulario**: un formulario de otro
  sitio puede hacer un POST cruzado sin JavaScript, pero no puede mandar
  `Content-Type: application/json` sin preflight de CORS, y acá no hay CORS
  abierto.
- Las respuestas del panel salen con `Cache-Control: no-store`: traen
  conversaciones de gente.
- **Una sesión deja de valer en cinco casos**, chequeados en cada request:
  no existe, está revocada, venció, la cuenta se desactivó, o la sesión es
  anterior al último cambio de credenciales de esa cuenta.

### 3.7 Configuración

Con el CRM prendido, lo único obligatorio es **`CRM_BASE_URL`** (https, salvo
localhost con `DEBUG=true`). **No hay ninguna credencial del panel en la
config**: las cuentas viven en la base. `validar_config()` corta el arranque
si falta o si es http en producción, y `al_iniciar()` avisa con un WARNING si
el panel está prendido y todavía no hay ninguna cuenta — el síntoma de eso
(nadie puede entrar y el login no dice por qué, a propósito) no se explica
solo.

### 3.8 Lo que no cambió

El webhook queda **independiente**: no comparte autenticación, ni secreto, ni
cookie con el panel. `POST /webhook` se sigue validando con la firma de Meta,
estar logueado en el panel no la saltea, y `/health` no pasa por el login (si
pasara, un problema del panel se vería como si el bot estuviera caído).
Ningún secreto de Meta se usa como credencial del panel.

## 4. Estado de pausa: lo que ve el bot, no la columna

`modo_humano` solo no alcanza para decir si el bot está pausado: la pausa por
intervención manual **expira** a los `PAUSA_HUMANA_MINUTOS` (ver
spec-pausa-por-intervencion-humana.md), y la columna se queda prendida igual.
El panel muestra el estado que calcula `pausa_vigente` (`app/pausa.py`), que
es exactamente el que consulta el bot antes de responder. Si mostrara el flag
crudo, diría "pausado" de conversaciones que el bot ya está atendiendo.

`pausa_vigente` se mudó de `app/main.py` a `app/pausa.py` por eso: lo
necesitan el webhook y el CRM, y `app/main.py` importa el router del CRM, así
que el CRM no puede importar `app/main.py` de vuelta. Es un traslado, no un
rediseño.

## 5. Reactivar

Apaga los cinco campos de la pausa (`modo_humano`, `motivo_pausa`,
`modo_humano_desde`, `resumen_escalamiento`, `escalada_en`) con
`reactivar_bot` (`app/pausa.py`) — la misma función que ahora usa
`scripts/resetear_modo_humano.py`, para que el script y el botón dejen la
conversación en el mismo estado.

Lo que **no** hace, y está cubierto por tests:

- No manda nada por WhatsApp. Del otro lado no pasa nada: para el usuario,
  reactivar es invisible.
- No reprocesa los mensajes que llegaron durante la pausa. El bot contesta
  recién con el próximo mensaje entrante, con las reglas de siempre (límite
  por hora, historial, todo igual).
- No borra ni toca mensajes.

Se limpia también el resumen del escalamiento, a propósito: es el detalle de
*esa* pausa, y dejarlo haría que el panel siguiera mostrando el motivo de algo
que ya no está pasando.

## 6. Historial largo y refresco

- El historial se pide **por páginas** (50 mensajes). Al abrir una
  conversación se ve la última página; "Cargar mensajes anteriores" pide la
  anterior con `antes_de=<id>`, y el panel compensa el alto que aparece arriba
  para que quien estaba leyendo siga viendo el mismo mensaje.
- El **refresco es cada 8 segundos** y pide solo lo que llegó después del
  último mensaje conocido (`desde=<id>`), más el estado de la conversación. No
  se repinta el historial entero: si lo hiciera, el scroll de quien está
  leyendo saltaría cada ocho segundos.
- Si quien mira **no** está abajo de todo, los mensajes nuevos se agregan sin
  mover el scroll. Si estaba abajo, baja solo.
- Con la pestaña en segundo plano no se pide nada.

## 7. Seguridad del contenido

Los mensajes se pintan **siempre** con `textContent`, nunca con `innerHTML`:
lo que escribe alguien por WhatsApp es texto, no marcado. Hay un test que lee
`panel.js` y falla si aparece `.innerHTML`, `.outerHTML` o
`insertAdjacentHTML(`.

## 8. Identidad visual

Colores de la marca real, no inventados: el cian `#00DFF5` y el verde
`#7EEE9A` son los del degradé del logo oficial (`app/crm/estaticos/logo-ene.png`,
muestreados del PNG), y el naranja `#E1522C` es el acento cálido de ENE que ya
usa el sitio del IA LAB. El logo es el PNG oficial con transparencia, copiado
sin modificar.

Tipografía del sistema, sin fuente externa: el panel tiene que abrir aunque el
servidor no tenga salida a internet.

En celular se ve **una vista por vez**: la lista, o el historial con un botón
"← Conversaciones" para volver.

## 9. Fuera de alcance (por ahora)

- Responder desde el panel.
- Buscar o filtrar por texto.
- Notificar a alguien cuando el bot escala (sigue abierto, ver PENDIENTES.md
  sección 1.b: el panel es dónde leerlo, no el aviso de que pasó).
- **Roles y permisos.** Hay cuentas individuales (cada persona entra con la
  suya), pero todas ven y pueden lo mismo: no hay perfiles de solo lectura ni
  administración desde el panel.
- **Gestionar usuarios desde el panel**: se hace con
  `scripts/crm_usuario.py`, desde la consola del servidor.
- **Recuperar la contraseña por mail.** La cambia quien administra, con
  `cambiar-password`. Un flujo de recuperación pide casilla saliente y trae
  su propia superficie de ataque, para cinco personas que se ven todos los
  días.
- **Segundo factor.** Vale la pena el día que el panel salga de la oficina;
  hoy lo que lo protege es https, el límite de intentos y que las cuentas las
  cree una persona a mano.
- Cualquier cosa que escriba en la conversación o en el contacto.

## 10. Lo que falta validar

El login se probó en dos niveles:

- **Con la suite de tests** (SQLite, sin red): alta de la primera cuenta,
  contraseña correcta e incorrecta, hash Argon2id y ausencia de texto plano,
  límite de intentos, no-enumeración, sesión manipulada / vencida / cerrada /
  invalidada al cambiar la contraseña o desactivar la cuenta, CSRF en
  reactivar y salir, ningún endpoint privado sin sesión, y la independencia
  del webhook de Meta.
- **Contra un Postgres real**, en un contenedor de prueba aislado (no la base
  del bot): creación de las tablas, alta desde el comando de consola, login
  correcto e incorrecto, bloqueo por intentos, lectura y reactivación de una
  conversación sin mandar nada por WhatsApp, logout con y sin token CSRF, y
  el arranque muriendo con código 3 ante las tablas del login anterior.

Lo que **no** se probó todavía: el panel servido por https con un dominio
real, y las pantallas abiertas a mano en un navegador de escritorio y de
celular. El checklist para ponerlo en producción está en PENDIENTES.md,
sección 1.c.
