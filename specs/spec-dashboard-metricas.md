# Spec: dashboard de costos y actividad en el CRM

Entrega nueva sobre el CRM existente (`specs/spec-crm-conversaciones.md`). Agrega
una vista de métricas dentro del mismo panel — no un servicio aparte, no una
base aparte.

## Alcance mínimo

Con un rango de fechas (`desde`/`hasta`, por defecto los últimos 7 días,
interpretados en `TIMEZONE`):

- Conversaciones totales (con actividad en el rango) y nuevas (creadas en el rango).
- Mensajes entrantes, enviados por el bot, enviados por humanos (por `creado_en` en el rango).
- Conversaciones activas y pausadas/escaladas — **estado actual, no del rango**:
  es un snapshot de `pausa_vigente()` en este instante (mismo criterio que ya
  usa la lista de conversaciones), no algo que tenga sentido recortar por
  fecha.
- Llamadas al modelo, errores y escaladas, en el rango.
- Tokens de entrada/salida por proveedor+modelo, cuando la API los informe.
- Costo estimado de IA con tarifas configurables (`TARIFAS_IA_JSON`), **nunca
  hardcodeadas**. Sin tarifa configurada para un proveedor+modelo, o sin
  tokens registrados: **N/D**, no cero.
- Tiempo medio de respuesta: de la llamada al modelo (`duracion_ms` en
  `LlamadaIA`), no del viaje completo hasta que Meta confirma el envío — eso
  es lo único que se puede medir con precisión sin instrumentar el envío
  aparte. Se rotula así en el panel para no prometer algo que no se mide.
- Tasa de escalamiento (escaladas / llamadas al modelo del rango).
- Mensajes por conversación (mensajes entrantes del rango / conversaciones
  con actividad en el rango).

Nunca se muestran teléfonos ni contenido de mensajes — todo lo que sale del
endpoint de métricas son números y (proveedor, modelo).

## Modelo de datos

Tabla nueva `llamadas_ia` (`LlamadaIA` en `app/models.py`, junto al resto del
dominio del bot — es lo que genera cada llamada a `generar_respuesta`, no algo
propio del CRM): una fila por invocación a `generar_respuesta` desde
`responder()` en `app/main.py`, éxito o fracaso.

Campos: `conversacion_id`, `proveedor`, `modelo`, `creado_en`, `duracion_ms`,
`resultado` (`ok` / `error_transitorio` / `error` / `vacio`), `escalo`
(bool), `tokens_entrada`, `tokens_salida` — los dos últimos nullable, NULL
cuando el proveedor no informó uso (pasa con `fijo`, y puede pasar con un
`openai_compat` que no devuelva `usage`).

**Simplificación deliberada**: no se separan tokens de caché de Claude
(`cache_creation_input_tokens`/`cache_read_input_tokens`) en columnas propias
— se sumtan al total de `tokens_entrada`. Separarlos daría un costo más
preciso (la caché es más barata) pero no es necesario para el alcance mínimo
y complica el modelo y las tarifas; queda como mejora futura si hace falta
afinar el costo.

Como es una tabla nueva, `create_all` (`app/db.py:init_db`) la crea sola en
cualquier base, incluida una que ya tiene `conversaciones`/`mensajes` — no
hace falta migración para que la app arranque. Igual se agrega
`scripts/migracion_metricas_ia.sql` (UP/DOWN, sin correr) seguro por
consistencia con el resto del repo, para que quede documentado qué generó la
entrega y cómo revertirlo sin depender de `create_all`.

## Tarifas configurables

`TARIFAS_IA_JSON`, una única variable de entorno con un JSON:

```json
{
  "claude:claude-haiku-4-5-20251001": {"entrada": 0.8, "salida": 4.0},
  "openai_compat:deepseek-chat": {"entrada": 0.14, "salida": 0.28}
}
```

Clave `"{proveedor}:{modelo}"`, valores en USD por millón de tokens. Parseo
tolerante en `app/config.py`: JSON inválido o ausente da `{}` (todo N/D), no
tira abajo el arranque — no es un secreto ni algo que deba cortar el boot
como sí hace `validar_config()` con lo que es indispensable.

## Endpoint

`GET /crm/api/metricas?desde=YYYY-MM-DD&hasta=YYYY-MM-DD`, detrás de
`requiere_sesion` como el resto de la API del panel. Sin parámetros, los
últimos 7 días. Las fechas son días calendario en `TIMEZONE` (la misma que ya
usa el panel para mostrar horas), convertidos a límites UTC antes de
consultar — la base guarda todo en UTC.

## Vista

`/crm/metricas`, página nueva sobre la misma sesión y el mismo login que
`/crm` — reusa `requiere_sesion`, no agrega autenticación propia. Un link
cruzado en la cabecera de las dos páginas (`Conversaciones` ↔ `Métricas`).
Reusa las variables y clases de `crm.css` (chips, tarjetas, paleta) en vez de
una hoja de estilos aparte — coherencia con el panel existente, no un
sub-producto visual distinto. Responsive con el mismo breakpoint de 860px que
ya usa `crm.css`.

## Qué no cambia

El webhook (`POST /webhook`, `GET /webhook`) no se toca. `responder()` sigue
haciendo lo mismo que hacía; lo único nuevo es que además escribe una fila en
`llamadas_ia` con lo que ya sabía (proveedor, duración, resultado) más lo que
ahora le devuelven los proveedores (tokens). Ningún proveedor cambia su
contrato hacia el resto del código — `RespuestaGenerada` gana dos campos
opcionales (`tokens_entrada`, `tokens_salida`), con default `None`, así que
nada que ya construya un `RespuestaGenerada` sin nombrarlos se rompe.
