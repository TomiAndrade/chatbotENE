"""Atención humana sobre una conversación: abrir, tomar y resolver.

Es el único módulo que escribe en `atenciones` (ver `Atencion` en
app/models.py). Vive fuera del CRM, igual que app/pausa.py, porque lo usan
los dos lados: el flujo del bot (`escalar_a_humano` en app/main.py abre una
atención al derivar) y las rutas del CRM (iniciar, tomar, resolver).

La regla que sostiene todo: **mientras haya una atención abierta (pendiente o
en atención), el bot está pausado con una pausa que no vence.** Por eso abrir
una atención prende la pausa en el mismo commit, y resolverla la levanta en
el mismo commit. Resolver no manda nada por WhatsApp: el bot vuelve recién
con el próximo mensaje entrante.
"""

from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.agrupamiento import avanzar_hasta_el_ultimo_entrante
from app.models import Atencion, Conversacion, EstadoAtencion, MotivoAtencion, MotivoPausa
from app.pausa import reactivar_bot

_ESTADOS_ABIERTOS = (EstadoAtencion.PENDIENTE.value, EstadoAtencion.EN_ATENCION.value)


class AtencionYaAbierta(Exception):
    """La conversación ya tiene una atención abierta."""


class AtencionNoDisponible(Exception):
    """No se puede tomar, resolver ni responder: cambió o ya se resolvió."""


class AtencionSinTomar(AtencionNoDisponible):
    """Se intentó resolver o responder una pendiente: primero hay que tomarla."""


class AtencionAjena(AtencionNoDisponible):
    """Se intentó resolver o responder una atención que tiene otra persona."""


def atencion_abierta(db: Session, conversacion_id: int) -> Atencion | None:
    """La atención pendiente o en atención de la conversación, si hay una.
    Nunca hay más de una (ver el índice único parcial de `Atencion`)."""
    return (
        db.query(Atencion)
        .filter(Atencion.conversacion_id == conversacion_id, Atencion.estado.in_(_ESTADOS_ABIERTOS))
        .first()
    )


def _validar_responsable_para_responder(atencion: Atencion | None, usuario_id: int) -> Atencion:
    """Aplica las reglas de autorización de una respuesta humana.

    Se comparte entre la validación rápida y la revalidación bloqueante para
    que los dos pasos no puedan divergir. Una atención resuelta no aparece en
    la consulta de abiertas y se trata como no disponible.
    """
    if atencion is None:
        raise AtencionNoDisponible()
    if atencion.estado == EstadoAtencion.PENDIENTE.value:
        raise AtencionSinTomar()
    if atencion.estado != EstadoAtencion.EN_ATENCION.value:
        raise AtencionNoDisponible()
    if atencion.responsable_id != usuario_id:
        raise AtencionAjena()
    return atencion


def validar_para_responder(db: Session, conversacion_id: int, usuario_id: int) -> Atencion:
    """Validación rápida antes de intentar reservar presupuesto.

    No es la barrera de concurrencia: quien envía tiene que llamar además a
    `bloquear_para_responder` después de cualquier commit intermedio y justo
    antes de Meta.
    """
    return _validar_responsable_para_responder(atencion_abierta(db, conversacion_id), usuario_id)


def bloquear_para_responder(db: Session, conversacion_id: int, usuario_id: int) -> Atencion:
    """Revalida la atención y bloquea su fila hasta el commit del envío.

    Tiene que ejecutarse después de `reservar_gasto`, que hace commit y por
    lo tanto liberaría cualquier bloqueo anterior. `populate_existing` evita
    confiar en la instancia que la validación rápida pudo dejar en el mapa de
    identidad de SQLAlchemy: el estado se vuelve a leer realmente de la base.

    En Postgres, `FOR UPDATE` hace que `resolver` espere si el envío obtuvo el
    bloqueo primero. Si `resolver` ganó antes, esta consulta ya no encuentra
    una atención abierta válida y no se llega a llamar a Meta. SQLite ignora
    `FOR UPDATE`; la exclusión real queda pendiente de validación contra
    Postgres antes de producción.
    """
    abierta = (
        db.query(Atencion)
        .filter(
            Atencion.conversacion_id == conversacion_id,
            Atencion.estado.in_(_ESTADOS_ABIERTOS),
        )
        .populate_existing()
        .with_for_update()
        .first()
    )
    return _validar_responsable_para_responder(abierta, usuario_id)


