/* Pantalla de entrar.
 *
 * Manda usuario y contraseña a /crm/api/login como JSON y, si el servidor
 * dice que sí, va al panel. La cookie de sesión la pone el servidor en esa
 * misma respuesta: acá no se guarda nada, ni en localStorage ni en ningún
 * lado. La contraseña vive lo que dura el fetch.
 *
 * Todo lo que se muestra sale de textos de este archivo o del `detail` que
 * manda el servidor, pintado con textContent — nunca con innerHTML.
 */

const TEXTOS_DE_ERROR = {
  sesion: "Tu sesión venció. Volvé a entrar.",
  red: "No pudimos conectar con el servidor. Probá de nuevo.",
  generico: "No pudimos completar el ingreso. Probá de nuevo.",
};

const formulario = document.getElementById("formulario");
const campoUsuario = document.getElementById("usuario");
const campoPassword = document.getElementById("password");
const boton = document.getElementById("entrar");
const error = document.getElementById("error");

function mostrarError(texto) {
  error.textContent = texto;
  error.hidden = false;
}

function limpiarError() {
  error.hidden = true;
}

/* El `?error=sesion` con el que el panel manda acá cuando la sesión dejó de
 * valer mientras alguien lo estaba usando. Es el único motivo que viaja por
 * la URL, y se traduce por la tabla de arriba: nunca se pinta lo que venga
 * en la query, que lo elige quien arma el link. */
const motivo = new URLSearchParams(window.location.search).get("error");
if (motivo) {
  mostrarError(TEXTOS_DE_ERROR[motivo] || TEXTOS_DE_ERROR.generico);
}

formulario.addEventListener("submit", async (evento) => {
  evento.preventDefault();
  limpiarError();

  const usuario = campoUsuario.value.trim();
  const password = campoPassword.value;
  if (!usuario || !password) {
    mostrarError("Completá usuario y contraseña.");
    return;
  }

  boton.disabled = true;
  boton.textContent = "Entrando…";

  try {
    const respuesta = await fetch("/crm/api/login", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ usuario, password }),
    });

    if (respuesta.ok) {
      // La contraseña no queda en el campo mientras el navegador navega.
      campoPassword.value = "";
      window.location.href = "/crm";
      return;
    }

    const cuerpo = await respuesta.json().catch(() => ({}));
    mostrarError(cuerpo.detail || TEXTOS_DE_ERROR.generico);
  } catch (problema) {
    mostrarError(TEXTOS_DE_ERROR.red);
  }

  // Se vacía siempre después de un intento fallido: que no quede tipeada en
  // una pantalla que alguien deja abierta.
  campoPassword.value = "";
  campoPassword.focus();
  boton.disabled = false;
  boton.textContent = "Entrar";
});
