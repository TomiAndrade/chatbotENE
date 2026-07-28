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

Mantener la selección por `PROVEEDOR_IA`. Implementaciones: `fijo` (la actual, se conserva para tests), `claude`, `gemini`.

---

## Proveedores

El proyecto debe poder correr con Gemini durante el desarrollo (free tier, sin tarjeta) y con Claude en producción. Ambas implementaciones detrás de la misma firma.

Configuración por variables de entorno:

```
PROVEEDOR_IA=gemini
MODELO=gemini-2.0-flash
ANTHROPIC_API_KEY=
GEMINI_API_KEY=
```

El modelo se define por env var, no hardcodeado. En producción se prevé usar un modelo chico y rápido (Haiku), no el más grande.

**Importante:** las dos APIs tienen formatos distintos de tool calling. La traducción entre el formato propio de cada proveedor y `RespuestaGenerada` vive dentro de cada implementación, no afuera.

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

Timeout de la llamada al modelo: **20 segundos**. Si tarda más, en WhatsApp ya se percibe como caído.

No reintentar más de una vez. Un reintento largo empeora la experiencia más de lo que la salva.

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
PROVEEDOR_IA=gemini
MODELO=gemini-2.0-flash
ANTHROPIC_API_KEY=
GEMINI_API_KEY=
TIMEZONE=America/Argentina/Buenos_Aires
HISTORIAL_MAX_MENSAJES=20
HISTORIAL_DIAS_VALIDEZ=7
LIMITE_MENSAJES_HORA=30
```

Agregarlas al `.env.example` con valores vacíos donde sean claves.

**Recordatorio de la etapa 1:** ninguna clave hardcodeada, `.env` en `.gitignore`.

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
