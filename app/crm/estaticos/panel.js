/* Panel del CRM: lista de conversaciones a la izquierda, historial a la
 * derecha, y un único botón que escribe algo ("Reactivar bot").
 *
 * Dos reglas que conviene no romper al tocar este archivo:
 *
 * 1. El contenido de los mensajes se pinta SIEMPRE con textContent, nunca con
 *    innerHTML. Lo que escribió la persona del otro lado de WhatsApp es
 *    texto, no HTML: si se inyectara como HTML, un mensaje con una etiqueta
 *    adentro se ejecutaría en la pantalla de quien atiende.
 * 2. El refresco periódico no puede moverle el scroll a quien está leyendo.
 *    Antes de agregar mensajes nuevos se mira si estaba abajo de todo; si no
 *    lo estaba, se agregan y el scroll se queda donde estaba.
 */

const SEGUNDOS_DE_REFRESCO = 8;
const CONVERSACIONES_POR_PAGINA = 40;
// El tope que acepta el servidor (LIMITE_CONVERSACIONES_MAXIMO en rutas.py).
const CONVERSACIONES_MAXIMO = 100;

const estado = {
  csrf: null,
  zonaHoraria: undefined,
  filtro: "todas",
  conversaciones: [],
  hayMas: false,
  desplazamiento: 0,
  elegidaId: null,
  mensajes: [],
  hayAnteriores: false,
  motivos: {},
};

const elementos = {
  layout: document.querySelector(".layout"),
  lista: document.getElementById("lista"),
  cargarMas: document.getElementById("cargar-mas"),
  conteoTodas: document.getElementById("conteo-todas"),
  conteoPausadas: document.getElementById("conteo-pausadas"),
  sinSeleccion: document.getElementById("sin-seleccion"),
  detalle: document.getElementById("detalle"),
  contacto: document.getElementById("contacto"),
  contactoMeta: document.getElementById("contacto-meta"),
  estadoChip: document.getElementById("estado-chip"),
  exportar: document.getElementById("exportar"),
  reactivar: document.getElementById("reactivar"),
  avisoPausa: document.getElementById("aviso-pausa"),
  avisoReactivado: document.getElementById("aviso-reactivado"),
  hilo: document.getElementById("hilo"),
  mensajes: document.getElementById("mensajes"),
  cargarAnteriores: document.getElementById("cargar-anteriores"),
  usuario: document.getElementById("usuario-actual"),
  errorGlobal: document.getElementById("error-global"),
  errorGlobalTexto: document.getElementById("error-global-texto"),
  reintentar: document.getElementById("reintentar"),
  volver: document.getElementById("volver"),
  salir: document.getElementById("salir"),
};

/* ---------- Acceso al servidor ---------- */

/* Lo que se muestra en la barra cuando la excepción no trae nada legible.
 * La barra nunca aparece sin texto: vacía no dice qué pasó y encima parece
 * un error de la pantalla, no de la carga. */
const ERROR_SIN_MENSAJE = "Algo falló al cargar. Probá de nuevo.";

const ERROR_DE_CONEXION =
  "No pudimos conectar con el servidor. Puede ser tu conexión, o que el servidor esté caído.";

async function api(url, opciones = {}) {
  let respuesta;
  try {
    respuesta = await fetch(url, { credentials: "same-origin", ...opciones });
  } catch (problema) {
    // `fetch` solo rechaza cuando el request no llegó a destino (sin red, el
    // servidor caído, el túnel cortado). El mensaje que trae lo escribe el
    // navegador, viene en inglés y cambia según cuál sea ("Failed to fetch",
    // "NetworkError when attempting to fetch resource"): no sirve para
    // mostrárselo a nadie.
    throw new Error(ERROR_DE_CONEXION);
  }

  if (respuesta.status === 401) {
    // La sesión venció o la cookie ya no vale: a la pantalla de entrar, sin
    // dar vueltas.
    window.location.href = "/crm/login?error=sesion";
    throw new Error("Sesión vencida");
  }
  if (!respuesta.ok) {
    const cuerpo = await respuesta.json().catch(() => ({}));
    throw new Error(
      cuerpo.detail || `El servidor respondió con un error (${respuesta.status}). Probá de nuevo.`
    );
  }
  return respuesta.json();
}

