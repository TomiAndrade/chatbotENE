"""Corre a mano el checklist de validación de Postgres del README
("Postgres — notas y checklist manual antes de deployar") contra una base
real.

No se corre solo ni en CI: hace falta apuntar a una base de Postgres de
verdad (por ejemplo la de Render) y el checklist es manual a propósito,
porque son justo las diferencias que SQLite no reproduce.

Uso, desde chatbot-polo/:

    DATABASE_URL="postgresql://usuario:password@host:5432/nombre_db" python scripts/verificar_postgres.py

(en PowerShell: `$env:DATABASE_URL = "..."; python scripts/verificar_postgres.py`)

No hardcodea ninguna URL: la lee de la variable de entorno DATABASE_URL, la
misma que usa `app/config.py`. Corre los pasos en orden, pero uno que falla
no corta los siguientes — al final imprime un resumen con todo lo que pasó y
sale con código 1 si algo falló. Todo lo que inserta para probar lo borra al
final (en un `finally`, así que corre incluso si algo revienta antes).
"""

import os
import sys
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy.exc import IntegrityError  # noqa: E402

from app.config import config  # noqa: E402
from app.db import SessionLocal, crear_engine, init_db  # noqa: E402
from app.models import Conversacion, Mensaje, MotivoPausa, RolMensaje, ahora_utc  # noqa: E402

resultados = []  # (nombre, ok, detalle)


def registrar(nombre: str, ok: bool, detalle: str = "") -> None:
    resultados.append((nombre, ok, detalle))
    print(f"[{'OK' if ok else 'FALLA'}] {nombre}")
    if detalle:
        for linea in detalle.splitlines():
            print(f"       {linea}")


def paso_1_init_db() -> None:
    nombre = "1. init_db() crea las tablas sin error"
    try:
        init_db()
        registrar(nombre, True, f"conectado contra: {config.database_url.split('@')[-1]}")
    except Exception as e:
        registrar(nombre, False, f"esperado: sin excepción | obtenido: {type(e).__name__}: {e}")


def paso_2_guardar_conversacion_y_mensaje(db, run_id: str):
    nombre = "2. Guardar una conversación (con modo_humano_desde) y un mensaje"
    canal_prueba = f"verificacion-{run_id}"
    try:
        conversacion = Conversacion(
            canal=canal_prueba,
            identificador_externo=f"tel-verificacion-{run_id}",
            modo_humano=True,
            motivo_pausa=MotivoPausa.ESCALAMIENTO,
            modo_humano_desde=ahora_utc(),
            resumen_escalamiento="Conversación de prueba generada por scripts/verificar_postgres.py",
            escalada_en=ahora_utc(),
        )
        db.add(conversacion)
        db.commit()
        db.refresh(conversacion)

        mensaje = Mensaje(
            conversacion_id=conversacion.id,
            rol=RolMensaje.USUARIO,
            contenido="Mensaje de prueba generado por scripts/verificar_postgres.py",
            wa_message_id=f"wamid-verificacion-{run_id}-1",
        )
        db.add(mensaje)
        db.commit()
        db.refresh(mensaje)

        registrar(nombre, True, f"conversacion.id={conversacion.id}, mensaje.id={mensaje.id}")
        return canal_prueba, conversacion, mensaje
    except Exception as e:
        db.rollback()
        registrar(nombre, False, f"esperado: sin excepción | obtenido: {type(e).__name__}: {e}")
        return canal_prueba, None, None


def paso_3_tzinfo(db, conversacion, mensaje) -> None:
    nombre = "3. Las 5 columnas DateTime(timezone=True) vuelven con tzinfo no nulo"
    if conversacion is None or mensaje is None:
        registrar(nombre, False, "omitido: depende del paso 2, que falló")
        return

    try:
        db.expire_all()
        conversacion_releida = db.query(Conversacion).filter_by(id=conversacion.id).one()
        mensaje_releido = db.query(Mensaje).filter_by(id=mensaje.id).one()

        columnas = {
            "Conversacion.creada_en": conversacion_releida.creada_en,
            "Conversacion.ultimo_mensaje_en": conversacion_releida.ultimo_mensaje_en,
            "Conversacion.modo_humano_desde": conversacion_releida.modo_humano_desde,
            "Conversacion.escalada_en": conversacion_releida.escalada_en,
            "Mensaje.creado_en": mensaje_releido.creado_en,
        }
        sin_tzinfo = [nombre_col for nombre_col, valor in columnas.items() if valor is None or valor.tzinfo is None]

        if not sin_tzinfo:
            detalle = "\n".join(f"{k} = {v!r}" for k, v in columnas.items())
            registrar(nombre, True, f"esperado: tzinfo no nulo en las 5 | obtenido:\n{detalle}")
        else:
            detalle = "\n".join(f"{k} = {v!r}" for k, v in columnas.items())
            registrar(
                nombre,
                False,
                f"esperado: tzinfo no nulo en las 5 | sin tzinfo: {sin_tzinfo}\nvalores:\n{detalle}",
            )
    except Exception as e:
        db.rollback()
        registrar(nombre, False, f"esperado: sin excepción | obtenido: {type(e).__name__}: {e}")


