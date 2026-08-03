# Especificación — Bot WhatsApp ENE · Etapa 2

> Documento de contexto para Claude Code.
> **Prerrequisito:** etapa 1 cerrada y probada end-to-end.
> **Alcance de esta etapa:** conectar el modelo, armar el historial, e implementar el escalamiento a humano vía tool calling.

---

## Qué se construye

Reemplazar la respuesta fija por una generada por un LLM, con el system prompt y el knowledge base del proyecto, historial de conversación, y una herramienta que permite al modelo transferir la conversación a una persona.

Archivos de contexto que ya están en el repo:

- `prompts/system-prompt.md` — instrucciones del asistente, con el placeholder `{{KNOWLEDGE_BASE}}`
- `prompts/knowledge-base.md` — información del polo y del laboratorio

Ambos se cargan al arrancar el servidor y se cachean en memoria. No leer del disco en cada mensaje.

---

## Cambio de firma en la capa de respuesta

La firma actual devuelve `str`. Con tool calling eso ya no alcanza: hay que saber si el modelo escaló y con qué resumen.

Nueva firma:

```
generar_respuesta(historial: list[Mensaje], mensaje_nuevo: str) -> RespuestaGenerada
```

Donde `RespuestaGenerada` es un dataclass con:

| Campo | Tipo | Notas |
|---|---|---|
| `texto` | `str \| None` | La respuesta al usuario. `None` si solo escaló |
| `escalar` | `bool` | Si el modelo llamó a la herramienta |
| `resumen` | `str \| None` | Resumen de contexto para quien atienda |

Esta firma es la que va a durar. La implementación de adentro puede cambiar de proveedor sin afectar al resto.

Mantener la selección por `PROVEEDOR_IA`. Implementaciones: `fijo` (la actual, se conserva para tests), `openai_compat`, `claude`.

---

## Proveedores

El proyecto debe poder correr durante el desarrollo contra un endpoint gratuito o barato, y con Claude en producción. Ambas implementaciones detrás de la misma firma.

`openai_compat` no es un proveedor concreto: es **un solo proveedor parametrizado por `BASE_URL`**, que sirve para cualquier endpoint con formato de la API de OpenAI (chat completions + tool calling en ese formato). Eso incluye OpenRouter, DeepSeek, el endpoint gratuito de NVIDIA, o un modelo corriendo local — todos hablan el mismo formato de request/response. No atarlo a un servicio concreto ni en el nombre ni en el código; el servicio que se use hoy puede cambiar mañana sin tocar la implementación, solo la env var.

Configuración por variables de entorno:

```
PROVEEDOR_IA=openai_compat
BASE_URL=https://openrouter.ai/api/v1
MODELO=nvidia/nemotron-3-ultra-550b-a55b:free
ANTHROPIC_API_KEY=
OPENAI_COMPAT_API_KEY=
```

Esos son los valores de desarrollo actuales. `OPENAI_COMPAT_API_KEY` se llama así, genérico, a propósito: rotar de proveedor (OpenRouter → DeepSeek → lo que sea) es cambiar `BASE_URL`, `MODELO` y esta clave, sin renombrar nada en el código.

El modelo se define por env var, no hardcodeado. En producción se prevé usar Claude con un modelo chico y rápido (Haiku), no el más grande.

**Importante:** el formato OpenAI (`openai_compat`) y el formato nativo de Anthropic (`claude`) manejan tool calling distinto. La traducción entre el formato propio de cada proveedor y `RespuestaGenerada` vive dentro de cada implementación, no afuera.

Consultar la documentación oficial de cada API antes de implementar. No asumir la forma del payload.

---

## Armado del historial

- Traer los **últimos 20 mensajes** de la conversación, en orden cronológico.
- **Si pasaron más de 7 días** desde el último mensaje de la conversación, ignorar el historial y arrancar de cero. El asistente vuelve a presentarse.
- Mapear los roles al formato del proveedor:
  - `usuario` → rol de usuario
  - `bot` → rol de asistente
  - `humano` → rol de asistente, **pero con el texto prefijado** con algo tipo `[Respuesta de una persona del equipo]`

El último punto importa: los mensajes escritos por una persona del polo son contexto válido, pero el modelo no debe tomarlos como ejemplos de su propio estilo ni asumir que puede prometer lo mismo que prometió un humano.

---

## Prompt

Se arma una sola vez al arrancar: se lee `system-prompt.md` y se reemplaza `{{KNOWLEDGE_BASE}}` por el contenido de `knowledge-base.md`.

