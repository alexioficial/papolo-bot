"""
Store TTL-based de pending confirmations para ops destructivas.

Independiente de Discord — solo guarda codigos. La logica de postear al thread
y matchear respuestas del usuario vive en conversations.py / handlers.py.
"""

import secrets
import threading
import time

TTL_SECONDS = 300  # 5 min

_pending: dict = {}  # (conv_uuid, action, target) -> (code, expires_at)
_lock = threading.Lock()


def _now() -> float:
    return time.time()


def _purge_locked() -> None:
    now = _now()
    expired = [k for k, (_, exp) in _pending.items() if exp < now]
    for k in expired:
        _pending.pop(k, None)


def request(conv_uuid: str, action: str, target: str) -> str:
    """Crea (o refresca) un codigo de confirmacion. Devuelve el codigo en hex."""
    with _lock:
        _purge_locked()
        code = secrets.token_hex(3)  # 6 hex chars: "a3f8c1"
        _pending[(conv_uuid, action, target)] = (code, _now() + TTL_SECONDS)
    return code


def consume(conv_uuid: str, action: str, target: str, token: str) -> bool:
    """Valida y elimina. True si era valido."""
    with _lock:
        _purge_locked()
        key = (conv_uuid, action, target)
        entry = _pending.get(key)
        if not entry:
            return False
        code, exp = entry
        if exp < _now() or token != code:
            return False
        _pending.pop(key, None)
        return True
