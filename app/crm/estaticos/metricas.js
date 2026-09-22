/* Dashboard de costos y actividad (specs/spec-dashboard-metricas.md).
 *
 * Mismo patrón de acceso al servidor que panel.js (api()): 401 manda al
 * login, cualquier otro error no-2xx tira con el detail del servidor. No
 * hay nada de contenido de mensajes ni teléfonos acá — todo lo que llega de
 * /crm/api/metricas son números agregados.
 */

const ERROR_SIN_MENSAJE = "Algo falló al cargar. Probá de nuevo.";
const ERROR_DE_CONEXION =
  "No pudimos conectar con el servidor. Puede ser tu conexión, o que el servidor esté caído.";

const ND = "N/D";

const estado = { csrf: null };

const elementos = {
  errorGlobal: document.getElementById("error-global"),
  errorGlobalTexto: document.getElementById("error-global-texto"),
  reintentar: document.getElementById("reintentar"),
  usuario: document.getElementById("usuario-actual"),
  salir: document.getElementById("salir"),
  formulario: document.getElementById("filtro-rango"),
  desde: document.getElementById("desde"),
  hasta: document.getElementById("hasta"),
  cargando: document.getElementById("cargando"),
  contenido: document.getElementById("contenido"),
  tablaCuerpo: document.getElementById("tabla-proveedores-cuerpo"),
};

async function api(url, opciones = {}) {
  let respuesta;
  try {
    respuesta = await fetch(url, { credentials: "same-origin", ...opciones });
  } catch (problema) {
    throw new Error(ERROR_DE_CONEXION);
  }
  if (respuesta.status === 401) {
    window.location.href = "/crm/login?error=sesion";
    throw new Error("Sesión vencida");
  }
  if (!respuesta.ok) {
    const cuerpo = await respuesta.json().catch(() => ({}));
    throw new Error(cuerpo.detail || `El servidor respondió con un error (${respuesta.status}). Probá de nuevo.`);
  }
  return respuesta.json();
}

function mostrarError(problema) {
  elementos.errorGlobalTexto.textContent = problema.message || ERROR_SIN_MENSAJE;
  elementos.errorGlobal.hidden = false;
}

function limpiarError() {
  elementos.errorGlobalTexto.textContent = "";
  elementos.errorGlobal.hidden = true;
}

/* ---------- Formato ---------- */

const numeroEntero = new Intl.NumberFormat("es-AR");
const numeroDecimal = new Intl.NumberFormat("es-AR", { maximumFractionDigits: 1 });
const numeroMoneda = new Intl.NumberFormat("es-AR", { style: "currency", currency: "USD", maximumFractionDigits: 4 });
const numeroMonedaArs = new Intl.NumberFormat("es-AR", { style: "currency", currency: "ARS", maximumFractionDigits: 2 });

function entero(valor) {
  return valor === null || valor === undefined ? ND : numeroEntero.format(valor);
}

function porcentaje(valor) {
  return valor === null || valor === undefined ? ND : `${numeroDecimal.format(valor * 100)}%`;
}

function duracion(ms) {
  if (ms === null || ms === undefined) return ND;
  return ms >= 1000 ? `${numeroDecimal.format(ms / 1000)} s` : `${numeroEntero.format(Math.round(ms))} ms`;
}

function moneda(valor) {
  return valor === null || valor === undefined ? ND : numeroMoneda.format(valor);
}

function monedaArs(valor) {
  return valor === null || valor === undefined ? ND : numeroMonedaArs.format(valor);
}

/* ---------- Pintado ---------- */

function pintarTarjetas(datos) {
  document.getElementById("m-conversaciones-actividad").textContent = entero(datos.conversaciones.con_actividad);
  document.getElementById("m-conversaciones-nuevas").textContent = entero(datos.conversaciones.nuevas);
  document.getElementById("m-mensajes-entrantes").textContent = entero(datos.mensajes.entrantes);
  document.getElementById("m-mensajes-bot").textContent = entero(datos.mensajes.bot);
  document.getElementById("m-mensajes-humano").textContent = entero(datos.mensajes.humano);
  document.getElementById("m-mensajes-por-conversacion").textContent =
    datos.mensajes_por_conversacion === null ? ND : numeroDecimal.format(datos.mensajes_por_conversacion);

  document.getElementById("m-activas").textContent = entero(datos.estado_actual.activas);
  document.getElementById("m-pausadas").textContent = entero(datos.estado_actual.pausadas);

  document.getElementById("m-llamadas").textContent = entero(datos.modelo.llamadas);
  document.getElementById("m-errores").textContent = entero(datos.modelo.errores);
  document.getElementById("m-escaladas").textContent = entero(datos.modelo.escaladas);
  document.getElementById("m-tasa-escalamiento").textContent = porcentaje(datos.modelo.tasa_escalamiento);
  document.getElementById("m-tiempo-medio").textContent = duracion(datos.modelo.duracion_ms_promedio);

  const costoTexto = moneda(datos.modelo.costo_estimado_usd_total);
  document.getElementById("m-costo-total").textContent =
    datos.modelo.costo_estimado_incompleto && costoTexto !== ND ? `${costoTexto} (parcial)` : costoTexto;
}

