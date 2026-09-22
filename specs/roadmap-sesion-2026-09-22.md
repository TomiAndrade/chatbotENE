# Bot ENE + CRM + acceso desde IA LAB — contexto y roadmap

Fecha de corte: 22/09/2026. Documento para iniciar una sesión independiente.
No es autorización para implementar todo el roadmap. Hoy se organiza y luego se entrega un prompt por tarea cuando Tomi lo pida.

## Forma de trabajo acordada

- El asistente de esta sesión ayuda a decidir alcance, redacta prompts para Claude Code, recomienda Haiku/Sonnet/Opus y revisa los resultados con Tomi. No implementa por su cuenta ni lanza Claude automáticamente.
- Tomi copia los prompts a Claude Code. Claude realiza código, tests, migraciones y documentación técnica. Tomi maneja commits, push y publicación.
- Una tarea acotada por prompt. Incluir objetivo, contexto mínimo, archivos relevantes, exclusiones, criterios de aceptación y verificación. No entregar todos los prompts juntos.
- Al volver: pedir resumen de Claude, archivos cambiados y resultados de checks. Revisar el diff relevante si hay acceso al repo; sin diff no afirmar que se revisó el código.
- Los problemas encontrados se devuelven como correcciones para Claude; no corregir directamente salvo nuevo pedido explícito.
- Ahorro: respuestas breves, lecturas dirigidas, no releer todo el repo o toda la historia, no repetir auditorías ni pruebas ya suficientes. Sin subagentes ni esperas supervisando a Claude por defecto.
- Modelos de Claude, como criterio de trabajo y no garantía de rendimiento: Haiku para cambios mecánicos bien delimitados; Sonnet por defecto para backend, tests e integración; Opus solo para un problema complejo que lo justifique. No confundir el modelo de Claude Code con el modelo que usa el bot en producción.
- Tono informal argentino. No implementar, desplegar ni modificar datos reales por inferencia de este documento.

## Proyectos y límites

- Bot: `C:/Users/tomas/monorepo/chatbot-polo`, repo independiente `TomiAndrade/chatbotENE`, rama de trabajo `develop`; nunca push directo a `main`.
- Web: `C:/Users/tomas/monorepo/IALAB-WEB`, repo independiente `ialabenepctnqn/web-ia-lab`. `main` publica en Netlify; trabajar en `develop`.
- Leer instrucciones aplicables antes de cada tarea. No cambiar de rama ni alterar cambios preexistentes automáticamente.
- Backend: pruebas pertinentes y llamadas directas cuando corresponda. Frontend: no levantar dev server ni tomar screenshots; Tomi verifica visualmente. Usar build si existe y verificaciones estáticas adecuadas al sitio vanilla.
- Nunca exponer secretos. Migraciones se preparan y prueban aparte; no ejecutarlas en producción sin pedido específico.

## Estado real y evidencia

- Tomi confirmó que el bot ya fue probado, funciona bastante bien y se mejora en producción. No volver a tratar el proyecto entero como no desplegado por leer documentación vieja.
- Panel publicado: https://chatbotene.onrender.com/crm/metricas . No entrar ni cambiar configuración solo por tener esta URL.
- El chat compartido leído reporta mejoras al prompt: brevedad, preguntar antes del catálogo, no ofrecer redactar mails/CV, no inventar capacidades y no abrir un menú ante un simple “ok”.
- Adjuntos no soportados: respuesta fija según tipo real, sin llamada al modelo. Agrupamiento: procesamiento por conversación, mensajes recibidos durante generación van al lote siguiente y recuperación de lotes abandonados.
- La última entrega de agrupamiento del chat reporta 295 tests; la del dashboard 324. Son resultados históricos reportados, no tests ejecutados en esta sesión. El roadmap anterior dice 282 y quedó atrasado.
- Dashboard implementado: conversaciones, mensajes por autor, llamadas, errores, escalamiento, tiempo de generación, tokens por proveedor/modelo y costo estimado de IA.
- Tarifas mediante `TARIFAS_IA_JSON`; N/D puede indicar precios o tokens faltantes. Hosting, base y Meta no están incluidos en ese cálculo.
- En producción faltaban columnas del agrupamiento. Tomi confirmó en el chat la creación de las cuatro: `generando_desde`, `generando_token`, `ultimo_mensaje_agrupado_id` y `mensajes.tipo`. Eso no verifica por sí solo todo el esquema ni cada funcionalidad.
- Último asunto del chat: dos segundos de agrupamiento resultaban insuficientes; se propusieron 5 segundos de ventana, 15 de espera máxima y 60 de abandono. Falta confirmar qué quedó efectivamente activo en Render. No asumirlo por los defaults locales.
- No consta cierre individual de las pruebas reales de adjuntos y ráfagas; preguntar solo si esa evidencia hace falta para la tarea elegida.