def abrir_por_escalamiento(db: Session, conversacion: Conversacion, resumen: str | None) -> Atencion | None:
    """Abre una atención pendiente porque el bot derivó. No commitea: la
    llama `escalar_a_humano` justo antes del commit que prende la pausa, así
    las dos cosas quedan juntas o no queda ninguna.

    Si ya hay una atención abierta no crea otra. Devuelve None en ese caso.
    """
    if atencion_abierta(db, conversacion.id) is not None:
        return None

    atencion = Atencion(
        conversacion_id=conversacion.id,
        estado=EstadoAtencion.PENDIENTE.value,
        motivo=MotivoAtencion.ESCALAMIENTO.value,
        resumen=resumen,
    )
    db.add(atencion)
    return atencion


def iniciar_desde_crm(db: Session, conversacion: Conversacion, usuario_id: int) -> Atencion:
    """Alguien del equipo abre una atención desde la vista general. Queda
    pendiente y sin responsable (se asigna al tomarla), pero **el bot se
    pausa en el acto**.

    Si la conversación ya estaba pausada por un escalamiento del bot (por
    ejemplo, uno anterior a que existieran las atenciones), la atención
    hereda ese motivo y el resumen, y la pausa se deja como está: las dos
    no vencen. Cualquier otro estado (sin pausa, o con una pausa manual que
    vence a las dos horas) pasa a `ATENCION_CRM`, que no vence.

    Levanta `AtencionYaAbierta` si ya hay una abierta. Si dos personas
    inician a la vez, el índice único parcial de `Atencion` frena a la
    segunda y el resultado es el mismo.
    """
    db.refresh(conversacion)
    if atencion_abierta(db, conversacion.id) is not None:
        raise AtencionYaAbierta()

    ahora = datetime.now(timezone.utc)
    ya_escalada = conversacion.modo_humano and conversacion.motivo_pausa == MotivoPausa.ESCALAMIENTO

    atencion = Atencion(
        conversacion_id=conversacion.id,
        estado=EstadoAtencion.PENDIENTE.value,
        motivo=(MotivoAtencion.ESCALAMIENTO if ya_escalada else MotivoAtencion.SIN_CLASIFICAR).value,
        resumen=conversacion.resumen_escalamiento if ya_escalada else None,
        iniciada_por_id=usuario_id,
        creada_en=ahora,
    )
    db.add(atencion)

    if not ya_escalada:
        conversacion.modo_humano = True
        conversacion.motivo_pausa = MotivoPausa.ATENCION_CRM
        conversacion.modo_humano_desde = ahora

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise AtencionYaAbierta()

    db.refresh(atencion)
    return atencion


def tomar(db: Session, atencion: Atencion, usuario_id: int) -> Atencion:
    """Asigna la atención a `usuario_id` y la pasa a "en atención".

    **Atómico**: un único `UPDATE` condicionado a que siga pendiente y sin
    responsable. Si dos personas toman a la vez, el motor serializa las dos
    sentencias y solo una afecta la fila (`rowcount == 1`). Mismo idioma que
    `_reclamar_generacion` en app/main.py y `reservar_gasto` en
    app/costo_meta.py.

    Tomar de nuevo una atención que ya es tuya no es un error: devuelve la
    misma atención (por ejemplo, un doble click).

    Levanta `AtencionNoDisponible` si la tiene otra persona o ya se resolvió.
    """
    filas_afectadas = (
        db.query(Atencion)
        .filter(
            Atencion.id == atencion.id,
            Atencion.estado == EstadoAtencion.PENDIENTE.value,
            Atencion.responsable_id.is_(None),
        )
        .update(
            {
                Atencion.estado: EstadoAtencion.EN_ATENCION.value,
                Atencion.responsable_id: usuario_id,
                Atencion.tomada_en: datetime.now(timezone.utc),
            },
            synchronize_session=False,
        )
    )
    db.commit()
    db.refresh(atencion)

    if filas_afectadas == 1:
        return atencion
    if atencion.estado == EstadoAtencion.EN_ATENCION.value and atencion.responsable_id == usuario_id:
        return atencion
    raise AtencionNoDisponible()