function pintarWhatsappMeta(datos) {
  document.getElementById("m-meta-mensajes").textContent = entero(datos.mensajes_contabilizados);
  document.getElementById("m-meta-costo").textContent = monedaArs(datos.costo_estimado_ars);
  document.getElementById("m-meta-presupuesto").textContent = monedaArs(datos.presupuesto_mensual_ars);
  document.getElementById("m-meta-porcentaje").textContent = porcentaje(datos.porcentaje_utilizado);
  document.getElementById("m-meta-saldo").textContent = monedaArs(datos.saldo_estimado_restante_ars);

  // Clampeado a [0, 100]: pasado el 100% del presupuesto la barra se ve
  // llena, no se desborda ni corta el layout. Sin colores de alerta a
  // propósito (ver crm.css) — esta etapa todavía no define esos umbrales.
  // Se anima con scaleX (transform), no con width — ver el comentario de
  // .barra-progreso-relleno en crm.css.
  const porcentajeCien = datos.porcentaje_utilizado === null ? 0 : datos.porcentaje_utilizado * 100;
  const anchoBarra = Math.max(0, Math.min(100, porcentajeCien));
  const barra = document.getElementById("meta-barra");
  const relleno = document.getElementById("meta-barra-relleno");
  relleno.style.transform = `scaleX(${anchoBarra / 100})`;
  barra.setAttribute("aria-valuenow", Math.round(anchoBarra));
}

function filaDeProveedor(fila) {
  const tr = document.createElement("tr");
  const celdas = [
    fila.proveedor,
    fila.modelo || ND,
    entero(fila.llamadas),
    entero(fila.tokens_entrada),
    entero(fila.tokens_salida),
    moneda(fila.costo_estimado_usd),
  ];
  celdas.forEach((valor) => {
    const td = document.createElement("td");
    td.textContent = valor;
    tr.append(td);
  });
  return tr;
}

function pintarTabla(porProveedorModelo) {
  elementos.tablaCuerpo.replaceChildren();
  if (porProveedorModelo.length === 0) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 6;
    td.className = "estado-vacio";
    td.textContent = "Sin llamadas al modelo en este rango.";
    tr.append(td);
    elementos.tablaCuerpo.append(tr);
    return;
  }
  porProveedorModelo.forEach((fila) => elementos.tablaCuerpo.append(filaDeProveedor(fila)));
}

/* ---------- Carga ---------- */

function parametrosDeRango() {
  const parametros = new URLSearchParams();
  if (elementos.desde.value) parametros.set("desde", elementos.desde.value);
  if (elementos.hasta.value) parametros.set("hasta", elementos.hasta.value);
  return parametros;
}

async function cargar() {
  elementos.cargando.hidden = false;
  elementos.contenido.hidden = true;
  try {
    const datos = await api(`/crm/api/metricas?${parametrosDeRango()}`);
    pintarTarjetas(datos);
    pintarTabla(datos.modelo.por_proveedor_modelo);
    pintarWhatsappMeta(datos.whatsapp_meta);
    elementos.cargando.hidden = true;
    elementos.contenido.hidden = false;
    limpiarError();
  } catch (problema) {
    elementos.cargando.hidden = true;
    mostrarError(problema);
  }
}

async function salir() {
  elementos.salir.disabled = true;
  elementos.salir.textContent = "Saliendo…";
  try {
    await api("/crm/api/logout", { method: "POST", headers: { "X-CRM-CSRF": estado.csrf } });
  } catch (problema) {
    mostrarError(problema);
  }
  window.location.href = "/crm/login";
}

async function iniciar() {
  elementos.formulario.addEventListener("submit", (evento) => {
    evento.preventDefault();
    cargar();
  });
  elementos.reintentar.addEventListener("click", cargar);
  elementos.salir.addEventListener("click", salir);

  try {
    const sesion = await api("/crm/api/sesion");
    estado.csrf = sesion.csrf;
    elementos.usuario.textContent = sesion.usuario;
    await cargar();
  } catch (problema) {
    mostrarError(problema);
  }
}

iniciar();