## Métricas disponibles, con límites

Captura aportada por Tomi, rango 01/08–23/09/2026: 11 conversaciones con actividad y 11 nuevas; 112 mensajes entrantes, 111 del bot, 0 humanos; 3 llamadas IA; 0 errores/escaladas; generación media 16,5 s; costo N/D. Proveedor `openai_compat`, modelo `openai/gpt-5-mini`: 60.690 tokens de entrada y 3.121 de salida.

El registro de llamadas se agregó con el dashboard: no dividir el consumo de esas tres llamadas por los 111 mensajes históricos para estimar costo unitario. Usar un período con cobertura conocida. Cero errores en esta muestra no prueba calidad general. La demora de agrupamiento puede sumarse a la de generación: medir latencia total antes de ajustar.

## Prioridades de hoy (orden propuesto, no compromiso de completar todas)

### 1. Botón flotante en IA LAB

Decisión de Tomi: abrir WhatsApp; el chat embebido queda pendiente. No crear un canal web ahora.

- Confirmar número público del bot; no inferirlo de IDs de Meta o teléfonos de pruebas del chat.
- Definir etiqueta y mensaje precargado; “Consultanos por WhatsApp” y “Hola, vengo de la web de IA LAB” son propuestas, no copy aprobado.
- Integrar con el layout compartido y revisar comportamiento en intro, móvil y páginas internas; sin tapar navegación o contenido.
- Cierre: enlace con destino correcto, mensaje codificado, accesibilidad por teclado y revisión visual de Tomi. Publicación por separado.
- Modelo orientativo: Haiku si el cambio queda totalmente delimitado; Sonnet si hay interacción con el layout.

### 2. Medición y costos de esta semana

- Confirmar la configuración de agrupamiento y la cobertura temporal de métricas, sin rehacer el dashboard.
- Identificar proveedor de facturación real antes de fijar tarifas; un nombre de modelo no prueba que se facture directo con ese fabricante.
- Revisar contrato de `TARIFAS_IA_JSON` y proponer configuración con tarifas verificadas, moneda y unidad. No inventar precios ni confundir tokens cacheados con normales.
- Meta: verificar tabla efectiva desde 01/10/2026, país de destino, franquicias y categorías. La consulta directa de Meta falló en esta sesión. Gupshup anunció 1.000 mensajes de servicio gratis por número/mes y cobro posterior; tratarlo como referencia a confirmar, no tarifa oficial ya validada.
- Un enlace común de la web a WhatsApp no se debe presupuestar como entrada gratuita de anuncios Click-to-WhatsApp.
- Inicio limpio de medición sin borrar historial. Propuesta de miércoles/jueves: 5–10 personas, 30–50 conversaciones; viernes revisar muestra y proyectar escenarios mensuales. Tomi aún no aprobó cantidades.
- Casos: membresías, alquiler genérico/precios, reservas, información ausente, fuera de tema, adjuntos y ráfagas. Medir resolución, mensajes salientes entregados, consumo IA y tiempo total.
- Separar costo IA, Meta, hosting y base; contrastar estimaciones con consumos reales. Revisar cobertura de estados de entrega antes de asumir que mensajes guardados equivalen a cobrados.
- Modelo orientativo: Sonnet. No agregar analítica compleja ni RAG sin una necesidad medida.