def resolver(db: Session, atencion: Atencion, usuario_id: int) -> Atencion:
    """Cierra la atención y levanta la pausa del bot, en el mismo commit.

    **Solo puede resolverla su responsable**: la atención tiene que estar en
    atención y tomada por `usuario_id`. Una pendiente hay que tomarla primero
    (`AtencionSinTomar`), y una que tiene otra persona no se toca
    (`AtencionAjena`). No hay roles ni override: si alguien se va y deja una
    atención tomada, hoy la única salida es scripts/resetear_modo_humano.py.

    El motivo y el resumen quedan en la fila de la atención, aunque
    `reactivar_bot` limpie los de la conversación. No manda nada por
    WhatsApp: el bot contesta recién el próximo mensaje entrante.

    La condición va entera en un solo `UPDATE`: si entre la lectura y la
    escritura cambió algo (otra persona la resolvió o la tomó), no afecta
    ninguna fila y la pausa no se toca.

    **Todo mensaje del usuario recibido mientras la atención estuvo abierta
    le pertenece a esa atención, no al bot**: `avanzar_hasta_el_ultimo_
    entrante` (app/agrupamiento.py) mueve la marca de agrupado hasta el
    último mensaje de texto que exista en este momento, en el mismo commit
    que reactiva el bot. Sin esto, esos mensajes quedarían pendientes y el
    bot terminaría respondiendo algo que ya se le contestó a mano — o, si el
    proceso se reinicia antes de que llegue un mensaje nuevo, respondiendo
    sin que haya entrado nada. El próximo mensaje que llegue después de este
    commit sí es del bot, como siempre.

    Levanta `AtencionNoDisponible` (o una de sus dos subclases) si no se
    pudo resolver.
    """
    filas_afectadas = (
        db.query(Atencion)
        .filter(
            Atencion.id == atencion.id,
            Atencion.estado == EstadoAtencion.EN_ATENCION.value,
            Atencion.responsable_id == usuario_id,
        )
        .update(
            {
                Atencion.estado: EstadoAtencion.RESUELTA.value,
                Atencion.resuelta_en: datetime.now(timezone.utc),
                Atencion.resuelta_por_id: usuario_id,
            },
            synchronize_session=False,
        )
    )
    if filas_afectadas != 1:
        db.rollback()
        db.refresh(atencion)
        if atencion.estado == EstadoAtencion.PENDIENTE.value:
            raise AtencionSinTomar()
        if atencion.estado == EstadoAtencion.EN_ATENCION.value:
            raise AtencionAjena()
        raise AtencionNoDisponible()

    conversacion = db.query(Conversacion).filter(Conversacion.id == atencion.conversacion_id).one()
    reactivar_bot(conversacion)
    avanzar_hasta_el_ultimo_entrante(db, conversacion)
    db.commit()
    db.refresh(atencion)
    return atencion


def resolver_si_hay_abierta(db: Session, conversacion: Conversacion, usuario_id: int | None) -> None:
    """Solo para scripts/resetear_modo_humano.py, que reactiva el bot desde
    la consola sin pasar por "Resolver". Si quedara una atención abierta con
    el bot ya activo, el panel mostraría "en atención" en una conversación
    que el bot está contestando.

    **No aplica la regla de `resolver`** (solo el responsable): la consola
    necesita acceso a la base, no una cuenta del panel, y es la única salida
    si alguien deja una atención tomada y no vuelve. El panel no la usa:
    "Reactivar bot" pasa por `resolver`.

    No commitea y no toca la pausa: eso lo hace quien llama, con
    `reactivar_bot`, en el mismo commit. Sí avanza la marca de agrupado
    (`avanzar_hasta_el_ultimo_entrante`) cuando de verdad había una atención
    abierta para cerrar — misma regla que `resolver`, y por el mismo motivo:
    quien llama también reactiva el bot en el mismo commit. Si no había
    ninguna atención abierta, no se toca la marca: no hay nada que la
    justifique.
    """
    ahora = datetime.now(timezone.utc)
    filas_afectadas = (
        db.query(Atencion)
        .filter(
            Atencion.conversacion_id == conversacion.id,
            Atencion.estado.in_(_ESTADOS_ABIERTOS),
        )
        .update(
            {
                Atencion.estado: EstadoAtencion.RESUELTA.value,
                Atencion.resuelta_en: ahora,
                Atencion.resuelta_por_id: usuario_id,
            },
            synchronize_session=False,
        )
    )
    if filas_afectadas:
        avanzar_hasta_el_ultimo_entrante(db, conversacion)
