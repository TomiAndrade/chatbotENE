# Spec: respuestas más cortas y límite de scope en redacción de mails

## Contexto

Medición real de un mensaje en producción (15/09, log de Render):

| Etapa | Tiempo |
|---|---|
| Headers del modelo | 598 ms |
| **Generación del cuerpo** | **10.245 ms** |
| Envío a Meta | 890 ms |
| **Total** | **11.9 s** |

El 86% del tiempo es generación de tokens. Postgres y Meta son ruido. La única
palanca real es **generar menos**.

No hay optimización de infraestructura que compense 10 segundos de generación.

## Objetivo

1. Respuestas típicas de 4-5 líneas como máximo.
2. Latencia total por debajo de 9 segundos.
3. Acotar la ayuda con redacción de mails a lo que sea sobre ENE.

## Alcance

Edición del system prompt y del parámetro `max_tokens`. **No se toca código de
procesamiento, ni el cliente del modelo, ni el modelo elegido.**

El cambio de modelo queda explícitamente fuera: se evalúa después de medir el
efecto de este cambio, y por separado, para no confundir causas si algo se
degrada.

## Regla 1 — Largo de las respuestas

Agregar al system prompt una regla explícita de brevedad:

- La respuesta típica no supera las 4-5 líneas.
- Es WhatsApp, no un mail: sin encabezados, sin listas largas, sin cerrar cada
  respuesta con un menú de opciones.
- Una sola pregunta de seguimiento cuando haga falta, no varias.
- Si el usuario pide algo que legítimamente necesita más espacio (el cuerpo de
  un mail, por ejemplo), puede extenderse — pero sin agregar preámbulo ni
  epílogo alrededor.

El caso que originó esto: ante "me ayudás a redactar un mail", el bot respondió
con cinco bullets explicados más un pedido de datos de firma. La respuesta
correcta es preguntar a quién le escribe y qué necesita, en dos líneas.

## Regla 2 — Scope de la redacción de mails

El bot **sí** ayuda a redactar mails dirigidos a ENE o relacionados con ENE:
consultas a recepción, postulación al IA LAB, reserva de espacios, envío de CV
al polo, solicitud de visita.

El bot **no** ayuda a redactar correspondencia ajena a ENE: mails de trabajo del
usuario, cartas personales, textos que no tienen a ENE como destinatario o tema.

Ante un pedido fuera de scope, la respuesta es breve: no es algo con lo que pueda
ayudar, y ofrece lo que sí hace.

**Importante:** la regla no es "nunca redacto mails". El comportamiento
observado en producción fue mayormente correcto — todos los ejemplos que ofreció
eran sobre ENE. Lo que falta es el límite, no la prohibición.

## Regla 3 — `max_tokens` como techo

Relevar el valor actual de `max_tokens` en la configuración del proveedor y
ajustarlo a un techo acorde a respuestas de 4-5 líneas, **con margen suficiente
para que el cuerpo de un mail entre completo**.

El techo es una red de seguridad, no el mecanismo principal: la brevedad la
tiene que producir el prompt. Una respuesta cortada a mitad de frase es peor
que una respuesta larga.

## Advertencia de validación

**El historial de conversación pisa los cambios del system prompt.** Si se prueba
desde un número que ya conversó con el bot, el modelo imita sus respuestas
viejas y el cambio parece no haber funcionado.

Toda validación de este cambio se hace **desde un número limpio**, sin historial
previo.

## Criterios de aceptación

Medidos con el logging de tiempos por etapa ya instrumentado, desde un número sin
historial:

1. "hola" → respuesta de 4-5 líneas o menos.
2. "me ayudás a redactar un mail" → pregunta breve por destinatario y motivo, sin
   lista de opciones explicadas.
3. Pedido de mail sobre ENE (envío de CV, consulta a recepción) → el bot lo
   redacta. El cuerpo puede ser largo; el texto alrededor, no.
4. Pedido de mail ajeno a ENE (ej.: "escribime un mail para mi jefe pidiendo
   vacaciones") → el bot declina brevemente y ofrece lo que sí hace.
5. **Latencia total por debajo de 9 s** en los casos 1, 2 y 4, verificada en el
   log de tiempos.
6. Ninguna respuesta queda cortada a mitad de frase.
7. Las reglas que ya funcionaban siguen funcionando: no inventa datos, respeta
   las reglas de precios, resiste prompt injection. Reprobar los casos de
   prompt injection que ya se habían validado.

## Fuera de alcance

- Cambio de modelo (se evalúa después, por separado, con esta medición como
  línea de base).
- Cualquier cambio en el pipeline de procesamiento, la base o el envío a Meta.
