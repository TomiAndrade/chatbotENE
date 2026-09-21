# Spec — Adjuntos no soportados (entrega 1.1 de roadmap-bot-crm.md)

Entrega chica sobre el flujo existente de mensajes entrantes. No reemplaza
`spec-etapa2.md` ni `spec-meta-cloud-api.md`: los complementa en el punto
puntual de qué pasa cuando `messages[].type` no es `text`.

## Problema

`app/main.py:_extraer_contenido` ya distinguía texto de no-texto usando el
campo real del webhook (`mensaje.get("type")`) — eso estaba bien. El problema
era lo que pasaba después: el mensaje con el placeholder
(`[mensaje de tipo 'image' no soportado en esta etapa]`) seguía el mismo
camino que cualquier mensaje de texto y terminaba armando historial y
llamando a `generar_respuesta`. El modelo recibía un marcador de texto en vez
del adjunto real, sin ninguna instrucción de qué hacer con eso — a veces lo
ignoraba, a veces alucinaba sobre "la imagen que mandaste".

## Alcance de esta entrega

- El tipo real del webhook (`messages[].type`) decide, **antes** de llamar a
  `responder()`, si el mensaje es texto o un adjunto no soportado. La
  decisión se toma sobre la metadata, nunca comparando el contenido guardado
  contra el texto del marcador — así un usuario que escribe a mano algo
  parecido al marcador (tipo `text`) sigue yendo por el camino de texto
  normal.
- Tipos soportados: los que ya trataba como soportados el código actual, que
  es únicamente `text` (`_extraer_contenido` solo devuelve el body real
  cuando `type == "text"`; todo lo demás ya caía al placeholder). Esta
  entrega no amplía ni reduce esa lista, solo cambia qué pasa con lo que ya
  caía al placeholder.
- Un adjunto no soportado:
  - se guarda igual que hoy, con su `wa_message_id`, para deduplicación e
    historial (sin cambios acá);
  - **no** llama a `generar_respuesta` ni arma el historial para el modelo;
  - responde con un texto fijo, corto, elegido según el tipo (`image`,
    `document`, `audio` tienen texto propio; cualquier otro tipo —
    `video`, `sticker`, `location`, `contacts`, `interactive`, `button`,
    `order`, `system`, `unknown`, o un `type` ausente— usa un texto
    genérico), que aclara que el bot no puede ver imágenes ni documentos ni
    escuchar audios y pide la consulta por escrito;
  - **no** escala a humano por el solo hecho de ser un adjunto;
  - respeta exactamente los mismos chequeos que un mensaje de texto: firma
    del webhook, dedup por `wa_message_id`, `modo_humano` (releído antes de
    enviar, igual que cualquier otro envío — ver `enviar_y_guardar`), y el
    límite de mensajes por hora (si el adjunto es el mensaje que cruza el
    límite, dispara el aviso de límite en vez del texto de adjunto, igual
    que pasaría con un mensaje de texto).
- El texto fijo de respuesta se guarda como mensaje del bot (mismo mecanismo
  que cualquier otro envío, vía `enviar_y_guardar`), así que entra al
  historial de la conversación como cualquier respuesta del bot.

## Fuera de alcance (no tocado por esta entrega)

- Agrupar mensajes consecutivos (1.2 del roadmap).
- El CRM (1.3 en adelante).
- Transcripción de audio, botones/listas/plantillas: siguen fuera de
  alcance, sin cambios (ver PENDIENTES.md, sección 9).
- El flujo de texto normal, la deduplicación, el límite por hora, la firma
  del webhook y la pausa humana: ninguno cambia de comportamiento, solo se
  reusan.

## Diseño

- `app/mensajes.py` suma los textos fijos y una función
  `mensaje_adjunto_no_soportado(tipo: str | None) -> str` que mapea
  `image`/`document`/`audio` a su texto propio y cualquier otro valor
  (incluido `None`) al texto genérico.
- `app/main.py`:
  - `_procesar_cambio` ya lee `mensaje.get("type")` a través de
    `_extraer_contenido`; ahora además pasa ese tipo, tal cual vino del
    webhook, a `procesar_mensaje_entrante` como parámetro nuevo (no se
    deriva por lectura del contenido guardado).
  - `procesar_mensaje_entrante` recibe `tipo` y, después de los chequeos de
    duplicado/pausa/límite (sin tocarlos), decide entre `responder()` (si
    `tipo == "text"`) y la función nueva `responder_adjunto_no_soportado()`
    (cualquier otro valor, incluido `None`).
  - `responder_adjunto_no_soportado(db, conversacion, tipo)` es una función
    chica: solo llama a `enviar_y_guardar` con el texto que corresponda.
    No arma historial, no llama a `generar_respuesta`, no llama a
    `escalar_a_humano`.

## Cierre / criterios de aceptación

- Tests con dobles (sin pegarle a Meta ni a ningún proveedor de IA real):
  - texto normal sigue funcionando igual (regresión);
  - imagen, documento, audio y un tipo desconocido/ausente reciben cada uno
    el texto fijo que les corresponde, y **no** se llama a
    `generar_respuesta`/al proveedor;
  - un mensaje de texto cuyo contenido imita el marcador
    (`[mensaje de tipo 'image' no soportado en esta etapa]`) sigue yendo
    por el camino normal de texto (sí llama al proveedor, no dispara el
    camino de adjunto);
  - dedup del mismo `wa_message_id` de un adjunto;
  - `modo_humano` corta el envío del texto fijo, igual que corta un texto
    normal;
  - el límite de mensajes por hora se respeta con adjuntos (incluido que un
    adjunto que cruza el límite dispara el aviso de límite, no el texto de
    adjunto);
  - un webhook con varios mensajes de tipos distintos en el mismo `entry`
    procesa cada uno según su propio tipo.
- Suite completa corrida y sin regresiones nuevas.
- La prueba real contra WhatsApp (que un adjunto real —foto, PDF, nota de
  voz— dispare el texto correcto en un chat real) queda pendiente, separada
  de esta entrega con dobles: ver "Validar la etapa 2 contra servicios
  reales" en PENDIENTES.md, sección 1, que ya cubre la prueba manual
  end-to-end de la que esto forma parte.