def paso_4_enums(db, canal_prueba: str, conversacion, run_id: str) -> None:
    nombre = "4. Insertar cada valor de RolMensaje y motivo_pausa sin error"
    if conversacion is None:
        registrar(nombre, False, "omitido: depende del paso 2, que falló")
        return

    fallidos = []
    probados = []

    for rol in RolMensaje:
        try:
            m = Mensaje(
                conversacion_id=conversacion.id,
                rol=rol,
                contenido=f"Prueba de enum RolMensaje={rol.value}",
                wa_message_id=f"wamid-verificacion-{run_id}-rol-{rol.value}",
            )
            db.add(m)
            db.commit()
            probados.append(f"RolMensaje.{rol.name}")
        except Exception as e:
            db.rollback()
            fallidos.append(f"RolMensaje.{rol.name}: {type(e).__name__}: {e}")

    for motivo in MotivoPausa:
        try:
            c = Conversacion(
                canal=canal_prueba,
                identificador_externo=f"tel-verificacion-{run_id}-motivo-{motivo.value}",
                modo_humano=True,
                motivo_pausa=motivo,
                modo_humano_desde=ahora_utc(),
            )
            db.add(c)
            db.commit()
            probados.append(f"MotivoPausa.{motivo.name}")
        except Exception as e:
            db.rollback()
            fallidos.append(f"MotivoPausa.{motivo.name}: {type(e).__name__}: {e}")

    if not fallidos:
        registrar(nombre, True, f"valores insertados sin error: {', '.join(probados)}")
    else:
        registrar(
            nombre,
            False,
            "esperado: los 5 valores (3 de RolMensaje + 2 de MotivoPausa) sin error\n"
            + "\n".join(fallidos),
        )


def paso_5_integrity_error(db, conversacion, mensaje) -> None:
    nombre = "5. Duplicar wa_message_id -> IntegrityError, y el rollback deja la sesión usable"
    if conversacion is None or mensaje is None:
        registrar(nombre, False, "omitido: depende del paso 2, que falló")
        return

    try:
        duplicado = Mensaje(
            conversacion_id=conversacion.id,
            rol=RolMensaje.USUARIO,
            contenido="Duplicado deliberado para probar IntegrityError",
            wa_message_id=mensaje.wa_message_id,
        )
        db.add(duplicado)
        db.commit()
    except IntegrityError:
        db.rollback()
        try:
            conteo = db.query(Mensaje).filter_by(conversacion_id=conversacion.id).count()
            registrar(
                nombre,
                True,
                f"IntegrityError capturado, rollback hecho, sesión usable "
                f"(SELECT count posterior devolvió {conteo})",
            )
        except Exception as e:
            registrar(
                nombre,
                False,
                f"esperado: sesión usable tras el rollback | obtenido al re-consultar: "
                f"{type(e).__name__}: {e}",
            )
    except Exception as e:
        db.rollback()
        registrar(
            nombre,
            False,
            f"esperado: IntegrityError al insertar wa_message_id duplicado | "
            f"obtenido: {type(e).__name__}: {e}",
        )
    else:
        registrar(
            nombre,
            False,
            "esperado: IntegrityError al insertar wa_message_id duplicado | obtenido: el insert no falló",
        )


def limpiar(run_id: str) -> int:
    """Borra todo lo que este run haya insertado, identificado por el canal
    'verificacion-<run_id>' que ningún dato real usa. Corre con una sesión
    propia para no depender del estado en el que haya quedado la de los
    pasos (puede estar en medio de una transacción rota)."""
    db = SessionLocal()
    try:
        conversaciones = db.query(Conversacion).filter(Conversacion.canal == f"verificacion-{run_id}").all()
        ids = [c.id for c in conversaciones]
        if ids:
            db.query(Mensaje).filter(Mensaje.conversacion_id.in_(ids)).delete(synchronize_session=False)
            db.query(Conversacion).filter(Conversacion.id.in_(ids)).delete(synchronize_session=False)
            db.commit()
        return len(ids)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def main() -> int:
    if "DATABASE_URL" not in os.environ:
        print("FALTA DATABASE_URL en el entorno. Ejemplo de uso:\n")
        print('  DATABASE_URL="postgresql://usuario:password@host:5432/nombre_db" python scripts/verificar_postgres.py')
        return 1

    raw = config.database_url
    if not raw.startswith("postgresql://"):
        # Se muestra solo el esquema, nunca la URL entera: trae usuario y
        # contraseña. Mismo criterio que validar_config() en
        # app/validacion_config.py.
        esquema = raw.split("://", 1)[0] if "://" in raw else "(sin '://')"
        print(
            f"DATABASE_URL no parece apuntar a Postgres (esquema: {esquema!r}). "
            "Este script valida específicamente el checklist de Postgres del README, "
            "no tiene sentido correrlo contra SQLite."
        )
        return 1

    run_id = uuid4().hex[:10]
    print(f"Verificando contra: {config.database_url.split('@')[-1]} (run_id={run_id})\n")

    # app/db.py ya no crea el engine al importar (ver
    # spec-validacion-config-arranque.md): hay que pedirlo explícito antes
    # de init_db() y de cualquier SessionLocal().
    crear_engine()

    paso_1_init_db()

    db = SessionLocal()
    canal_prueba, conversacion, mensaje = paso_2_guardar_conversacion_y_mensaje(db, run_id)
    paso_3_tzinfo(db, conversacion, mensaje)
    paso_4_enums(db, canal_prueba, conversacion, run_id)
    paso_5_integrity_error(db, conversacion, mensaje)
    db.close()

    print()
    try:
        borrados = limpiar(run_id)
        print(f"Limpieza: {borrados} conversación(es) de prueba y sus mensajes borrados.")
    except Exception as e:
        print(
            f"OJO: la limpieza falló ({type(e).__name__}: {e}). "
            f"Quedaron datos de prueba con canal='verificacion-{run_id}' — borrarlos a mano."
        )

    print("\nResumen:")
    ok_total = True
    for nombre, ok, _ in resultados:
        print(f"  [{'OK' if ok else 'FALLA'}] {nombre}")
        ok_total = ok_total and ok

    return 0 if ok_total else 1


if __name__ == "__main__":
    sys.exit(main())