**Los bloques marcados `[PENDIENTE]` en el knowledge base se dejan tal cual.** Le indican al modelo qué no sabe, que es justamente lo que necesita para escalar en vez de inventar.

Si el proveedor soporta **caché de prompt**, activarlo para el bloque de sistema. Es fijo en todas las llamadas y reduce costo y latencia de forma significativa.

En `openai_compat` esto depende del proveedor detrás de `BASE_URL` (algunos cachean automático, otros no exponen el control) y queda opcional, sin bloquear la implementación. En `claude` va explícito, con `cache_control` sobre el bloque de sistema.

---

## Herramienta `escalar_a_humano`

Definición expuesta al modelo:

- **Nombre:** `escalar_a_humano`
- **Descripción:** transfiere la conversación a una persona del equipo. Usar cuando la consulta no puede resolverse con la información disponible, cuando el usuario pide hablar con alguien, o ante reclamos.
- **Parámetro `resumen`** (string, requerido): resumen breve de qué necesita la persona, para dar contexto a quien atienda.

Cuando el modelo la llama, el servidor debe:

1. Marcar `modo_humano = true` en la conversación.
2. Guardar el `resumen` — agregar un campo `resumen_escalamiento` (text, nullable) y `escalada_en` (datetime, nullable) a la tabla `conversaciones`.
3. Enviar al usuario un mensaje de aviso **generado por código, no por el modelo** (ver abajo).
4. Loguearlo de forma visible.

**No implementar el bucle completo de tool calling.** Si el modelo llama a la herramienta, se corta ahí: no se le devuelve el resultado para que siga generando. El mensaje al usuario lo escribe el código. Esto simplifica mucho y no se pierde nada.

Si el modelo devuelve texto **y además** llama a la herramienta, enviar ambos: primero su texto, después el aviso de escalamiento.

**Diferencia de formato al leer el argumento:** en formato OpenAI (`openai_compat`), `function.arguments` llega como **string con JSON adentro**, no como objeto — requiere `json.loads` del lado de la implementación. Si el tool call llegó pero el parseo falla (o parsea pero no trae `resumen`), **escalar igual** (`escalar=True`, `resumen=None`): un `JSONDecodeError` no puede tumbar el request. Perder el resumen es peor para quien atienda, pero perder el escalamiento en sí es peor todavía.

---

## Mensaje de escalamiento — lo escribe el código

El modelo no tiene reloj. El aviso depende del horario y por eso se genera en el servidor.

- **Zona horaria:** `America/Argentina/Buenos_Aires`. Configurable por env var (`TIMEZONE`). No usar la hora del sistema sin convertir: en producción el servidor corre en UTC.
- **Dentro del horario de atención** (lunes a viernes, 9 a 17): avisar que la consulta pasa al equipo y que responden a la brevedad.
- **Fuera del horario** (noches, fines de semana, feriados): avisar que la consulta quedó registrada y que responden en horario de atención, aclarando cuál es. **No prometer inmediatez.**

Los textos van en un módulo de mensajes, no dispersos en el código, para poder ajustarlos sin tocar lógica.

`[NOTA]` Los feriados no se contemplan en esta etapa; se tratan como días hábiles. Anotarlo como deuda conocida.

---

## Manejo de errores

Si la llamada al modelo falla (timeout, rate limit, error de la API):

1. **No dejar al usuario sin respuesta.** El silencio es la peor salida.
2. Enviar un mensaje de disculpa genérico e **inmediatamente escalar** a humano. Una conversación en manos de una persona es mejor que una conversación muerta.
3. Loguear el error completo.

Timeout de la llamada al modelo: **20 segundos en total, no por intento**. Si tarda más, en WhatsApp ya se percibe como caído — y lo que percibe el usuario es la espera completa, así que el presupuesto se reparte entre los intentos: con un reintento, 10 segundos cada uno.

No reintentar más de una vez. Un reintento largo empeora la experiencia más de lo que la salva.

**Un proveedor OpenAI-compatible puede devolver el error adentro de un HTTP 200.** No todos los fallos llegan como excepción: un `200 OK` con body `{"error": {...}}` es un fallo igual, y hay que detectarlo explícitamente chequeando la clave `error` en el body de la respuesta. Si no se detecta, el parseo no encuentra `choices` y el fallo termina tratado como respuesta vacía en vez de como error — que es el camino equivocado (ver el punto 7 de más abajo sobre respuesta vacía). El chequeo va sobre el contenido del body, no sobre el código HTTP.

