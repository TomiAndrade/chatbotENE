# Spec — Migración de Kapso a Meta Cloud API

**Objetivo:** reemplazar Kapso por conexión directa a la Cloud API de Meta, sin cambiar el contrato interno del sistema.

**Principio rector:** `procesar_mensaje_entrante(identificador_externo, wa_message_id, contenido)` no cambia su firma ni su comportamiento. Todo lo que sigue existe para alimentar esa función con los mismos tres valores que hoy le pasa Kapso.

---

## 1. Archivo nuevo: `app/meta.py`

Copiar `app/kapso.py` y adaptar. La lógica de reintentos (`_conviene_reintentar`), el truncado a 4096 caracteres y el manejo de errores se conservan tal cual: están bien y aplican igual.

### Cambios en el envío

| | Kapso | Meta |
|---|---|---|
| Base URL | `https://api.kapso.ai/meta/whatsapp/v24.0` | `https://graph.facebook.com/{version}` |
| Auth | header `X-API-Key` | header `Authorization: Bearer {token}` |
| Path | `/{phone_number_id}/messages` | igual |
| Payload | — | **idéntico**, no se toca |

La versión de API va en config (`META_API_VERSION`, default `v23.0`), no hardcodeada.

### Cambios en la verificación de firma

Meta manda el header **`X-Hub-Signature-256`** y el valor viene con prefijo:

```
X-Hub-Signature-256: sha256=a1b2c3...
```

Hay que **sacar el prefijo `sha256=`** antes de comparar. El HMAC es SHA256 del body crudo usando el **App Secret** de la app de Meta (`META_APP_SECRET`), no un secreto que se configure aparte.

Mantener la lógica actual de escape por `DEBUG`: sin secreto configurado y con `DEBUG=false`, rechazar.

Seguir usando `hmac.compare_digest`.

### Función nueva: verificación del webhook (GET)

Meta valida la URL del webhook con un GET antes de empezar a mandar eventos:

```
GET /webhook?hub.mode=subscribe&hub.verify_token=XXX&hub.challenge=1158201444
```

Si `hub.mode == "subscribe"` y `hub.verify_token` coincide con `META_VERIFY_TOKEN`, hay que responder **el valor de `hub.challenge` como texto plano** (no JSON) con status 200. Si no coincide, 403.

El `verify_token` lo inventás vos y lo cargás igual en el panel de Meta y en la config.

---

## 2. Endpoint del webhook en `main.py`

### Ruteo

Kapso ruteaba por el header `X-Webhook-Event`. **Meta no manda ese header.** Hay que inspeccionar la estructura del payload.

### Estructura del payload de Meta

```json
{
  "object": "whatsapp_business_account",
  "entry": [{
    "id": "<waba_id>",
    "changes": [{
      "field": "messages",
      "value": {
        "metadata": {"phone_number_id": "..."},
        "contacts": [{"profile": {"name": "Tomi"}, "wa_id": "549299..."}],
        "messages": [{
          "from": "549299...",
          "id": "wamid.HBgLNTQ5...",
          "timestamp": "1735689600",
          "type": "text",
          "text": {"body": "hola"}
        }]
      }
    }]
  }]
}
```

Mapeo al contrato interno:

- `identificador_externo` ← `messages[].from`
- `wa_message_id` ← `messages[].id`
- `contenido` ← `_extraer_contenido(messages[])` — **la función actual sirve sin cambios**, Meta usa el mismo `type` / `text.body`

### Recorrido

**Iterar sobre `entry[]` y `changes[]`.** Meta puede mandar varios mensajes en una sola entrega, incluso de personas distintas. No asumir un solo mensaje.

Encolar una `background_task` por cada mensaje, como se hace hoy.

### Statuses: descartar

Cuando el evento es de entrega, `value` trae **`statuses[]`** en lugar de `messages[]`:

```json
"value": {"statuses": [{"id": "wamid...", "status": "delivered", "recipient_id": "..."}]}
```

Chequear explícitamente: **si `value` no tiene `messages`, no procesar nada.** No inferir por descarte.

Loguear a nivel debug y devolver 200. Nunca devolver error ante un status: Meta lo lee como fallo y reintenta.

### Siempre 200

Ante payload incompleto, tipo desconocido o cualquier cosa inesperada, loguear y devolver 200. Un 4xx o 5xx hace que Meta reintente y, si se repite, que desuscriba el webhook.

La única excepción es la firma inválida: ahí sí 401 o 403.

---

## 3. ⚠️ Números argentinos — no normalizar

Meta devuelve `from` y `wa_id` con el formato que usa internamente. **Para Argentina el "9" puede aparecer o no** (`5492996XXXXXX` vs `542996XXXXXX`), y no siempre coincide con cómo la persona tiene guardado el número.

**Reglas:**

- Guardar `identificador_externo` **exactamente como viene** en `from`. No agregar ni sacar el 9, no reformatear.
- Para responder, usar **ese mismo valor verbatim** como `to`.
- Nunca construir el destinatario a mano ni normalizar el formato.

Si se normaliza, se rompe la búsqueda de conversación (se duplican contactos) o directamente falla el envío.

---

## 4. Config

Campos nuevos en `app/config.py`:

| Variable | Descripción |
|---|---|
| `META_PHONE_NUMBER_ID` | ID del número, del panel de Meta |
| `META_ACCESS_TOKEN` | Token permanente del System User |
| `META_APP_SECRET` | Para validar la firma |
| `META_VERIFY_TOKEN` | Inventado, se carga igual en Meta y acá |
| `META_API_VERSION` | Default `v23.0` |

Documentar todos en `.env.example`. Los de Kapso quedan, pero marcados como obsoletos.

---

## 5. Lo que queda dormido

**`procesar_mensaje_saliente` y la pausa por intervención humana no tienen disparador bajo Cloud API.**

Ese flujo dependía de `kapso.direction == "outbound"` y `kapso.origin == "business_app"`, que Kapso emitía cuando alguien respondía desde la app de WhatsApp Business. Con Cloud API no existe app de negocio: los mensajes salen por API o no salen.

**No borrar nada.** El código y sus tests quedan intactos, igual que `escalar_a_humano`. Si más adelante ENE habilita Coexistence o se agrega una bandeja, el disparador se vuelve a conectar.

Sí conviene dejar disponible el reseteo manual (`resetear_modo_humano.py`) como interruptor de emergencia.

---

## 6. Verificación

1. `GET /webhook` con el verify token correcto devuelve el challenge en texto plano, sin comillas ni JSON.
2. `GET /webhook` con token incorrecto devuelve 403.
3. `POST /webhook` con firma inválida devuelve 401.
4. Un payload con `statuses[]` devuelve 200 y no encola ninguna tarea.
5. Un payload con dos mensajes en el mismo `entry` encola dos tareas.
6. Un payload con `type` no soportado (imagen, audio) guarda el placeholder y no rompe.
7. El mismo `wa_message_id` entregado dos veces se procesa una sola vez.
8. La suite completa sigue pasando.

## 7. Fuera de alcance

- Templates y mensajes fuera de la ventana de 24hs.
- Descarga de media (audio, imágenes).
- Botones y listas interactivas.
- Migración a PostgreSQL (va en un cambio aparte).