/* La barra roja de arriba: se muestra **solo** cuando algo falló.
 *
 * Una lista vacía no es un error y no pasa por acá — de eso se encarga
 * `pintarLista`, con el cartel "Todavía no hay conversaciones". Y cada carga
 * que sale bien llama a `limpiarError()`, así que la barra desaparece sola en
 * cuanto el servidor vuelve.
 *
 * Que el atributo `hidden` alcance para ocultarla depende de una regla del
 * CSS (`[hidden] { display: none !important }`): `.barra-error` fija
 * `display: flex`, y sin esa regla le gana al `hidden` del navegador y la
 * barra queda visible y vacía. Ya pasó.
 */
function mostrarError(problema) {
  elementos.errorGlobalTexto.textContent = problema.message || ERROR_SIN_MENSAJE;
  elementos.errorGlobal.hidden = false;
}

function limpiarError() {
  elementos.errorGlobalTexto.textContent = "";
  elementos.errorGlobal.hidden = true;
}

/* ---------- Fechas ---------- */

function formatear(iso, opciones) {
  if (!iso) return "";
  try {
    return new Intl.DateTimeFormat("es-AR", { timeZone: estado.zonaHoraria, ...opciones }).format(new Date(iso));
  } catch (problema) {
    // Zona horaria que el navegador no conoce: se cae a la hora local en vez
    // de dejar la pantalla sin fechas.
    return new Intl.DateTimeFormat("es-AR", opciones).format(new Date(iso));
  }
}

const hora = (iso) => formatear(iso, { hour: "2-digit", minute: "2-digit" });
const diaYMes = (iso) => formatear(iso, { day: "2-digit", month: "2-digit" });
const diaCompleto = (iso) => formatear(iso, { weekday: "long", day: "numeric", month: "long", year: "numeric" });
const claveDeDia = (iso) => formatear(iso, { year: "numeric", month: "2-digit", day: "2-digit" });

function momentoCorto(iso) {
  if (!iso) return "";
  return claveDeDia(iso) === claveDeDia(new Date().toISOString()) ? hora(iso) : diaYMes(iso);
}

/* ---------- Lista de conversaciones ---------- */

const NOMBRE_DE_ROL = { usuario: "Contacto", bot: "Bot", humano: "Equipo" };

function chipDeEstado(conversacion) {
  const chip = document.createElement("span");
  chip.className = conversacion.pausado ? "chip chip-pausado" : "chip chip-activo";
  chip.textContent = conversacion.pausado ? "Bot pausado" : "Bot activo";
  return chip;
}

function itemDeConversacion(conversacion) {
  const item = document.createElement("button");
  item.type = "button";
  item.className = "item" + (conversacion.id === estado.elegidaId ? " elegida" : "");
  item.dataset.id = String(conversacion.id);

  const fila = document.createElement("div");
  fila.className = "item-fila";

  const identificador = document.createElement("span");
  identificador.className = "item-identificador";
  identificador.textContent = conversacion.identificador_externo;

  const cuando = document.createElement("span");
  cuando.className = "item-hora";
  cuando.textContent = momentoCorto(conversacion.ultima_actividad);

  fila.append(identificador, cuando);

  const previa = document.createElement("p");
  previa.className = "item-previa";
  if (conversacion.vista_previa) {
    const quien = document.createElement("span");
    quien.className = "quien";
    quien.textContent = NOMBRE_DE_ROL[conversacion.vista_previa.rol] + ": ";
    previa.append(quien, document.createTextNode(conversacion.vista_previa.texto));
  } else {
    previa.textContent = "Sin mensajes todavía";
  }

  item.append(fila, previa, chipDeEstado(conversacion));
  item.addEventListener("click", () => elegir(conversacion.id));
  return item;
}

function pintarLista() {
  const scroll = elementos.lista.scrollTop;
  elementos.lista.replaceChildren();

  if (estado.conversaciones.length === 0) {
    const vacio = document.createElement("p");
    vacio.className = "estado-vacio";
    vacio.textContent =
      estado.filtro === "pausadas"
        ? "No hay conversaciones pausadas. El bot está respondiendo todas."
        : "Todavía no hay conversaciones.";
    elementos.lista.append(vacio);
  } else {
    estado.conversaciones.forEach((conversacion) => {
      elementos.lista.append(itemDeConversacion(conversacion));
    });
  }

  elementos.cargarMas.hidden = !estado.hayMas;
  // Refrescar la lista no puede tirar el scroll para arriba mientras alguien
  // la está recorriendo.
  elementos.lista.scrollTop = scroll;
}

function pintarEsqueleto() {
  elementos.lista.replaceChildren();
  for (let i = 0; i < 5; i += 1) {
    const hueso = document.createElement("div");
    hueso.className = "esqueleto";
    elementos.lista.append(hueso);
  }
}

