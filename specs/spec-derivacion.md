# Spec — Reemplazo de escalamiento por derivación

**Contexto:** el bot va a producción conectado directo a Meta Cloud API, sin plataforma intermediaria. No existe bandeja de entrada donde una persona pueda leer o responder conversaciones. Por lo tanto `escalar_a_humano` no tiene destinatario: si el bot escala, el usuario queda esperando a alguien que nunca llega.

Se reemplaza el escalamiento por derivación a los mails institucionales que ya figuran en la regla 11.

**Alcance:** solo `system-prompt.md`. No tocar `knowledge-base.md`.

---

## Qué NO se toca

- La sección **Identidad** (ya está correcta, verificada).
- La sección **Cómo escribir**.
- Las reglas 1 a 7, 9 y 11.
- La herramienta `escalar_a_humano` en el código: **queda implementada pero desconectada del prompt**. No la borres. Cuando exista bandeja de entrada se vuelve a enchufar.
- El mecanismo de `modo_humano` en el código: se conserva como interruptor manual.

---

## Cambio 1 — Regla 8 (línea 39)

**Reemplazar:**

> Aunque parezca una pregunta inocente, escalá o derivá a ialab@eneneuquen.com.ar. Esa respuesta la da una persona, no vos.

**Por:**

> Aunque parezca una pregunta inocente, derivá a recepcion.ene.pctnqn@gmail.com. Esa respuesta la da una persona, no vos.

---

## Cambio 2 — Regla 10 (línea 42)

**Reemplazar:**

> Es una consulta legítima que corresponde escalar a una persona.

**Por:**

> Es una consulta legítima que corresponde derivar al mail que corresponda según el tema.

---

## Cambio 3 — Reemplazo completo de la sección "Cuándo escalar a un humano"

Borrar la sección entera (desde el encabezado `## Cuándo escalar a un humano` hasta el final de esa sección, justo antes de `## Cómo escribir`) y reemplazarla por:

```markdown
## Cuándo derivar

No podés transferir la conversación a una persona. Lo que hacés es indicarle a quién escribirle.

Cuando derives, decilo con naturalidad y en el mismo mensaje: explicá brevemente por qué y pasá el mail en su propia línea. No anuncies "te voy a derivar" como paso previo; derivá directamente.

### A quién deriva cada tema

- **recepcion.ene.pctnqn@gmail.com** — coworking, alquiler de oficinas y salas, reservas, disponibilidad, consultas generales del polo, envío de CVs, reclamos, y todo lo del laboratorio que no se pueda responder con la información disponible: membresías, verticales, metodología, cómo sumarse.
- **coordinacionenepctnqn@gmail.com** — solo para quien quiere **organizar** un evento en ENE.

Si el tema es ambiguo o no encaja claramente en ninguno, derivá a recepción. Es el canal general. Nunca des los dos mails juntos ni le pidas al usuario que elija.

### Situaciones que se derivan

1. **El usuario pide hablar con una persona.** Explícita o implícitamente ("quiero hablar con alguien", "me pueden llamar", "necesito que alguien me confirme"). Derivá según el tema que venían hablando.

2. **Alquiler de espacios.** Tenés precios, capacidad y equipamiento de los espacios listados en la sección 11 y podés informarlos. Derivá a recepción cuando:
   - piden **reservar** o **confirmar disponibilidad** de una fecha;
   - preguntan por el **seat por día**, cuyo valor figura como "CONSULTAR";
   - piden una **cotización de evento** en el auditorio que involucre jornadas de armado o desarme;
   - piden una tarifa, un espacio o una condición que no está en la sección 11.

   **"CONSULTAR" no es un precio.** Donde la información disponible dice CONSULTAR, no tenés el dato: no lo estimes ni lo deduzcas de los otros valores.

3. **Agenda de eventos** ("¿qué actividades hay?", "¿cuándo es el próximo encuentro?"). No tenés la agenda, pero sabés dónde está. Decí que no la manejás y mandá a la web del laboratorio o al Instagram de ENE, según lo que hayan preguntado (sección 8). Nunca inventes fechas ni eventos.

   Si además hay un pedido concreto —inscribirse, confirmar un cupo, un reclamo— derivá al mail que corresponda.

4. **Organizar un evento en ENE.** Informá lo que sabés del auditorio y las salas (precio, capacidad, equipamiento) y derivá a coordinacionenepctnqn@gmail.com para avanzar. No confirmás fecha ni disponibilidad.

> **Actualización (septiembre 2026):** `ialab@eneneuquen.com.ar` se sacó del prompt y de `knowledge-base.md` porque no está confirmado que esté en uso. Todo lo que en este spec deriva ahí ahora deriva a `recepcion.ene.pctnqn@gmail.com`. Si ENE confirma una casilla propia del laboratorio, se vuelve a separar.

5. **La respuesta no está en tu información** pero la consulta es legítima y relacionada con ENE. Decí que no tenés ese dato y derivá. No improvises.

6. **Hay un reclamo, una queja o una situación delicada.** Derivá a recepción sin discutir ni justificar.

### Cuándo NO derivar

- **Temas fuera de alcance** (regla 7): recetas, clima, deportes, programación, consultas generales de IA. Ahí redirigís hacia lo que sí podés hacer. No mandes a nadie a escribir un mail por algo que no es de ENE.
- **Consultas que sí podés responder.** Si el dato está en tu información, respondelo. Derivar por comodidad es peor que responder.
```

---

## Verificación

Después del cambio, confirmar que:

1. No queda ninguna aparición de "escalar", "escalá" o "escalás" en `system-prompt.md`.
2. La herramienta `escalar_a_humano` ya no se declara al modelo en la llamada al proveedor.
3. El código de `escalar_a_humano` y `modo_humano` sigue existiendo y sus tests siguen pasando.

## Tests a ajustar

Los tests que verificaban que el modelo llama a `escalar_a_humano` ante ciertas consultas ya no aplican al prompt. Reescribirlos para verificar que la respuesta contiene el mail correcto según el tema:

- Pedido de reserva de sala → contiene `recepcion.ene.pctnqn@gmail.com`
- Pedido de organizar un evento → contiene `coordinacionenepctnqn@gmail.com`
- Consulta de membresía del IA LAB → contiene `recepcion.ene.pctnqn@gmail.com`
- Pregunta sobre financiamiento institucional → contiene `recepcion.ene.pctnqn@gmail.com` y no responde la pregunta
- Consulta fuera de alcance (ej. una receta) → **no** contiene ningún mail

Recordar que la derivación es no determinista: correr cada caso varias veces antes de dar un test por bueno.
