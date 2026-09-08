# Spec: validación de configuración al arranque

## Contexto

El bot arranca aunque falte configuración. Los defaults silenciosos hacen que un
deploy mal configurado quede "andando" pero roto:

- Si falta `DATABASE_URL`, cae a SQLite efímero. El bot responde, guarda en un
  archivo que se pierde en el próximo deploy, y nadie se entera.
- Si falta `PROVEEDOR_IA`, cae al proveedor `fijo`.

Esto es especialmente grave en esta arquitectura porque **no hay bandeja de
entrada**: si algo falla, el usuario no recibe nada, Meta ya se llevó su 200 y no
reintenta, y el único rastro son los logs de Render. Un bot roto que arranca es
peor que un deploy que falla.

## Objetivo

Que el proceso no levante si la configuración está incompleta o es incoherente,
y que quede en los logs contra qué configuración está corriendo.

## Decisión de diseño: sin modo dev

**Las reglas son idénticas en todos los entornos.** No se agrega ninguna variable
`ENTORNO` / `ENV` / `DEBUG` que relaje las validaciones. No hay ningún camino por
el cual la validación se salte.

Razón: cualquier escape hatch termina activándose en producción por accidente, y
el modo dev que permite SQLite reintroduce exactamente el fallo que esta spec
elimina. El costo asumido es que correr local exige levantar Postgres.

## Dónde se dispara la validación

**Al arrancar la aplicación, no al importar el módulo de config.**

Esto es un requisito duro. La suite de tests corre contra SQLite; si la
validación se ejecuta como efecto colateral del import, rompe todos los tests.

Implementar como una función explícita de validación, invocada desde el startup
de la aplicación FastAPI (evento de arranque o lifespan). Importar el módulo de
config no debe disparar nada.

Si la validación falla: loguear el error completo y terminar el proceso con
código de salida distinto de cero, **antes** de que el server empiece a aceptar
requests. En Render eso marca el deploy como fallido y mantiene sirviendo la
versión anterior, que es el comportamiento deseado.

## Reglas de validación

### 1. Eliminar los defaults peligrosos

`DATABASE_URL` y `PROVEEDOR_IA` dejan de tener valor por defecto. Ausentes, son
error de validación. No hay fallback a SQLite ni a `fijo` en ningún caso.

### 2. Presencia de las obligatorias

Todas las variables requeridas para operar deben estar presentes y no vacías
(string vacío o solo espacios cuenta como ausente).

**Relevar el módulo de configuración actual del proyecto para obtener los nombres
reales — no inventar ni asumir nombres.** El conjunto incluye, como mínimo:

- La URL de la base de datos
- El proveedor de IA
- Las credenciales de Meta Cloud API (token de acceso, phone number ID, verify
  token del webhook, app secret para verificar firma)

### 3. Coherencia por modo, no solo presencia

No alcanza con que las variables existan. Cada modo activo exige su conjunto:

- Si el proveedor de IA es `openai_compat`: deben estar la API key y el nombre
  del modelo.
- Si `ESCALAMIENTO_HABILITADO` está en true o la derivación por mail está
  activa: deben estar completas las credenciales SMTP (host, puerto, usuario,
  contraseña, destinatarios).
- Cualquier otro modo condicional que exista en la config actual sigue la misma
  lógica: si el modo está activo, sus dependencias son obligatorias.

### 4. Validación de formato de `DATABASE_URL`

No basta con que exista. Debe empezar con `postgresql://` o `postgresql+psycopg://`
(verificar cuál usa el proyecto). Una URL mal pegada o apuntando a SQLite es
error de validación, no advertencia.

### 5. Reportar todos los errores juntos

La validación acumula **todos** los problemas encontrados y los reporta en un
único error, no aborta en el primero. Un deploy fallido debe alcanzar para saber
todo lo que falta.

El mensaje debe nombrar cada variable faltante o inválida y, cuando sea
condicional, decir por qué se exige ("SMTP_HOST es obligatorio porque la
derivación por mail está habilitada").

## Línea de resumen al arrancar

Si la validación pasa, loguear una línea de resumen con la configuración
efectiva:

- Proveedor de IA y modelo
- Host y nombre de la base (**sin usuario ni contraseña**)
- Versión de la API de Meta que usa el código
- Estado de `ESCALAMIENTO_HABILITADO`

Esto da un ancla en los logs de Render para saber contra qué config está
corriendo lo que se está viendo.

**Ningún secreto en los logs, ni siquiera truncado o enmascarado.** Tokens, keys,
contraseñas y app secret no aparecen de ninguna forma. Si hace falta confirmar
que una key está cargada, alcanza con un booleano.

## Fuera de alcance

- Cualquier cambio en la lógica del webhook, el manejo de mensajes o el prompt.
- Migrar de v23.0 a v26.0 de la API de Meta (pendiente separado).
- Alertas por mail ante fallos de procesamiento (pendiente separado, arrastra una
  decisión de privacidad abierta).

## Criterios de aceptación

1. Levantar la app sin `DATABASE_URL` → el proceso muere con código distinto de
   cero y el log nombra la variable faltante. No se crea ningún archivo SQLite.
2. Levantar la app con `DATABASE_URL` apuntando a SQLite → mismo resultado.
3. Levantar sin `PROVEEDOR_IA` → mismo resultado.
4. Levantar con proveedor `openai_compat` pero sin API key → muere y el log
   explica que la key se exige por el proveedor elegido.
5. Levantar con tres variables faltantes → el log las lista a las tres, no solo
   la primera.
6. Levantar con la config completa → arranca normal y loguea la línea de resumen
   sin ningún secreto.
7. **La suite de tests sigue pasando sin cambios**, corriendo contra SQLite.
   Si algún test se rompe, la validación se está disparando en el import y hay
   que moverla al startup.
