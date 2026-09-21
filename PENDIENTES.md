# Pendientes — Bot WhatsApp ENE IA LAB

Todo lo que queda por hacer, al 31/07/2026, con la etapa 1 cerrada y la etapa 2
implementada y con tests.

Lo importante primero: **del `spec-etapa2.md` no quedó nada sin implementar en
código**. Los 62 tests pasan. Lo que falta para poder cerrar la etapa 2 como se
cerró la 1 es la validación contra los servicios reales (punto 1 de acá abajo).

Ordenado por lo que conviene hacer antes, no por importancia absoluta.

---

## 1. Validar la etapa 2 contra servicios reales — bloqueante

La suite de `tests/` mockea Kapso y mockea el proveedor de IA. Eso confirma que
el wiring interno está bien (historial, escalamiento, límite, manejo de
errores), pero **todavía no se mandó un solo mensaje real por `openai_compat`
ni por Claude**, ni se hizo una vuelta completa por WhatsApp con IA de verdad.

De los 8 criterios de aceptación del `spec-etapa2.md`, sólo el 8 ("los tests
pasan") está verificado. Los otros siete hay que probarlos a mano, celular →
sandbox de Kapso → ngrok → servidor, igual que en la prueba end-to-end de la
etapa 1:

1. Se escribe "hola" y el bot se presenta como asistente de ENE, **no** del
   IA LAB.
2. Se pregunta el precio de la membresía individual y responde $85.000, corto y
   sin markdown.
3. Se pregunta cuánto sale la sala de reuniones y **responde** (USD 100 la
   jornada, hasta 16 personas, + IVA) **sin escalar**. Después se pide
   reservarla para una fecha y ahí sí **escala**: `modo_humano` en `true`,
   resumen guardado, y llega el aviso que corresponde al horario.
4. A partir de ahí el bot **no responde más** en esa conversación.
5. Se pregunta una receta de cocina y redirige **sin** escalar.
6. Se pregunta algo relacionado pero ausente del knowledge base y **escala** en
   vez de rechazarlo como fuera de tema. Elegir el ejemplo contra el KB del
   día: creció mucho y varios huecos viejos ya no lo son. Sirven el bicicletero
   o la acústica de la sala de podcast.
7. Se pregunta cuánto sale un seat **por día** —el único CONSULTAR de la
   sección 11— y **no inventa** un número, teniendo el precio del seat mensual
   y el de la oficina por día al lado en la misma tabla.
8. Se pregunta qué actividades hay este mes y **no escala**: manda a la web del
   IA LAB o al Instagram de ENE, sin inventar fechas.

El punto 6 es el que más suele fallar. Si el bot lo rechaza como fuera de tema,
el problema está en el prompt, no en el código. El 7 es el nuevo candidato:
tener precios cerca del hueco invita a interpolar mucho más que no tener
ninguno.

Estos criterios se revisaron en agosto de 2026, cuando la ronda de datos de ENE
llenó la sección 11 del knowledge base. Los viejos 3 y 7 se aprobaban con el
comportamiento equivocado: el 3 pedía escalar ante cualquier consulta de
alquiler, y el 7 daba por buena la respuesta si el bot **no** decía el precio de
una oficina, que ahora sabe.

Para correr la prueba hace falta:

- Un proveedor con IA de verdad en `.env`. Hay dos caminos:
  - `PROVEEDOR_IA=openai_compat` con `BASE_URL`, `MODELO` y
    `OPENAI_COMPAT_API_KEY` (los valores de `.env.example` apuntan a
    OpenRouter con el free de Nemotron — ver la sección 2 sobre su tasa de
    fallos).
  - `PROVEEDOR_IA=claude` con `ANTHROPIC_API_KEY` y `MODELO` (Haiku).

  **Ojo:** `.env.example` trae `PROVEEDOR_IA=fijo`, que es el valor correcto
  para desarrollar y para los tests, pero con `fijo` el bot contesta el texto
  de la etapa 1 y ninguno de los 7 puntos se cumple. Ya no existe un proveedor
  `gemini`: se reemplazó por `openai_compat`, que cubre cualquier endpoint con
  formato de la API de OpenAI.
- `uvicorn app.main:app --reload --port 8000` y `ngrok http 8000` (puerto 8000,
  no 3000).
- La URL de ngrok configurada como webhook en el sandbox de Kapso.

No requiere cambios de código.

---

## 1.b. Avisarle al equipo cuando el bot escala — bloqueante de producción

**Hoy nadie se entera de un escalamiento.** El bot le dice al usuario *"tu
consulta pasó a una persona del equipo, en breve te responden por acá"*, prende
`modo_humano` y escribe un `WARNING` en el log. Eso es todo. Si nadie está
mirando el log o el panel de Kapso, la conversación queda muerta con una
promesa hecha. El bot **no puede salir a producción así**: es peor que no tener
bot, porque el usuario se queda esperando.

**Decidido: se notifica por mail, no por WhatsApp.** Mandarle un WhatsApp al
equipo parece lo natural porque la plomería de envío ya está hecha, pero un
mensaje iniciado por el negocio fuera de la ventana de 24 horas necesita
plantilla aprobada por Meta y **se cobra por conversación**: sería pagar por
cada escalamiento, más el trámite de aprobación de la plantilla. El mail no
cuesta nada ni tiene plantillas que aprobar. Que quede escrito para no volver a
discutirlo.

Lo que hace falta:

- Un módulo de mail con las credenciales SMTP en `.env` (host, usuario,
  password y casilla destino), fuera del repo como el resto de las claves.
- El mail se manda **después** del commit de `modo_humano`, nunca antes, y su
  fallo **no puede romper el escalamiento** — mismo orden y mismo criterio que
  el aviso al usuario (ver "El escalamiento no es atómico y el orden importa"
  en `CLAUDE.md`). Si el SMTP falla, se loguea diciendo qué se perdió.
- Contenido mínimo del mail: el número del usuario, el resumen que dejó el
  modelo, y la hora. Quien atiende tiene que poder ir al panel de Kapso y
  encontrar la conversación sin leerla entera.
- **No se da por terminado con tests.** Es una integración externa: hasta que no
  llegue un mail de verdad a la casilla, está mockeado nomás.

Esto agranda el alcance: `spec-etapa2.md` lista "notificaciones al equipo cuando
se escala" como explícitamente fuera de alcance. Se decidió sumarlo en agosto de
2026 al ver que sin esto el escalamiento no existe en la práctica.

Sigue abierto **quién** es esa persona y con qué casilla. El horario ya no:
es 8–18 de lunes a viernes, unificado con el del edificio (`app/mensajes.py`).

**Actualización (septiembre 2026, CRM):** con el panel de `/crm`
(spec-crm-conversaciones.md) ya existe **dónde** leer una conversación
escalada y devolverla al bot — eso antes no existía y era la mitad del
problema. La otra mitad sigue igual: **nadie se entera de que pasó**. El
panel hay que abrirlo; no avisa. Así que esto sigue siendo bloqueante para
prender `ESCALAMIENTO_HABILITADO`, y el aviso por mail sigue pendiente tal
como está descrito arriba. Lo que cambió es que ya no hace falta resolver
"¿y después dónde lo leen?" en el mismo paso.

---

## 1.c. Poner el CRM en producción — bloqueante del panel

El login del panel es **propio**: usuario y contraseña de una cuenta de
`crm_usuarios`, con Argon2id (ver spec-crm-conversaciones.md, sección 3).
Reemplazó a Auth0 en septiembre de 2026, antes de que la aplicación de Auth0
llegara a existir, así que no quedó ninguna integración externa que validar:
lo que falta es operativo.

Lo que se probó y cómo (para no volver a discutirlo):

- **Contra la suite de tests**: alta de la primera cuenta, contraseña
  correcta e incorrecta, hash Argon2id sin texto plano, límite de intentos,
  no-enumeración de cuentas, sesión manipulada / vencida / cerrada /
  invalidada al cambiar la contraseña o desactivar la cuenta, CSRF en
  reactivar y salir, ningún endpoint privado sin sesión, y que el webhook de
  Meta no dependa del panel.
- **Contra un Postgres real** (contenedor de prueba aislado, no la base del
  bot): creación de las tablas, alta de la primera cuenta desde el comando de
  consola, login correcto e incorrecto, bloqueo por intentos, lectura y
  reactivación de una conversación sin mandar nada por WhatsApp, logout con y
  sin token CSRF, y que el arranque muera con código 3 si quedaron tablas del
  login anterior.

Lo que falta, en orden:

1. **Servir el panel por https** y poner `CRM_BASE_URL` con ese dominio. Con
   http fuera de localhost el arranque corta: la cookie de sesión no puede
   salir con el flag `Secure` y la contraseña viajaría en claro.
2. **`DEBUG=false`** (es lo mismo que ya pide el checklist de deploy del
   bot).
3. **Crear la primera cuenta** en el servidor de producción:
   `python scripts/crm_usuario.py crear <usuario>`. El comando pide la
   contraseña sin mostrarla; no se pasa por chat ni por WhatsApp.
4. **Respaldar la base** antes de prender el panel: el alta de cuentas
   escribe en la misma base que el bot.
5. **Probar el acceso a mano** desde un navegador: entrar, leer una
   conversación, reactivar el bot, salir, y confirmar que la cookie ya no
   sirve.
6. **Si la base ya tuvo el CRM con Auth0** (no es el caso de la base de
   producción, que nunca lo tuvo): borrar `crm_transacciones_oidc` y
   `crm_sesiones` antes de arrancar. El servidor no levanta si están y dice
   el comando exacto.

Lo que **no** hace falta y conviene no inventar: no hay registro público, no
hay recuperación de contraseña por mail (la cambia quien administra, con el
comando) y no hay roles — toda cuenta activa ve el panel entero.

---

## 1.d. Integrar la rama del CRM con `develop` y con BSUID

`feat/crm-conversaciones` salió de `73ce9a8`. Nada de esto está hecho todavía
y **no se mezcló ninguna rama**: es el mapa para cuando se haga.

### De `develop` (1 commit por delante)

`d75437e` — "el bot habla de ENE en primera persona, no como tercero", que
toca `prompts/system-prompt.md` y `prompts/knowledge-base.md`. **La rama del
CRM no toca `prompts/`**, así que entra sin conflicto.

### De `feat/whatsapp-bsuid` (sin commitear, en su propio worktree)

Las dos ramas tocan diez archivos en común. Ninguna se puede mergear encima
de la otra sin mirar; lo que sigue es qué hay que adaptar en cada uno.

**Ya resuelto de este lado**: `app/db.py` gana en las dos ramas un accesor al
engine con el mismo propósito. Acá se lo llamó **`obtener_engine()`**, igual
que en BSUID, justamente para que no queden dos funciones haciendo lo mismo.

Conflictos de texto, todos chicos:

| Archivo | Qué choca |
|---|---|
| `app/config.py` | Las dos suman campos al dataclass y a `_cargar_config()`. Se quedan los dos: `preferir_bsuid_al_enviar` y `crm_habilitado`/`crm_base_url`. |
| `app/db.py` | `init_db()` suma imports en las dos (`app.crm.modelos` acá, los modelos de BSUID allá) y las dos agregan `obtener_engine()` — dejar **una sola**. |
| `app/main.py` | El bloque de imports, y `al_iniciar()`: acá suma `_revisar_crm()` después de `init_db()`. BSUID reescribe el cuerpo del webhook, que el CRM no toca. |
| `app/models.py` | El CRM solo cambió una referencia en un docstring. |
| `scripts/resetear_modo_humano.py` | Las dos lo reescriben. El CRM lo dejó llamando a `reactivar_bot`; BSUID le cambia cómo busca la conversación. |
| `.env.example`, `README.md`, `CLAUDE.md`, `PENDIENTES.md` | Secciones agregadas en las dos. Se concatenan. |

Tests a adaptar:

| Archivo | Qué hay que hacer |
|---|---|
| `tests/conftest.py` | Las dos suman variables de entorno y fixtures. **Ojo con `meta_enviados`**: BSUID lo cambia de lista de textos a lista de `(destino, texto)`. Los tests del CRM solo cuentan cuántos elementos tiene (para afirmar que el panel **no** manda nada), así que siguen andando, pero conviene leerlos de nuevo. |
| `tests/helpers.py` | BSUID cambia las firmas de `payload_meta_texto` y compañía. `tests/test_crm_acceso.py` las llama posicionalmente (`payload_meta_texto(id, telefono, texto)`) en dos tests; revisar que el orden siga siendo ese. El `crear_conversacion` que agregó el CRM no choca con nada. |
| `tests/test_validacion_config.py` | `_config_valida()` y `_config_con_crm()` tienen que listar los campos de las dos ramas, o `Config` no se construye. |
| `tests/test_pausa_humana.py` | El CRM solo renombró referencias en docstrings. |

Lo que no es un conflicto de texto pero hay que decidir:

- **Qué muestra el panel como identificador.** Hoy muestra
  `identificador_externo` verbatim (spec-crm-conversaciones.md, sección 1).
  Con BSUID, una conversación puede tener BSUID, teléfono, nombre visible y
  username (`IdentificadorConversacion`, y las columnas nuevas de
  `Conversacion`). El panel es justamente donde eso se mira, así que
  `app/crm/servicio.py:_resumen` va a querer mostrar el nombre visible con el
  identificador debajo, en vez de un número pelado. **No está hecho**: la
  rama del CRM no sabe que BSUID existe.
- **La limpieza de tablas de `tests/conftest.py`** borra `crm_*` de un lado e
  `identificadores` del otro, y los identificadores tienen FK a
  conversaciones: el orden de los `delete()` importa.

---

## 2. Hallazgos del spike de tool calling contra OpenRouter (31/07/2026)

Antes de sumar un proveedor nuevo se corrió un **spike descartable, fuera de
este repo** (`~/spike`, no versionado), que pega a la API de OpenRouter con
`nvidia/nemotron-3-ultra-550b-a55b:free` en formato OpenAI, declarando la
herramienta `escalar_a_humano`. Se hicieron tres tandas: `tool_choice` forzado,
`tool_choice: "auto"` con un system prompt corto de prueba, y `auto` con el
**system prompt real del bot** (armado con el propio `app/prompt.py`, no una
copia).

**Ya está todo implementado en `app/proveedor_openai_compat.py`.** Esta sección
queda como registro de dónde salió cada decisión: lo que sigue describe lo que
el spike encontró y cómo se resolvió, no trabajo pendiente.

### Lo que quedó validado

- **El contrato de tool calling en formato OpenAI funciona.** Llega
  `tool_calls`, `function.name` es `escalar_a_humano` y `function.arguments`
  trae la clave `resumen`.
- **`function.arguments` viene como string con JSON adentro, no como objeto** —
  requiere un `json.loads` del lado del proveedor. Es distinto de Claude, donde
  el SDK ya entrega un dict (`bloque.input`).
- **Con el prompt real, el escalamiento se dispara** en el criterio de
  aceptación 3 (alquiler de sala). La respuesta vino con `content: None` —o sea
  que respetó *"No anuncies que vas a escalar ni escribas un mensaje de
  despedida"*— y el resumen reflejaba la regla 10 del prompt: *"El polo ofrece
  este servicio pero el bot no tiene esos datos"*.

  **Ojo al releer esto:** el spike corrió contra el KB de julio, cuando el bot
  no tenía ningún precio de espacios y escalar era la única respuesta posible.
  Con el KB de agosto ese mismo mensaje **no debe escalar**: tiene que
  responder el precio. El criterio 3 se reescribió en `spec-etapa2.md` y ahora
  distingue preguntar el precio (responde) de pedir una reserva (escala). Lo
  que este hallazgo sigue validando es el contrato de tool calling, no el
  comportamiento esperado ante esa pregunta.

### Cambios que hubo que hacer para sumar el proveedor OpenAI-compatible

- **OpenRouter devuelve errores adentro de un HTTP 200.** Un fallo llegó como
  `200 OK` con body `{"error":{"message":"Internal error...","code":502}}`.
  Era un agujero real en el manejo de errores: `con_un_reintento`
  (`app/respuesta.py`) reintenta ante **cualquier excepción**, y un 200 no
  levanta ninguna en un cliente HTTP — así que no había reintento, el parseo no
  encontraba `choices` y terminaba como respuesta vacía. El chequeo tenía que
  ser sobre la **clave `error` del body**, no sobre el código HTTP.
  > Ojo con el matiz al leer el código: el reintento por status (429 y 5xx) es
  > `_conviene_reintentar` en `app/kapso.py` y aplica a los **envíos a
  > Kapso**, no a la llamada al modelo. Son dos mecanismos distintos.

  **Resuelto:** `ProveedorOpenAICompat._llamar` chequea la clave `error` del
  body y levanta `ErrorTransitorioProveedor`. La sección "Manejo de errores"
  de `spec-etapa2.md` ya lo refleja, y está cubierto por
  `tests/test_proveedor_openai_compat.py::test_error_adentro_de_un_200_levanta_error_transitorio`.
- **El `resumen` puede venir mal formado.** Si el tool call llegó pero
  `arguments` no parsea como JSON, o parsea pero no trae `resumen`, **igual hay
  que escalar** (`escalar=True`, `resumen=None`): un `JSONDecodeError` no puede
  tumbar el request. Que el resumen se pierda es peor para quien atienda, pero
  no perder el escalamiento es lo que importa.

  **Resuelto:** `_interpretar_respuesta` atrapa el `JSONDecodeError` y escala
  con `resumen=None`. Cubierto por
  `tests/test_proveedor_openai_compat.py::test_arguments_mal_formado_escala_igual_con_resumen_none`.

### Confiabilidad del free tier y sus consecuencias

- **El free de Nemotron vía OpenRouter es poco confiable: 5 de 12 requests
  fallaron** con `Upstream error from Nvidia: ResourceExhausted: Worker local
  total request limit reached (32/32)`. Es saturación transitoria del tier
  gratuito, **sin relación con el tamaño del payload** (se descartó bisecando:
  28k chars pasaba, 32k fallaba y 35k/37k volvían a pasar — el rango no cierra
  por tamaño, y el propio mensaje de error dice que es límite de workers).
- **Consecuencia práctica sobre la regla "error → escalar":** con esa tasa de
  fallos, cerca de una de cada dos conversaciones de prueba va a quedar trabada
  en `modo_humano` sin que haya pasado nada malo con el bot.
- **Distinguir el error transitorio del proveedor del resto, al menos en
  desarrollo.** Un error transitorio debe responder "problema técnico, probá de
  nuevo" y **no** prender `modo_humano`. En producción con un proveedor pago,
  escalar sigue siendo lo correcto — la regla original no estaba mal, le
  faltaba el caso de desarrollo.

  **Resuelto:** `ErrorTransitorioProveedor` (`app/respuesta.py`) más la rama
  por `DEBUG` en `main.responder`. Cubierto por
  `tests/test_error_transitorio.py`.
- **Hace falta un script de reset de `modo_humano` por teléfono.** No es un
  nice-to-have: los criterios de aceptación 5, 6 y 7 **no se pueden probar sin
  él**, porque el criterio 4 (el bot deja de responder tras escalar) corta la
  conversación. Con la tasa de fallos de arriba se va a usar seguido.

  **Resuelto:** `scripts/resetear_modo_humano.py <telefono>`.

### Costo del prompt y elección de proveedor

El system prompt real (`system-prompt.md` + `knowledge-base.md`) eran **37.517
caracteres = 10.371 tokens**, con 36 bloques `[PENDIENTE]`. **Veníamos
estimando ~3.000 tokens**, o sea más del triple. Eso reordena las opciones:

| Proveedor | Efecto |
|---|---|
| Groq | **Queda descartado.** Con un TPD de 100–200K son 10 a 20 llamadas por día. |
| DeepSeek | El bono de 5M tokens rinde **~470 llamadas, no ~1.600**. |
| OpenRouter | **No se ve afectado**: su límite es por request, no por token. |

Y siguió creciendo, como estaba previsto. Tras la ronda de agosto de 2026 (los
precios de espacios, las condiciones comerciales y todo lo de la sección 3) el
prompt está en **56.079 caracteres**: 18.562 más, un **+50%**. La medición de
tokens de arriba fue real y ésta no — aplicando el mismo ratio de aquella
(3,617 caracteres por token) da **~15.500 tokens**, y el bono de DeepSeek
bajaría de ~470 llamadas a **~320**. Hay que volver a medirlo de verdad contra
el proveedor que se termine usando, no arrastrar la regla de tres.

Completar los `[PENDIENTE]` que quedan lo agranda todavía más. Sigue siendo
prefijo cacheable, así que el crecimiento es barato **si el caché funciona** —
lo que refuerza la prioridad de medir la tasa real de cache hit, no darla por
sentada.

### Cómo testear esto (aprendizajes de método)

- **El escalamiento es no determinista.** Con el prompt corto, dos corridas
  idénticas dieron resultados distintos: una escaló y la otra no. Con el prompt
  real escalaron 5 de 5. **Implicación directa para validar la etapa 2**: no
  sacar conclusiones de una sola muestra en ningún criterio que dependa del
  juicio del modelo (3, 5, 6 y 7 de la sección 1).
- **Fallo silencioso a vigilar:** el modelo puede prometer la derivación en el
  texto ("te derivo al área correspondiente") **sin llamar a la herramienta**.
  El usuario lee que lo van a derivar, `modo_humano` nunca se prende y nadie se
  entera. La regla del system prompt que lo prohíbe funcionó en las corridas con
  prompt real (`content: None`), pero hay que reconfirmarlo contra el modelo que
  se use en producción.
- **`enable_thinking: False` no tiene efecto en Nemotron**: la respuesta trajo
  `reasoning_tokens: 210` igual. Si la latencia importa, ese parámetro no es la
  palanca.

### Alcance — qué NO valida este spike

Valida el contrato de tool calling y que el prompt real induce el escalamiento
en el criterio 3. **No valida el bot.** Sigue faltando todo lo de la sección 1:
reconfirmar contra el proveedor que se termine usando y la vuelta completa por
WhatsApp. El spike corrió fuera del repo, contra un script suelto; que ahora
exista `PROVEEDOR_IA=openai_compat` (que sí cubre Nemotron vía OpenRouter, y
cualquier otro endpoint con formato OpenAI) no reemplaza esa prueba: es código
distinto del que se spikeó.

---

## 3. Completar los bloques `[PENDIENTE]` del knowledge base — con el equipo del polo

`prompts/knowledge-base.md` tiene los huecos marcados a propósito: le dicen al
modelo qué no sabe, que es exactamente lo que necesita para escalar en vez de
inventar. Están bien así para desarrollar, pero **antes de producción hay que
completarlos con el equipo**, porque cada uno es una consulta que hoy termina en
un humano.

**Ronda de agosto 2026 — una parte importante ya se completó.** El equipo de ENE
respondió por mail y de ahí salieron: horario real (8–18, sin feriados), acceso
por FACE ID, la tabla completa de precios y equipamiento de los ocho espacios
(sección 11), las condiciones comerciales y de facturación (sección 12 —
sección nueva), domicilio postal, política de logos, el circuito para organizar
eventos y la cafetería. La lista de decisiones que se aplicó vivía en
`cambios-kb-y-prompt.md`, ya borrado; lo que sobrevive de ese documento está en
el KB: los pendientes en su checklist final y, al pie, la tabla de **datos que
quedan deliberadamente afuera** (estructura societaria, link del grupo de
WhatsApp, mails de comprobantes, representante legal, nombres de las
plataformas de firma y facturación). Esa tabla existe para que nadie los agregue
más adelante "completando huecos": son decisiones, no olvidos.

Un bloque sigue vacío:

- **Agenda de eventos del laboratorio** (sección 8) — no hay fuente. Existe un
  Google Calendar público; falta definir si el bot lo lee (integración, fuera de
  alcance por ahora) o si se carga a mano, y quién avisa cuando hay un evento
  nuevo. La otra mitad de la sección 8 —organizar un evento en ENE— quedó
  resuelta: el bot informa espacios y deriva a coordinacionenepctnqn@gmail.com.

Y hay huecos puntuales en el resto:

| Tema | Qué falta confirmar |
|---|---|
| Identidad | Cómo debe nombrarse el bot al presentarse ("ENE", "Polo Tecnológico Neuquén", otra forma) |
| Institucional | Misión o descripción de ENE como polo (hoy sólo está la del laboratorio); si hay otras iniciativas además del IA LAB; si la estructura societaria es información pública o la explica una persona |
| Acceso | Si piden DNI en recepción |
| Membresías | Si se puede dar de baja en cualquier momento o el compromiso es por el ciclo completo; si las 3 personas de la Corporativa son fijas o rotan; si al ingresar a mitad de ciclo se paga desde el mes que entra o hay ajuste |
| Pagos | Datos bancarios / CBU o alias (ver la nota de abajo) |
| Precios en pesos | **Quién actualiza** los `$85.000` / `$150.000` de las membresías. Se decidió que el bot los diga a secas, sin aclarar vigencia —es la respuesta más útil y la más natural para WhatsApp—, y el costo aceptado es que el día del aumento el bot siga dando el viejo con total seguridad hasta que alguien edite el KB. No puede detectarlo solo: no tiene reloj. Falta la persona a cargo. Los precios en dólares no tienen este problema (se facturan al dólar BNA del día). |
| Postulación | Plazo aproximado de respuesta tras enviar el formulario |
| Espacios | Valor del **seat por día** (figura como CONSULTAR); si la **sala de podcast** tiene tratamiento acústico o algo que la diferencie de la de reuniones —es la pregunta natural de quien compara USD 140 por 6 personas contra USD 100 por 16—; equipamiento real de la **oficina privada por día** (el documento fuente parece tener un copy-paste); costo del **domicilio postal** |
| Cowork | Si el horario es 8–18 como el resto del edificio (el KB lo unificó por decisión; el 9–17 anterior venía de la web y nadie lo confirmó para el cowork); si se reserva con anticipación; si las 2 veces por semana son días fijos o los elige el miembro; si el miembro de IA LAB accede a todos los servicios del cowork o sólo a escritorio y wifi |
| Actividades | Cuáles son las actividades abiertas al público y cómo enterarse |
| Cafetería | **Resuelto (septiembre 2026):** no hay teléfono que dar, la cafetería no toma pedidos a distancia. Quien está en el edificio baja a planta baja, pide ahí, y puede subir lo consumido a las oficinas. **The Coffee Store** y **Work Café** son el mismo local (Work Café es su sección privada), ya está en el KB. |
| Mails | `info@eneneuquen.com.ar` quedó **obsoleto** pero sigue publicado en la web de ENE. El bot ya no lo da (usa `recepcion.ene.pctnqn@gmail.com`), pero la gente lo va a seguir usando y esos mensajes no los lee nadie. Conviene que ENE lo baje de la web o lo redirija. `ialab@eneneuquen.com.ar` (septiembre 2026): se sacó del bot por no tener confirmado que esté activo — todo lo del laboratorio deriva ahora a `recepcion.ene.pctnqn@gmail.com`. Falta que ENE confirme si esa casilla del laboratorio funciona; si es así, se puede volver a separar. |
| Referentes | Si el bot puede entregar el mail del referente de una vertical. **Requiere decisión de ENE, no un dato**: el bot no puede verificar que alguien sea miembro, así que entregar mails de personas nombradas a terceros no verificados choca con la regla 4 y con la Ley 25.326. Las tres opciones planteadas están en la sección 14 del KB. Hasta que se resuelva, no se dan contactos de referentes. |
| Proyectos a pedido | Si se toman o no (el trabajo se organiza por verticales); hoy el bot escala esta consulta |

**Regla del proyecto que no cambia:** el bot **nunca** envía datos bancarios,
CBU ni alias, en ninguna etapa. Que el dato falte en el knowledge base no es el
motivo; aunque se complete, no se envía. Lo que hay que definir con el equipo es
quién los manda y cuándo, no si los manda el bot.

---

## 4. Que falte el secreto del webhook falle fuerte, no en silencio — resuelto

**Aplicado** (spec-validacion-config-arranque.md). El problema era el caso
silencioso: `DEBUG=true` con el secreto vacío dejaba el webhook abierto a
cualquiera que conozca la URL, y nada fallaba — el server levantaba bien, los
mensajes llegaban, el bot respondía, y lo único que avisaba era un `WARNING`
en el log que nadie mira.

`validar_config()` ahora exige `META_APP_SECRET` **sin condición**, en
`al_iniciar()`, sin importar el valor de `DEBUG`: vacío o ausente corta el
boot con exit code distinto de cero antes de aceptar un solo request. El
bypass por `DEBUG=true` de `verificar_firma_webhook` (`app/meta.py:121`)
quedó muerto en la práctica — no hay forma de arrancar con el secreto vacío
para llegar a ejercitarlo.

Lo que **no** cierra esto: `DEBUG=true` sigue cambiando otros
comportamientos, ver 4.b.

---

## 4.b. Huecos que quedaron de la validación de arranque

Salieron de la auditoría del branch `feat/validacion-config-arranque`, antes
de mergearlo. Ninguno es un bug de lo implementado: son casos que la spec no
cubrió y que siguen abiertos. Comparten la misma forma que el problema que
esa spec vino a resolver — **el server levanta perfecto y el síntoma es
silencioso**, que en esta arquitectura (sin bandeja de entrada, con Meta
llevándose su 200) significa que nadie se entera.

- **`PROVEEDOR_IA=fijo` pasa la validación, y `.env.example` lo trae seteado
  así.** La motivación de la spec era que, si faltaba la variable, se caía al
  proveedor `fijo` en silencio. Ahora hay que escribirla — pero el ejemplo que
  todo el mundo copia dice `fijo`, y el resultado es idéntico: un bot que
  arranca impecable y le contesta la misma respuesta fija a todos los que
  escriban. `fijo` es el proveedor de tests, no un modo de producción.
  Mínimo un `WARNING` ruidoso al arrancar con `fijo`; `PROVEEDORES_VALIDOS`
  está en `app/validacion_config.py:16`.
- **`DEBUG=true` no lo valida nada y cambia comportamiento de producción.**
  Es una elección legítima en desarrollo, así que no se puede prohibir sin
  más — pero en `app/main.py:359`, ante un error transitorio del proveedor de
  IA, con `DEBUG=true` se manda el mensaje técnico y **no se escala**: el
  usuario recibe un error y nadie se entera. Con `DEBUG=false` se escala, que
  es lo correcto. Queda como lo único del checklist de deploy que hay que
  revisar a mano.
- **`app/config.py` revienta en el import, antes de que `validar_config()`
  pueda hablar.** `ZoneInfo(...)` (línea 57) y los `int(...)` (58-62) se
  evalúan al construir el `Config`, o sea antes del startup. Un `TIMEZONE`
  mal escrito da un `ZoneInfoNotFoundError` pelado; `HISTORIAL_MAX_MENSAJES`
  con texto, un `ValueError`. El caso realista es
  **`LIMITE_MENSAJES_HORA=` definida pero vacía** en el panel de Render:
  `os.getenv` devuelve `""`, no `None`, así que el default nunca aplica y el
  proceso muere con `invalid literal for int() with base 10: ''`. Los tres
  salen fuera de `logger("bot")`, sin acumulación y sin la línea de resumen —
  justo lo que la spec quería evitar. Lo que corresponde es parsear esos
  valores dentro de `validar_config()`, o al menos tolerar el string vacío
  como ausente en `_cargar_config`.

Los tres verificados corriendo un `uvicorn` real, no leyendo el código.

---

## 5. Configuración de producción

Cuando se vaya a deployar (fuera de alcance por ahora, ver punto 9):

- `DEBUG=false` y `KAPSO_WEBHOOK_SECRET` con el valor del dashboard de Kapso.
- `PROVEEDOR_IA=claude` con `ANTHROPIC_API_KEY`.
- `MODELO` apuntando a un modelo chico y rápido (Haiku), **no** al más grande:
  las respuestas son cortas y el volumen es de WhatsApp.
- `DATABASE_URL` a Postgres. Migrar es cambiar esa variable y nada más: en
  Render se enlaza desde la base administrada y viene ya con el esquema
  `postgresql://`, que es el que espera SQLAlchemy y el que exige
  `validar_config()` — no se reescribe en ningún lado. Ver la sección 5.b
  por lo que **no** cubre ese cambio.

---

## 5.b. Postgres sin migraciones — prioritario post-lanzamiento

El esquema lo crea `init_db()` con `create_all`, y para el primer deploy
alcanza: la base arranca vacía y no hay datos que migrar. **Alembic quedó
deliberadamente fuera de la migración a Postgres** (spec-postgres.md, sección
5) para no agrandar el alcance con la fecha encima. No es un olvido, pero sí
un pendiente con fecha de vencimiento.

Dos cosas que hay que tener presentes hasta que exista Alembic:

- **`create_all` crea lo que falta, no modifica lo que existe.** Desde el
  momento en que haya conversaciones reales guardadas, cualquier cambio de
  esquema pasa a ser un `ALTER TABLE` a mano contra la base de producción:
  manual y riesgoso. Ahí es cuando conviene sumar Alembic, no antes.
- **Los enums son tipos nativos de Postgres.** `RolMensaje` y `motivo_pausa`
  se crean bien la primera vez, pero **agregar un valor nuevo a cualquiera de
  los dos va a requerir un `ALTER TYPE ... ADD VALUE` a mano**, porque
  `create_all` no toca un tipo que ya existe. Con SQLite esto no se nota: ahí
  los enums son texto con un CHECK y el problema no aparece. Hoy no molesta;
  molesta el día que se agregue un rol (un canal web con rol propio, por
  ejemplo) o un motivo de pausa nuevo, y el síntoma va a ser un
  `InvalidTextRepresentation` en runtime, no un error al arrancar.

**Primer caso real de lo de arriba: la entrega 1.2 (agrupamiento de
mensajes, specs/spec-agrupamiento-mensajes.md) sumó columnas a
`conversaciones` y `mensajes`.** `create_all` las crea solas en una base
nueva (tests, un deploy desde cero), pero no en una base Postgres existente.
Se resolvió con `scripts/migracion_agrupamiento.sql` (columnas nullable, sin
default, `ADD COLUMN IF NOT EXISTS` + rollback documentado) en vez de
Alembic, consistente con que Alembic sigue fuera de alcance — no se corrió
contra ninguna base real. Es el patrón a seguir para el próximo cambio de
esquema mientras no exista Alembic.

---

## 5.c. Pool de conexiones vs. threadpool de background tasks — antes del deploy

El engine de `app/db.py` no fija `pool_size` ni `max_overflow`: quedan en el
default de SQLAlchemy (`pool_size=5`, `max_overflow=10` → **15 conexiones por
proceso**). Con SQLite este número no importa —no hay pool de conexiones de
red—, así que el desbalance de abajo no se nota hasta Postgres.

- **El threadpool de background tasks es más grande que el pool de
  conexiones.** `procesar_mensaje_entrante` corre en el threadpool de AnyIO
  que usa Starlette para las `BackgroundTasks` (default **40 hilos**). En una
  ráfaga de mensajes, hasta 40 tareas concurrentes pelean por 15 conexiones:
  las que no consiguen una bloquean hasta 30s en el checkout (`pool_timeout`)
  y si se agota tiran `TimeoutError`.
- **Agrava esto que la sesión queda tomada durante toda la llamada al
  modelo, no sólo durante el acceso a la base.** `construir_historial`
  (`app/main.py:301`) abre una transacción con un SELECT y no se commitea
  hasta `enviar_y_guardar`; en el medio corre `generar_respuesta`, con un
  presupuesto de hasta 20s (`PRESUPUESTO_TOTAL_SEGUNDOS`, ver CLAUDE.md). Una
  conexión de Postgres queda retenida por un mensaje entero, no por una
  query.
- **La entrega 1.2 (agrupamiento de mensajes) alarga esto todavía más.**
  `agrupar_y_responder` mantiene la misma sesión abierta durante toda la
  espera de agrupamiento además de la llamada al modelo — hasta
  `AGRUPAR_ESPERA_MAXIMA_SEGUNDOS` (default 8s) de más por mensaje, sumados
  a los 20s de arriba. No se optimiza en esta entrega (ver
  specs/spec-agrupamiento-mensajes.md): es la misma clase de riesgo que ya
  documentaba este punto, no uno nuevo, y la solución de fondo sigue siendo
  la misma (medir con datos reales de Render antes de tocar `pool_size`).
- **Consecuencia si esto revienta:** el `TimeoutError` cae en el
  `except Exception` de `procesar_mensaje_entrante` (`app/main.py:416`), que
  loguea y sigue — el webhook ya devolvió 200 y Meta no reintenta, así que el
  mensaje se pierde en silencio salvo que alguien esté mirando el log de
  Render en ese momento.

No se tocan los números en esta migración porque hace falta un dato que
todavía no tenemos: cuántos workers va a levantar el start command de Render
(ver el punto siguiente) y qué límite de conexiones tiene el plan de Postgres
elegido. Subir `pool_size`/`max_overflow` a ciegas puede agotar ese límite si
hay más de un worker.

**El start command de Render tiene que ser de un solo worker** (p. ej.
`uvicorn app.main:app --host 0.0.0.0 --port $PORT`, sin `--workers`). El
engine de `app/db.py` es una variable de módulo: cada worker de uvicorn es un
proceso propio con su propio engine y su propio pool, así que un segundo
worker no comparte las 15 conexiones — las duplica. Con dos workers son 30
conexiones disputando el límite del plan de Postgres, no 15.

---

## 6. Guardar el `wa_message_id` de la respuesta del bot

Decidido explícitamente, no un olvido: hoy la respuesta del bot se guarda con
`wa_message_id = None` (`app/main.py:102`). Kapso devuelve el id en
`messages[0].id` al enviar y el campo del modelo existe justo para eso
(`app/models.py:46`), pero todavía no se persiste.

No rompe nada — la dedup es sólo sobre mensajes entrantes — pero sin el id no se
puede cruzar un mensaje de la base con lo que muestra el panel de Kapso, que es
lo primero que se quiere hacer cuando algo sale raro en producción.

---

## 7. Endurecer el chequeo de respuesta vacía — resuelto

**Aplicado.** `app/main.py` chequeaba `resultado.texto is None`; un texto de
sólo espacios pasaba ese chequeo y cala hasta el `if resultado.texto:` de más
abajo sin escalar. La condición ahora es
`if (not resultado.texto or not resultado.texto.strip()) and not resultado.escalar`,
que cierra el caso. Cubierto por
`tests/test_fallo_modelo.py::test_respuesta_de_solo_espacios_sin_escalar_tambien_se_trata_como_error`.

---

## 8. Deuda técnica menor

Anotada, no implementada. Ninguna rompe nada hoy; están acá para que no haya
que redescubrirlas.

- **Feriados.** Se tratan como día hábil, así que un 25 de mayo a las 11 el bot
  promete que responden "en breve". Está declarado como deuda conocida en el
  spec y anotado en `app/mensajes.py`.
- **El docstring de `SessionLocal` nombra módulos que no la importan.**
  `app/db.py:74` dice que hacen `from app.db import SessionLocal` "main.py,
  limite.py, historial.py, scripts, tests". `app/limite.py` y
  `app/historial.py` no la importan: reciben `db: Session` por parámetro. El
  razonamiento del docstring (por qué `SessionLocal` es una función y no el
  `sessionmaker` directo) sigue siendo correcto, solo está mal la lista.
- **`@app.on_event` está deprecado** en la versión de FastAPI del proyecto
  (`app/main.py`); la suite lo muestra como `DeprecationWarning` en cada
  corrida. Lo que corresponde es un handler de `lifespan`. Funciona igual, pero
  el warning va a seguir apareciendo y en algún momento se va a romper.
- **El camino público de `openai_compat` no está cubierto por tests.**
  `tests/test_proveedor_openai_compat.py` ataca `_interpretar_respuesta` y
  `_llamar` por separado, nunca `generar_respuesta`. Queda sin verificar que el
  mensaje `system` con el `SYSTEM_PROMPT` efectivamente se mande, que `tools`
  viaje en el payload, y que `_a_mensaje_openai` mapee bien los roles —
  incluido el prefijo `[Respuesta de una persona del equipo]`. Si alguien borra
  la línea del system prompt, la suite queda verde y el bot pierde todo el
  knowledge base. `tests/test_proveedor_claude.py` sí cubre el equivalente del
  lado de Claude y sirve de modelo para escribirlo.
- **`ProveedorClaude` nunca levanta `ErrorTransitorioProveedor`.** Sólo
  `openai_compat` clasifica 429/5xx como transitorios. Con
  `PROVEEDOR_IA=claude` y `DEBUG=true`, un `overloaded_error` de Anthropic
  escala a humano en vez de pedir que se reintente. En producción no cambia
  nada (ahí escalar es lo correcto, y es lo que hace), así que sólo molesta si
  se desarrolla contra Claude.
- **`con_un_reintento` reintenta fallos deterministas.** Reintenta ante
  **cualquier** excepción, incluido un 401 por API key inválida, que va a
  fallar igual las dos veces. `app/kapso.py` sí distingue con
  `_conviene_reintentar`; la capa del modelo no. Cumple el spec ("no reintentar
  más de una vez") pero gasta una request y duplica la latencia en fallos que
  no pueden salir distinto.
- **Se guarda en la base el texto sin truncar.** `KapsoClient` trunca a 4096
  caracteres antes de enviar (`app/kapso.py`), pero `enviar_y_guardar` persiste
  el `texto` completo. La base —y por lo tanto el historial que vuelve al
  modelo— contiene texto que el usuario nunca vio. Con el prompt pidiendo
  respuestas cortas es improbable, pero la divergencia está.
- **El aviso de límite depende de una igualdad exacta.**
  `main.procesar_mensaje_entrante` dispara el aviso sólo cuando
  `conteo_ultima_hora == config.limite_mensajes_hora + 1`. Si dos mensajes
  cruzan el límite concurrentemente y ambos cuentan lo mismo, nadie ve el `+1`
  exacto y **el aviso no se manda nunca**: silencio total sin explicación,
  contra lo que pide el spec ("Responder una vez avisando"). Con SQLite no se
  reproduce porque serializa las escrituras; **con Postgres en producción el
  escenario se abre**. La forma robusta sería un `>=` con un flag persistido de
  "ya avisé en esta ventana".
- **No hay tests de la constraint compuesta `(canal, identificador_externo)`
  de `Conversacion`.** Nada verifica que el mismo `identificador_externo` en
  dos canales distintos cree dos conversaciones separadas, ni que el mismo par
  `(canal, identificador_externo)` siga siendo único. Hoy no hay nada que
  romper porque solo existe el canal `whatsapp`, pero es lo primero a cubrir
  cuando se sume el canal web.
- **Con `ESCALAMIENTO_HABILITADO=false`, si el modelo igual llama a
  `escalar_a_humano` (no debería, al no estar declarada, pero no hay garantía
  de que no pase), `_interpretar_respuesta` descarta el tool call con
  `continue` y logueá un warning. Ya se revisó qué le vuelve a `main.py` en
  cada caso:**
  - **Sin texto además del tool call:** `resultado.texto` queda `None` y
    `resultado.escalar` en `False`, entra en la rama "el modelo devolvió una
    respuesta vacía sin escalar" de `main.py` (línea ~321) — manda
    `MENSAJE_ERROR_GENERICO` y corre el fallback `escalar_a_humano` interno.
    **Esto era falso y se arregló el 2026-09-15.** Ese fallback escalaba sin
    mirar `ESCALAMIENTO_HABILITADO`: prendía `modo_humano` —dejando al número
    sin respuesta del bot para siempre— y le prometía al usuario una persona
    que, sin bandeja de entrada, no existe. Pasó en producción con un mensaje
    real. Ahora `escalar_a_humano` (`app/main.py`) corta con un WARNING si el
    flag está en false, y los caminos de error mandan
    `MENSAJE_ERROR_SIN_ESCALAMIENTO`, que deriva al mail de recepción en vez
    de prometer un humano. Los 14 tests de escalamiento pedían la fixture
    `escalamiento_activo`: antes pasaban con el flag en false porque nadie lo
    miraba.
  - **Con texto además del tool call** (p. ej. el modelo escribe "dale, te
    paso con alguien" y en el mismo turno llama a la herramienta): ese texto
    **sí** se envía (`resultado.texto` no está vacío), pero como el tool call
    se descartó, `resultado.escalar` es `False` y no se llama a
    `escalar_a_humano`. El bot le promete al usuario un pase a una persona que
    nunca llega — mismo patrón que ya describe la sección 10 con la
    "derivación prometida sin llamar a la herramienta", pero ahora también
    puede pasar con el escalamiento. No se arregla filtrando el texto (sería
    peor: el modelo cortó ahí, no hay otra respuesta que mandar); requiere
    pensar el prompt o un chequeo explícito de "prometió pasar con alguien" —
    no trivial. Ver `_interpretar_respuesta` en `proveedor_claude.py` y
    `proveedor_openai_compat.py`.

---

## 9. Fuera de alcance hasta que se diga lo contrario

Esto **no** es deuda: son decisiones tomadas. Están acá para que no se
redescubran como si fueran olvidos.

- Deploy.
- Transcripción de audios. Sigue fuera de alcance, pero desde la entrega 1.1
  de `specs/roadmap-bot-crm.md` (ver specs/spec-adjuntos-no-soportados.md,
  2026-09-21) un audio (o cualquier adjunto no soportado) ya no llega al
  modelo: el `type` real del webhook dispara una respuesta fija pidiendo la
  consulta por escrito, sin escalar. El texto
  `[mensaje de tipo 'audio' no soportado en esta etapa]` se sigue guardando
  como `contenido` del mensaje entrante, solo para historial y diagnóstico —
  ya no es lo que decide la respuesta.
- Mensajes con botones, listas o plantillas.
- Devolver una conversación de humano a bot automáticamente — `modo_humano` se
  desmarca a mano en la base.
- Notificar al equipo cuando se escala. Hoy el escalamiento queda como un
  `WARNING` en el log y como `resumen_escalamiento` + `escalada_en` en la tabla
  `conversaciones`; nadie recibe un aviso activo.

---

## 10. El historial no le pasa al modelo el contenido de los mensajes humanos

Los mensajes que la secretaría manda desde la app (rol `humano`) se guardan
completos en la base, pero `app/historial.py:mapear_mensaje` reemplaza el
contenido por `MARCADOR_HUMANO` al armar el historial que va al modelo (ver
spec-pausa-por-intervencion-humana.md).

**Por qué:** el knowledge base documenta que los datos bancarios los manda el
equipo por ese mismo canal — la secretaría, desde la app. Si el contenido
pasara íntegro al historial, el CBU entraría al contexto del modelo, y la
regla "nunca datos bancarios" (que no cambia en ninguna etapa) pasaría de ser
una garantía dura — el dato no existe en el contexto — a depender de que el
modelo obedezca el prompt. Este modelo a veces no obedece: ver en la sección 2
el caso de la derivación prometida sin llamar a la herramienta.

**No sacar el filtro sin resolver antes qué pasa con el CBU.** Parece pérdida
de contexto innecesaria (el bot "no recuerda" lo que dijo la secretaría) y no
lo es.
