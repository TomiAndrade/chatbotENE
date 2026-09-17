"""CRM de conversaciones: el panel donde el equipo de ENE lee lo que el bot
viene conversando y puede reactivarlo cuando quedó pausado.

Un módulo por responsabilidad:

- `passwords.py`: cómo se hashea y se verifica una contraseña (Argon2id).
- `usuarios.py`: las cuentas del panel (crear, cambiar contraseña, desactivar).
- `intentos.py`: el límite de intentos de ingreso, contado contra la base.
- `sesiones.py`: crear, leer y revocar sesiones.
- `auth.py`: la cookie de sesión y el CSRF de las acciones que escriben.
- `servicio.py`: qué se muestra (consultas a la base, con SQLAlchemy).
- `modelos.py`: las tablas propias del panel.
- `rutas.py`: el router de FastAPI que une todo y sirve las páginas.

El CRM solo lee la base y, en un único endpoint, apaga la pausa de una
conversación. No manda mensajes por WhatsApp ni toca el flujo del webhook.
"""
