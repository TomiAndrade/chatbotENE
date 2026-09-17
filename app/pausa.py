"""Vigencia de la pausa del bot sobre una conversación.

Vive en su propio módulo, y no en `app/main.py` donde nació, porque ahora lo
necesitan dos lados que no se pueden importar entre sí: el flujo del webhook
(`app/main.py`) y el CRM (`app/crm/`, que muestra el estado de cada
conversación). `app/main.py` importa el router del CRM, así que el CRM no
puede importar `app/main.py` de vuelta.

Es un traslado, no un rediseño: la función es la misma que estaba en
`app/main.py` como `_pausa_vigente`.
"""

from datetime import datetime, timedelta, timezone

from app.config import config
from app.models import Conversacion, MotivoPausa


def pausa_vigente(conversacion: Conversacion, ahora: datetime) -> bool:
    """Si `conversacion` está en modo_humano *ahora mismo*, contemplando que
    la pausa por intervención manual expira.

    Quién decide si expira es `motivo_pausa`, no si `modo_humano_desde` tiene
    valor: solo INTERVENCION_MANUAL expira, a los `PAUSA_HUMANA_MINUTOS` de
    la última vez que la secretaría respondió (se reinicia con cada mensaje
    nuevo, ver `registrar_intervencion_humana`). ESCALAMIENTO no expira
    nunca, se desmarca a mano (o desde el CRM, con "Reactivar bot").

    Un `motivo_pausa` en None con `modo_humano` prendido no debería pasar
    (dato viejo, o alguien puso `modo_humano = 1` a mano por SQL sin
    especificar el motivo) — se trata como si no expirara: errar hacia
    "sigue pausado" es más seguro que arriesgarse a que el bot le escriba
    encima a alguien.

    El guard de `tzinfo is None` de acá abajo es para SQLite: devuelve los
    DateTime(timezone=True) sin tzinfo aunque se hayan guardado en UTC (se
    probó a mano: el round-trip pierde el offset). Todo lo que este proyecto
    guarda en esas columnas es `datetime.now(timezone.utc)` o equivalente,
    así que un valor naive acá se interpreta como UTC. Contra Postgres las
    fechas ya vuelven aware (`timestamptz`) y el guard no se dispara — sigue
    ahí porque el mismo código corre contra los dos motores.
    """
    if not conversacion.modo_humano:
        return False
    if conversacion.motivo_pausa != MotivoPausa.INTERVENCION_MANUAL:
        return True

    desde = conversacion.modo_humano_desde
    if desde is None:
        return True
    if desde.tzinfo is None:
        desde = desde.replace(tzinfo=timezone.utc)
    return ahora - desde <= timedelta(minutes=config.pausa_humana_minutos)


def reactivar_bot(conversacion: Conversacion) -> None:
    """Saca la pausa: el bot vuelve a responderle a esa conversación cuando
    llegue un mensaje nuevo. No commitea — lo hace quien llama.

    Limpia los cinco campos juntos, incluidos `resumen_escalamiento` y
    `escalada_en`: son el detalle de *esta* pausa, y dejarlos con una pausa
    ya levantada haría que el panel siguiera mostrando el motivo de algo que
    ya no está pasando. Es exactamente lo que venía haciendo
    `scripts/resetear_modo_humano.py`, que ahora llama acá en vez de repetir
    los cinco campos.

    No manda ningún mensaje ni reprocesa nada: los mensajes viejos quedan
    como están y el bot responde recién con el próximo mensaje entrante,
    sujeto a las reglas de siempre (límite por hora, historial, etc.).
    """
    conversacion.modo_humano = False
    conversacion.motivo_pausa = None
    conversacion.modo_humano_desde = None
    conversacion.resumen_escalamiento = None
    conversacion.escalada_en = None