**Distinguir el error transitorio del proveedor del resto de los fallos.** En desarrollo, contra un free tier, los errores transitorios (saturación del proveedor, 5xx, rate limit momentáneo) son frecuentes y no deberían tumbar la conversación en `modo_humano` cada vez: conviene responder algo como "hubo un problema técnico, probá de nuevo" y **no** escalar. En producción, con un proveedor pago, la regla no cambia: sigue escalando como está especificado arriba. Este comportamiento se controla por `DEBUG` (o una env var equivalente si conviene separarla) — no es una regla nueva que reemplaza la anterior, es una distinción que solo aplica mientras se desarrolla.

---

## Límite de uso por número

Protección básica contra abuso y contra costos inesperados: máximo **30 mensajes por hora por número de teléfono**.

Al superarlo, no llamar al modelo. Responder una vez avisando que se recibieron muchos mensajes y que continúe más tarde, y no volver a responder hasta que baje el límite.

Es una defensa simple contra un bucle accidental o alguien probando el bot a propósito.

---

## Detalles de envío

- WhatsApp corta los mensajes a 4096 caracteres. El prompt pide respuestas cortas, pero por las dudas truncar antes de enviar y loguear si pasa.
- Si el modelo devuelve texto vacío y no escaló, tratarlo como error (ver arriba).

---

## Variables de entorno nuevas

```
PROVEEDOR_IA=openai_compat
BASE_URL=https://openrouter.ai/api/v1
MODELO=nvidia/nemotron-3-ultra-550b-a55b:free
ANTHROPIC_API_KEY=
OPENAI_COMPAT_API_KEY=
TIMEZONE=America/Argentina/Buenos_Aires
HISTORIAL_MAX_MENSAJES=20
HISTORIAL_DIAS_VALIDEZ=7
LIMITE_MENSAJES_HORA=30
```

Esos son los valores de desarrollo actuales para `PROVEEDOR_IA`, `BASE_URL` y `MODELO`. Agregarlas al `.env.example`, con valores vacíos donde sean claves.

**Recordatorio de la etapa 1:** ninguna clave hardcodeada, `.env` en `.gitignore`.

---

## Entregable: script de reset de `modo_humano`

Un script (por número de teléfono) que desmarque `modo_humano` en la conversación correspondiente. Hoy ese desmarcado se hace a mano en la base; con un proveedor gratuito con tasa de error alta en desarrollo, va a hacer falta seguido.

Es **prerequisito** para poder correr los criterios de aceptación 5, 6 y 7: el criterio 4 (el bot deja de responder tras escalar) corta la conversación, y sin una forma de revertir `modo_humano` no se puede seguir probando los criterios siguientes sobre la misma conversación.

---

## Tests

Los cuatro scripts de la etapa 1 pasan a `tests/` con pytest si no se hizo todavía. Sumar:

1. **Armado del historial** — que traiga 20, en orden, y que el rol `humano` quede prefijado.
2. **Corte por antigüedad** — con el último mensaje a 8 días, el historial llega vacío.
3. **Escalamiento** — mockeando una respuesta del modelo que llama a la herramienta, verificar que se prende `modo_humano`, se guarda el resumen, y se manda el aviso.
4. **Mensaje según horario** — un martes a las 11 y un sábado a las 22 dan textos distintos. Con la hora inyectada, no con la real.
5. **Fallo del modelo** — que escale y avise en vez de quedarse mudo.
6. **Límite por número** — que el mensaje 31 en una hora no llame al modelo.

Los tests **no deben pegarle a la API real**. Mockear el proveedor.

---

## Criterio de aceptación

1. Se escribe "hola" y el bot se presenta como asistente de ENE.
2. Se pregunta el precio de la membresía individual y responde $85.000, corto y sin markdown.
3. Se pregunta por alquiler de una sala y **escala**: `modo_humano` queda en `true`, con resumen guardado, y llega el aviso correspondiente al horario.
4. A partir de ahí el bot **no responde más** en esa conversación.
5. Se pregunta una receta de cocina y redirige sin escalar.
6. Se pregunta algo relacionado pero ausente del knowledge base (por ejemplo, si hay bicicletero) y **escala** en vez de rechazar.
7. Se pregunta el precio de una oficina y **no inventa** un número.
8. Los tests pasan.

El punto 6 es el que más suele fallar. Si el bot lo rechaza como fuera de tema, el problema está en el prompt, no en el código.

---

## Fuera de alcance

- Devolver la conversación del humano al bot (se desmarca a mano en la base)
- Transcripción de audios
- Mensajes con botones, listas o plantillas
- Feriados
- Deploy
- Notificaciones al equipo cuando se escala
