# Especificación — Bot WhatsApp ENE IA LAB · Etapa 1

> Documento de contexto para Claude Code.
> **Alcance de esta etapa: plomería únicamente. Sin IA.**
> El objetivo es tener el circuito WhatsApp → servidor → base de datos → respuesta funcionando de punta a punta con respuestas fijas.

---

## Contexto del proyecto

Chatbot de WhatsApp para **ENE IA LAB**, un laboratorio de IA aplicada que opera en el Polo Tecnológico Neuquén. El bot atiende consultas del público sobre membresías, eventos y alquiler de espacios, y escala a un humano cuando no puede resolver.

El desarrollador es estudiante de Licenciatura en Ciencias de la Computación, con experiencia en Java y conocimientos básicos de Python. **El código debe ser legible y explicable por sobre ingenioso.** Preferir claridad a concisión.

---

## Stack

- **Lenguaje:** Python 3.11+
- **Framework web:** FastAPI
- **Base de datos:** SQLite en desarrollo, Postgres en producción
- **ORM:** SQLAlchemy — obligatorio, no escribir SQL crudo. La migración a Postgres debe ser un cambio de connection string.
- **Gestión de dependencias:** `requirements.txt` o `pyproject.toml`
- **Túnel local:** ngrok (externo, no es parte del código)

### Plataforma de WhatsApp

**Kapso** (https://docs.kapso.ai) — capa sobre la WhatsApp Cloud API oficial de Meta.
En esta etapa se usa el **sandbox** de Kapso, no un número productivo.

---

## Modelo de datos

### `conversaciones`

| Campo | Tipo | Notas |
|---|---|---|
| `id` | PK | |
| `telefono` | string, único, indexado | Número del usuario en formato internacional |
| `modo_humano` | boolean, default `false` | Si está en `true`, el bot NO responde |
| `creada_en` | datetime | |
| `ultimo_mensaje_en` | datetime | Se actualiza en cada mensaje entrante o saliente |

### `mensajes`

| Campo | Tipo | Notas |
|---|---|---|
| `id` | PK | |
| `conversacion_id` | FK → conversaciones | |
| `rol` | enum: `usuario` \| `bot` \| `humano` | Tres valores, no dos |
| `contenido` | text | |
| `wa_message_id` | string, nullable, único | ID del mensaje que devuelve Kapso |
| `creado_en` | datetime | |

**Sobre `rol`:** distinguir `bot` de `humano` importa. Cuando en la etapa 3 se arme el historial para el modelo, los mensajes escritos por una persona del polo son contexto válido pero no ejemplos de cómo debe responder el bot.

**Sobre `wa_message_id`:** sirve para deduplicar. Ver más abajo.

---

## Flujo a implementar

### Endpoint `POST /webhook`

Recibe las notificaciones de Kapso. Debe:

1. **Responder 200 lo antes posible.** Si el webhook tarda, Kapso reintenta y llegan mensajes duplicados. Toda la lógica pesada va después de confirmar la recepción, o en background.
2. **Deduplicar.** Si el `wa_message_id` ya existe en la base, descartar y salir. Los reintentos son normales, no un error.
3. **Buscar o crear la conversación** por número de teléfono.
4. **Guardar el mensaje entrante** con rol `usuario`.
5. **Si `modo_humano` es `true` → terminar acá.** No responder nada. Este chequeo es crítico: si el bot escribe encima de un humano, la experiencia se rompe.
6. **Generar la respuesta** llamando a la capa de respuesta (ver abajo).
7. **Guardar la respuesta** con rol `bot` y enviarla vía Kapso.

### Endpoint `GET /health`

Devuelve 200. Para verificar que el servicio está vivo.

---

## Capa de respuesta — punto clave de diseño

Toda la generación de respuestas debe quedar detrás de **una única función con firma estable**:

```
generar_respuesta(historial, mensaje_nuevo) -> str
```

En esta etapa su implementación devuelve un texto fijo. En la etapa 3 se reemplaza por una llamada a un LLM **sin tocar el resto del proyecto**.

Implementarla como una clase o módulo intercambiable, seleccionable por variable de entorno (`PROVEEDOR_IA=fijo` por ahora; después `claude`, `gemini`, etc.).

**Razón:** durante el desarrollo puede que se use Gemini o un modelo local por falta de créditos de API, y en producción Claude. Cambiar de proveedor tiene que ser tocar un archivo.

---

## Cliente de Kapso

Módulo aparte que encapsule las llamadas a la API de Kapso. Mínimo: enviar mensaje de texto.

- Consultar la documentación oficial en https://docs.kapso.ai antes de implementar — no asumir la forma de los endpoints ni del payload del webhook.
- Manejar errores de red con reintentos y backoff.
- Loguear request y response cuando `DEBUG=true`.

---

## Variables de entorno

Todas en `.env`, cargadas con `python-dotenv`. Incluir un `.env.example` con las claves vacías.

```
KAPSO_API_KEY=
KAPSO_PHONE_NUMBER_ID=
DATABASE_URL=sqlite:///./bot.db
PROVEEDOR_IA=fijo
DEBUG=true
```

### Seguridad — no negociable

- `.env` va en `.gitignore`. Verificarlo explícitamente.
- Ninguna clave hardcodeada en el código, ni en comentarios, ni en tests.
- El archivo `bot.db` también va en `.gitignore`.
- Si Kapso ofrece verificación de firma del webhook, implementarla. Sin eso, cualquiera que conozca la URL puede inyectar mensajes falsos.
- **El bot nunca debe enviar datos bancarios, CBU ni alias**, en ninguna etapa. Es una regla del proyecto, no una omisión temporal.

---

## Logging

Estructurado y legible en consola. Para cada mensaje: teléfono (puede ir parcialmente enmascarado), si se dedupliqué, si estaba en modo humano, y qué se respondió.

Durante el desarrollo esto se cruza con el panel de ngrok en `localhost:4040` para diagnosticar dónde se cortó el flujo.

---

## Estructura sugerida

```
/app
  main.py           FastAPI, endpoints
  models.py         SQLAlchemy
  db.py             sesión y conexión
  kapso.py          cliente de Kapso
  respuesta.py      generar_respuesta() — la capa intercambiable
  config.py         carga de variables de entorno
.env.example
.gitignore
requirements.txt
README.md
```

---

## Criterio de aceptación

La etapa está terminada cuando:

1. Se escribe al número sandbox desde un celular y el mensaje aparece en la consola del servidor.
2. El mensaje queda guardado en la base con su conversación asociada.
3. Llega una respuesta fija al WhatsApp del celular.
4. La respuesta también queda guardada, con rol `bot`.
5. Marcando `modo_humano = true` a mano en la base, el bot deja de responder a ese número.
6. Reenviar el mismo webhook desde el panel de ngrok **no** genera un mensaje duplicado.

---

## Fuera de alcance en esta etapa

- Llamadas a cualquier LLM
- Armado de historial para el modelo
- Detección automática de escalamiento
- Deploy
- Transcripción de audios
- Mensajes con botones, listas o plantillas

No implementar nada de esto todavía, aunque el diseño deba dejarles lugar.

---

## README

Incluir instrucciones de setup para alguien que clona el repo por primera vez: instalar dependencias, copiar `.env.example`, crear la base, levantar el servidor, levantar ngrok y configurar el webhook en Kapso.
