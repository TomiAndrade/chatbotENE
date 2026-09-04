# Spec — Pausa automática del bot por intervención humana

> Contexto: el número de ENE funciona en **modo coexistencia**. La secretaría sigue atendiendo desde la app de WhatsApp Business y el bot responde por API sobre el mismo número.
> Problema: si la secretaría está atendiendo una conversación, el bot no debe responder encima.
> Confirmado con Kapso: los mensajes enviados desde la app llegan al webhook y se pueden distinguir de los enviados por API.

---

## 1. Qué cambia

Hoy `modo_humano` se prende únicamente cuando el modelo llama a `escalar_a_humano`. Ahora también se prende cuando **la secretaría responde desde la app**.

No cambia la lógica de `modo_humano` en sí: mientras está prendido, el bot no responde. Cambia qué lo prende y cómo se apaga.

---

## 2. Evento nuevo a procesar

Suscribir el webhook al evento `whatsapp.message.sent`, además del de mensajes entrantes que ya se procesa.

Campos relevantes del payload:

| Campo | Valor | Significado |
|---|---|---|
| `message.kapso.direction` | `outbound` | Mensaje que sale del número hacia el contacto |
| `message.kapso.origin` | `business_app` | Lo mandó una persona desde la app |
| `message.kapso.origin` | `cloud_api` | Lo mandó el bot |
| `message.id` | `wamid.xxx` | Para deduplicación |
| `conversation.id` | `conv_xxx` | Identifica la conversación |

**Regla:** `direction == "outbound"` **y** `origin == "business_app"` → prender pausa.

Cualquier otra combinación se ignora.

---

## 3. Riesgo principal — que el bot se pause a sí mismo

Si el bot envía un mensaje y ese evento vuelve por el webhook como `outbound + cloud_api`, y la condición está mal escrita, el bot se pausa después de cada respuesta propia y no vuelve a contestar nunca.

Es un bug silencioso: no rompe nada, no tira excepción, simplemente el bot deja de funcionar.

**Requisito:** la condición debe verificar `origin == "business_app"` de forma explícita. No alcanza con chequear `direction == "outbound"`. No usar lógica del tipo "si no es del bot, entonces es humano": si el campo `origin` viene ausente o con un valor inesperado, **no** prender la pausa.

**Test obligatorio:** un evento `outbound + cloud_api` NO debe prender `modo_humano`.

---

## 4. Cambio de esquema

`modo_humano` hoy es un booleano. Para poder expirar la pausa hace falta guardar cuándo se prendió.

Agregar a `conversaciones`:

- `modo_humano_desde` — timestamp con zona horaria, nullable. Se setea al
  prender la pausa. Se limpia al apagarla.
- `motivo_pausa` — enum nullable (`escalamiento` / `intervencion_manual`).
  Necesario porque `modo_humano_desde` solo no alcanza para decidir si la
  pausa expira: solo `intervencion_manual` expira; `escalamiento` no, se
  desmarca a mano. Sin este campo, un `None` en `modo_humano_desde` tenía que
  significar dos cosas a la vez ("sin pausa" y "escalamiento"), y esa
  ambigüedad causó un bug real durante la implementación: un escalamiento
  del modelo podía heredar la fecha vieja de una pausa manual ya vencida y
  quedar mal calculado como expirado.

La base de desarrollo está vacía, así que no hay migración con datos.

**Zona horaria:** el resto del proyecto guarda en UTC y convierte solo para
mostrar (`app/mensajes.py`); `modo_humano_desde` sigue esa misma convención,
no `America/Argentina/Buenos_Aires` como decía antes esta sección. La
aritmética de expiración da igual en cualquier zona porque son datetimes
aware — lo que importa, y sigue en pie, es **no usar `datetime.now()`
naive**.

---

## 5. Cuándo se apaga la pausa

Tres formas:

1. **Por tiempo.** Pasada la ventana de pausa sin nueva intervención humana, el bot vuelve a responder.
2. **A mano**, con el script existente (`resetear_modo_humano.py`).
3. Cada mensaje nuevo de la secretaría **reinicia** la ventana, no la extiende sumando.

**Duración de la ventana:** configurable por `.env` (`PAUSA_HUMANA_MINUTOS`).

Kapso sugiere 30 minutos. Se propone **120 minutos** como valor inicial, por este caso concreto: si la secretaría contesta a las 17:50 y la persona responde a las 18:20, con 30 minutos el bot ya se despertó y escribe encima de una conversación humana en curso. El costo de una pausa larga de más es que el bot tarda en retomar; el costo de una corta de más es que pisa a una persona. El segundo es peor.

Valor a validar con uso real. Que sea configurable es parte del requisito.

---

## 6. Deduplicación

Kapso avisa que los webhooks pueden reintentarse.

La deduplicación por `wa_message_id` ya existe para mensajes entrantes. Hay que aplicarla también a los salientes. Verificar si la restricción de unicidad actual permite guardar ambos tipos sin colisión.

Un reintento del mismo evento no debe reiniciar la ventana de pausa.

---

## 7. Concurrencia

Este cambio toca `modo_humano`, que ya tuvo un TOCTOU detectado en la revisión de Opus.

La relectura de `modo_humano` desde la base antes de cada envío **ya está implementada** y cubre el caso principal: si la secretaría contesta mientras el modelo está generando, el bot relee antes de enviar y descarta su respuesta.

Verificar que esa relectura contemple también la expiración por tiempo, no solo el booleano.

---

## 8. Interacción con el escalamiento existente

`escalar_a_humano` y la pausa por intervención manual prenden el mismo `modo_humano`, pero por motivos distintos:

- **Escalamiento del modelo:** el bot decidió que hace falta una persona. La persona todavía no intervino.
- **Intervención manual:** una persona ya está respondiendo.

Por ahora se tratan igual. La única diferencia es que la intervención manual **sí expira por tiempo** y el escalamiento no (hoy se desmarca a mano).

Si esto se vuelve confuso más adelante, se puede agregar un campo `motivo_pausa`. No es necesario ahora.

---

## 9. Criterios de aceptación

Validar desde el celular, con el número real en coexistencia.

1. Un usuario escribe, el bot responde normalmente. `modo_humano` sigue apagado.
2. La secretaría responde desde la app. El usuario escribe de nuevo. **El bot no responde.**
3. Pasada la ventana de pausa sin intervención, el usuario escribe. **El bot responde.**
4. La secretaría manda dos mensajes seguidos. La ventana se reinicia con el segundo.
5. El bot responde varias veces seguidas sin intervención humana. **El bot no se pausa a sí mismo.** (Criterio crítico, ver sección 3.)
6. Un webhook duplicado del mismo mensaje de la secretaría no altera la ventana.

---

## 10. Tests

Además de los casos de arriba:

- Evento `outbound + business_app` → prende pausa
- Evento `outbound + cloud_api` → NO prende pausa
- Evento con `origin` ausente o desconocido → NO prende pausa
- Evento entrante normal → no toca la pausa
- Ventana expirada → el bot responde
- Ventana vigente → el bot no responde
- Reintento del mismo `message.id` → no reinicia la ventana

**Sobre los tests de pausa:** vale la advertencia que ya salió en la etapa 2 con `mensaje_escalamiento()`. Un test que solo cuenta mensajes puede quedar verde con la funcionalidad rota. Antes de dar los tests por buenos, sabotear deliberadamente la condición de pausa y verificar que fallen.

---

## 11. Fuera de alcance

- Distinguir el motivo de la pausa en el resumen de escalamiento
- Notificar a la secretaría que el bot se pausó
- Devolver la conversación al bot desde la app
- Migración a BSUID (va en su propio spec; el `conversation.id` de este payload conviene tenerlo en cuenta al hacerla)