async function cargarLista({ agregar = false } = {}) {
  if (!agregar) {
    estado.desplazamiento = 0;
  }

  // Al refrescar se vuelven a pedir todas las que ya estaban a la vista, no
  // la primera página nomás: si no, cada refresco le desharía el "Cargar
  // más" a quien lo acaba de apretar.
  const limite = agregar
    ? CONVERSACIONES_POR_PAGINA
    : Math.min(Math.max(CONVERSACIONES_POR_PAGINA, estado.conversaciones.length), CONVERSACIONES_MAXIMO);

  const parametros = new URLSearchParams({
    filtro: estado.filtro,
    limite: String(limite),
    desplazamiento: String(estado.desplazamiento),
  });

  const datos = await api(`/crm/api/conversaciones?${parametros}`);
  estado.conversaciones = agregar ? estado.conversaciones.concat(datos.conversaciones) : datos.conversaciones;
  estado.hayMas = datos.hay_mas;
  elementos.conteoTodas.textContent = String(datos.total);
  elementos.conteoPausadas.textContent = datos.total_pausadas > 0 ? String(datos.total_pausadas) : "";
  pintarLista();
}

/* ---------- Historial ---------- */

function estaAbajoDeTodo() {
  const distancia = elementos.hilo.scrollHeight - elementos.hilo.scrollTop - elementos.hilo.clientHeight;
  return distancia < 80;
}

function irAbajoDeTodo() {
  elementos.hilo.scrollTop = elementos.hilo.scrollHeight;
}

function separadorDeDia(iso) {
  const separador = document.createElement("li");
  separador.className = "separador-dia";
  const texto = document.createElement("span");
  texto.textContent = diaCompleto(iso);
  separador.append(texto);
  return separador;
}

function elementoDeMensaje(mensaje) {
  const item = document.createElement("li");
  item.className = `mensaje mensaje-${mensaje.rol}`;
  item.dataset.id = String(mensaje.id);

  const burbuja = document.createElement("div");
  burbuja.className = "burbuja";
  // textContent, nunca innerHTML (ver el comentario de arriba).
  burbuja.textContent = mensaje.contenido;

  const firma = document.createElement("div");
  firma.className = "firma";
  const quien = document.createElement("span");
  quien.className = "quien";
  const nombreRol = NOMBRE_DE_ROL[mensaje.rol] || mensaje.rol;
  quien.textContent = mensaje.rol === "humano" && mensaje.autor
    ? `${nombreRol} — ${mensaje.autor}`
    : nombreRol;
  const cuando = document.createElement("span");
  cuando.textContent = `${diaYMes(mensaje.creado_en)} ${hora(mensaje.creado_en)}`;
  firma.append(quien, cuando);

  item.append(burbuja, firma);
  return item;
}

function nodosDeMensajes(mensajes, diaPrevio) {
  /* Devuelve los <li> de esos mensajes, metiendo un separador cada vez que
     cambia el día. `diaPrevio` es el día del último mensaje ya pintado, para
     no repetir separador al agregar mensajes nuevos abajo. */
  const nodos = [];
  let dia = diaPrevio;
  mensajes.forEach((mensaje) => {
    const diaDelMensaje = claveDeDia(mensaje.creado_en);
    if (diaDelMensaje !== dia) {
      nodos.push(separadorDeDia(mensaje.creado_en));
      dia = diaDelMensaje;
    }
    nodos.push(elementoDeMensaje(mensaje));
  });
  return nodos;
}

function pintarHistorialCompleto() {
  elementos.mensajes.replaceChildren();
  if (estado.mensajes.length === 0) {
    const vacio = document.createElement("li");
    vacio.className = "estado-vacio";
    vacio.textContent = "Esta conversación todavía no tiene mensajes.";
    elementos.mensajes.append(vacio);
  } else {
    elementos.mensajes.append(...nodosDeMensajes(estado.mensajes, null));
  }
  elementos.cargarAnteriores.hidden = !estado.hayAnteriores;
}

