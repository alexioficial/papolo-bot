"""
Registro de turnos del agente en vuelo, por conversacion.

El bot crea un `Agent` nuevo por turno (get_or_create_agent carga el estado de la
DB), asi que el comando /papolo-stop corre en otra interaccion y no tiene la
referencia al agente que esta ejecutando. Este registro cierra ese gap: mientras
un turno corre, su Agent queda registrado bajo el uuid de la conversacion; el
comando lo busca y le pide cancelar.

La cancelacion es cooperativa (Agent.cancel() setea una flag que el loop de
send() chequea entre rondas de tool calls). No mata tools ya en vuelo.
"""

import threading

_lock = threading.Lock()
_active: dict[str, object] = {}  # conv_uuid -> Agent en ejecucion


def register(conv_uuid: str, agent) -> None:
    with _lock:
        _active[conv_uuid] = agent


def unregister(conv_uuid: str) -> None:
    with _lock:
        _active.pop(conv_uuid, None)


def is_active(conv_uuid: str) -> bool:
    with _lock:
        return conv_uuid in _active


def cancel(conv_uuid: str) -> bool:
    """Pide cancelar el turno activo de la conversacion.

    Devuelve True si habia un turno corriendo (y se le pidio parar), False si no
    habia nada en vuelo.
    """
    with _lock:
        agent = _active.get(conv_uuid)
    if agent is None:
        return False
    agent.cancel()
    return True