### 3. Exportación de conversaciones

Pedido explícito de Tomi; formato todavía no elegido. Recomendación: Markdown para analizar chats con IA; TXT para lectura simple; JSON después si se automatizan evaluaciones. PDF/CSV no son primera opción para el historial.

- Antes del prompt, cerrar uso principal (IA/equipo/ambos) y formato. Propuesta mínima: exportar una conversación completa desde su detalle.
- Conservar orden, autores, fechas/horas con zona, identificador y canal; incluir todos los mensajes aunque la vista esté paginada. Señalar adjuntos sin inventar contenido.
- Propuesta: opción de ocultar nombre/teléfono; no prometer anonimización del contenido libre.
- No generar resúmenes con IA ni exportaciones masivas como parte implícita. Mantener acceso autenticado y autorización existente.
- Cierre: descarga íntegra, caracteres correctos, permisos verificados y prueba con historial largo. Modelo orientativo: Sonnet.

### 4. Marcar “Respondió mal” (etapa 1.3)

Retomar el roadmap original: marca sobre mensaje del bot, motivo, comentario, respuesta esperada opcional, autor/fecha y consulta de pendientes. No enviar esas notas al usuario ni agregarlas automáticamente al prompt.

Puede hacerse después de exportación; son entregas separadas. No acoplar exportación a un esquema de marcas aún inexistente. Sesión/CSRF, texto seguro y migración reversible si corresponde. Modelo orientativo: Sonnet.

## Después de hoy

1. Casos reproducibles a partir de errores reales anonimizados; distinguir mocks de evaluación con modelo real.
2. Depurar conocimiento con comparación antes/después; conservar datos comerciales y límites. El contexto extenso es una hipótesis de costo a medir, no autorización para recortarlo a ciegas.
3. Bandeja pendiente/en atención/resuelta, independiente de bot activo/pausado; luego búsqueda, etiquetas, notas y responsables.
4. Atención humana desde CRM, control de carreras, devolución explícita al bot y avisos por mail. Adelantar si hay consultas reales sin atención; no afirmar que está resuelto por cero escaladas en la captura.
5. Canal de chat embebido en IA LAB: backlog explícito.

## Trampas conocidas

- `AGENTS.md` y `PENDIENTES.md` contienen estado histórico anterior al deploy y al dashboard. Respetar reglas vigentes, pero contrastar afirmaciones de estado con evidencia reciente.
- `create_all()` no agrega columnas a tablas existentes. El script SQL faltó en un deploy: verificar que las migraciones formen parte de la entrega antes de publicar.
- No rellenar indiscriminadamente mensajes históricos con tipo `text`: el recuperador podría tratarlos como pendientes y responder chats antiguos.
- El asistente del chat anterior infirió que agrupar funcionaba por el tipo del mensaje: eso solo no prueba el comportamiento completo.
- No asumir que “modo humano” implica que alguien fue notificado o respondió. El bot nunca entrega datos bancarios, CBU ni alias.

## Archivos para leer o adjuntar

Arrancar con este archivo. No hace falta adjuntar toda la conversación.

- `specs/roadmap-bot-crm.md`: detalle del roadmap previo; estado de ejecución parcialmente desactualizado.
- `specs/spec-dashboard-metricas.md`: para costos y cobertura del panel.
- `specs/spec-agrupamiento-mensajes.md`: solo al revisar agrupamiento.
- `AGENTS.md` y `PENDIENTES.md`: reglas y pendientes, con el límite histórico señalado.
- `IALAB-WEB/AGENTS.md`: antes de la tarea del botón.
- Para revisar una entrega en Chat sin repo: adjuntar diff, resumen y salida de tests pertinente. Con Codex local, indicar archivos cambiados y revisar allí.

Referencia del chat previo (ya resumida aquí): https://chatgpt.com/s/cx_6ab26f6cb3748191b600de2ac575f917
Referencia de cambio Meta no validada directamente con Meta: https://support.gupshup.io/hc/en-us/articles/62362400519705-WhatsApp-Service-Messages-Pricing-w-e-f-01-Oct-2026