function pintarEstadoDeConversacion(conversacion) {
  elementos.contacto.textContent = conversacion.identificador_externo;

  const partes = [conversacion.canal];
  if (conversacion.ultima_actividad) {
    partes.push(`última actividad ${diaYMes(conversacion.ultima_actividad)} ${hora(conversacion.ultima_actividad)}`);
  }
  elementos.contactoMeta.textContent = partes.join(" · ");

  elementos.estadoChip.className = conversacion.pausado ? "chip chip-pausado" : "chip chip-activo";
  elementos.estadoChip.textContent = conversacion.pausado ? "Bot pausado" : "Bot activo";

  elementos.reactivar.hidden = !conversacion.pausado;
  elementos.avisoPausa.hidden = !conversacion.pausado;

  if (!conversacion.pausado) {
    return;
  }

  elementos.avisoPausa.replaceChildren();
  const titulo = document.createElement("span");
  titulo.className = "titulo";
  titulo.textContent = "El bot no responde esta conversación";
  elementos.avisoPausa.append(titulo);

  const motivo = document.createElement("span");
  motivo.textContent =
    estado.motivos[conversacion.motivo_pausa] || "Pausada manualmente";
  elementos.avisoPausa.append(motivo);

  if (conversacion.escalada_en || conversacion.pausada_desde) {
    const desde = conversacion.escalada_en || conversacion.pausada_desde;
    const cuando = document.createElement("span");
    cuando.className = "detalle";
    cuando.textContent = `Desde el ${diaYMes(desde)} a las ${hora(desde)}`;
    elementos.avisoPausa.append(cuando);
  }

  if (conversacion.resumen_escalamiento) {
    const resumen = document.createElement("span");
    resumen.className = "resumen";
    resumen.textContent = conversacion.resumen_escalamiento;
    elementos.avisoPausa.append(resumen);
  }
}

async function elegir(id) {
  estado.elegidaId = id;
  estado.mensajes = [];
  elementos.avisoReactivado.hidden = true;
  elementos.sinSeleccion.hidden = true;
  elementos.detalle.hidden = false;
  elementos.layout.dataset.vista = "detalle";
  elementos.cargarAnteriores.hidden = true;

  const cargando = document.createElement("li");
  cargando.className = "estado-vacio";
  cargando.textContent = "Cargando el historial…";
  elementos.mensajes.replaceChildren(cargando);

  pintarLista();

  try {
    const datos = await api(`/crm/api/conversaciones/${id}/mensajes`);
    if (estado.elegidaId !== id) return; // cambiaron de conversación mientras cargaba
    estado.mensajes = datos.mensajes;
    estado.hayAnteriores = datos.hay_anteriores;
    pintarHistorialCompleto();
    pintarEstadoDeConversacion(datos.conversacion);
    irAbajoDeTodo();
    limpiarError();
  } catch (problema) {
    mostrarError(problema);
  }
}

async function cargarAnteriores() {
  if (estado.mensajes.length === 0) return;

  const primerId = estado.mensajes[0].id;
  elementos.cargarAnteriores.disabled = true;
  elementos.cargarAnteriores.textContent = "Cargando…";

  try {
    const datos = await api(
      `/crm/api/conversaciones/${estado.elegidaId}/mensajes?antes_de=${primerId}`
    );
    const alturaPrevia = elementos.hilo.scrollHeight;

    estado.mensajes = datos.mensajes.concat(estado.mensajes);
    estado.hayAnteriores = datos.hay_anteriores;
    pintarHistorialCompleto();

    // Quien estaba leyendo un mensaje viejo tiene que seguir viéndolo: se
    // compensa el alto que acaba de aparecer arriba.
    elementos.hilo.scrollTop += elementos.hilo.scrollHeight - alturaPrevia;
    limpiarError();
  } catch (problema) {
    mostrarError(problema);
  }

  elementos.cargarAnteriores.disabled = false;
  elementos.cargarAnteriores.textContent = "Cargar mensajes anteriores";
}

async function traerMensajesNuevos() {
  if (estado.elegidaId === null) return;

  const ultimo = estado.mensajes.length > 0 ? estado.mensajes[estado.mensajes.length - 1].id : 0;
  const datos = await api(`/crm/api/conversaciones/${estado.elegidaId}/mensajes?desde=${ultimo}`);

  pintarEstadoDeConversacion(datos.conversacion);

  if (datos.mensajes.length === 0) return;

  const estabaAbajo = estaAbajoDeTodo();
  const diaDelUltimo = estado.mensajes.length > 0
    ? claveDeDia(estado.mensajes[estado.mensajes.length - 1].creado_en)
    : null;

  if (estado.mensajes.length === 0) {
    elementos.mensajes.replaceChildren();
  }
  estado.mensajes = estado.mensajes.concat(datos.mensajes);
  elementos.mensajes.append(...nodosDeMensajes(datos.mensajes, diaDelUltimo));

  if (estabaAbajo) {
    irAbajoDeTodo();
  }
}

/* ---------- Exportar ---------- */

