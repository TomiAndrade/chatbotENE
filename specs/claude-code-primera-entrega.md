# Encargo para Claude Code: primera entrega del roadmap

El usuario pidió que Claude Code haga todo el trabajo de código y tests;
Codex coordina el roadmap y revisa. Implementá ahora exclusivamente la entrega
1.1 de `specs/roadmap-bot-crm.md`: respuesta fija a mensajes no soportados.

Leé `AGENTS.md`, `CLAUDE.md`, los specs aplicables y el código actual antes de
editar. Hay documentos históricos desactualizados: contrastá con el código
y documentá cualquier diferencia relevante. Trabajá en `develop`.

Creá un spec breve de esta entrega y luego implementá código, tests y ajustes
documentales necesarios. No te quedes solamente en un plan. Usá metadatos
reales del webhook, no una coincidencia textual con el marcador. Definí los
tipos soportados a partir del código actual. No cambies el flujo de textos,
la deduplicación, los límites, la firma ni la pausa humana. Un adjunto no debe
llamar al modelo ni disparar escalamiento por sí mismo.

Tests con dobles, sin contactar APIs ni usuarios reales: texto normal,
imagen/documento/audio y tipo desconocido, imitación textual del marcador,
dedup, modo humano, límite, errores de envío y procesamiento de varios
mensajes por webhook. Afirmá contenido y ausencia de llamadas al proveedor.
Seleccioná los tests relevantes existentes y agregá solo los que faltan.
Corré la suite y reportá fallos preexistentes por separado si los hubiera.

Descubrí el runtime local disponible (incluida `.venv`) sin instalar software
global. No leas ni muestres secretos de `.env`. No hagas deploy, push, commit,
resets, limpieza de archivos ajenos ni cambios a bases reales. No modifiques
configuraciones de permisos para evitar restricciones. Si una operación queda
bloqueada por permisos, reportá la operación exacta que falta.

Actualizá el estado de 1.1 en el roadmap y el pendiente correspondiente en
`PENDIENTES.md`, sin reescribir todo el historial. Al terminar, entregá un
resumen con archivos modificados, pruebas y resultados, limitaciones y
prueba manual pendiente. No implementes aún la agrupación ni el CRM.