/* Descarga el historial completo (no solo lo que está pintado en pantalla,
 * ver app/crm/servicio.py:todos_los_mensajes) como un .md. Una navegación
 * normal alcanza: el servidor manda Content-Disposition: attachment, así que
 * el navegador dispara la descarga sin salir del panel — no hace falta fetch
 * ni armar un blob a mano. */
function exportarConversacion() {
  const id = estado.elegidaId;
  if (id === null) return;
  window.location.href = `/crm/api/conversaciones/${id}/exportar`;
}

/* ---------- Reactivar ---------- */

async function reactivar() {
  const id = estado.elegidaId;
  if (id === null) return;

  elementos.reactivar.disabled = true;
  elementos.reactivar.textContent = "Reactivando…";

  try {
    const datos = await api(`/crm/api/conversaciones/${id}/reactivar`, {
      method: "POST",
      headers: { "X-CRM-CSRF": estado.csrf },
    });
    pintarEstadoDeConversacion(datos.conversacion);
    elementos.avisoReactivado.textContent = datos.mensaje;
    elementos.avisoReactivado.hidden = false;
    await cargarLista();
    limpiarError();
  } catch (problema) {
    mostrarError(problema);
  }

  elementos.reactivar.disabled = false;
  elementos.reactivar.textContent = "Reactivar bot";
}

/* ---------- Refresco periódico ---------- */

async function refrescar() {
  // Con la pestaña en segundo plano no se pide nada: nadie lo está mirando.
  if (document.hidden) return;

  try {
    await cargarLista();
    await traerMensajesNuevos();
    limpiarError();
  } catch (problema) {
    mostrarError(problema);
  }
}

/* ---------- Arranque ---------- */

function conectarEventos() {
  document.querySelectorAll(".filtro").forEach((boton) => {
    boton.addEventListener("click", async () => {
      estado.filtro = boton.dataset.filtro;
      document.querySelectorAll(".filtro").forEach((otro) => {
        const activo = otro === boton;
        otro.classList.toggle("activo", activo);
        otro.setAttribute("aria-pressed", String(activo));
      });
      pintarEsqueleto();
      try {
        await cargarLista();
        limpiarError();
      } catch (problema) {
        mostrarError(problema);
      }
    });
  });

  elementos.cargarMas.addEventListener("click", async () => {
    estado.desplazamiento = estado.conversaciones.length;
    elementos.cargarMas.disabled = true;
    try {
      await cargarLista({ agregar: true });
      limpiarError();
    } catch (problema) {
      mostrarError(problema);
    }
    elementos.cargarMas.disabled = false;
  });

  elementos.cargarAnteriores.addEventListener("click", cargarAnteriores);
  elementos.exportar.addEventListener("click", exportarConversacion);
  elementos.reactivar.addEventListener("click", reactivar);
  elementos.volver.addEventListener("click", () => {
    elementos.layout.dataset.vista = "lista";
  });
  elementos.reintentar.addEventListener("click", refrescar);

  elementos.salir.addEventListener("click", salir);
}

/* Salir: un POST que revoca la sesión en la base y borra la cookie. Va con
 * el token CSRF porque un logout por GET lo podría disparar cualquier página
 * ajena con una imagen apuntada acá.
 *
 * Con Auth0 hacía falta un segundo paso (mandar el navegador al logout del
 * proveedor, que era una sesión aparte). Ahora la única sesión que existe es
 * la del panel, así que con esto alcanza.
 */
async function salir() {
  elementos.salir.disabled = true;
  elementos.salir.textContent = "Saliendo…";

  try {
    await api("/crm/api/logout", {
      method: "POST",
      headers: { "X-CRM-CSRF": estado.csrf },
    });
  } catch (problema) {
    // Si el POST falló, igual se manda a la pantalla de entrar: lo peor que
    // puede pasar es que la sesión siga viva y haya que reintentar.
    mostrarError(problema);
  }
  window.location.href = "/crm/login";
}

async function iniciar() {
  conectarEventos();
  pintarEsqueleto();

  try {
    const sesion = await api("/crm/api/sesion");
    estado.csrf = sesion.csrf;
    estado.zonaHoraria = sesion.zona_horaria;
    elementos.usuario.textContent = sesion.usuario;

    estado.motivos = await api("/crm/api/motivos");
    await cargarLista();
    limpiarError();
  } catch (problema) {
    mostrarError(problema);
  }

  window.setInterval(refrescar, SEGUNDOS_DE_REFRESCO * 1000);
}

iniciar();
