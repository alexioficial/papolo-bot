"""Modelos de DeepSeek disponibles + resolucion del modelo configurado.

DeepSeek expone un endpoint OpenAI-compatible (`GET /models`) que lista los modelos
de la cuenta. Lo consultamos via el cliente del motor (`papolo.deepseek.get_client`)
y cacheamos el resultado: el callback de autocomplete de Discord tiene ~3s de
presupuesto y se dispara por cada tecla, no podemos pegarle a la API cada vez.

El modelo elegido se persiste en la tabla `settings` de SQLite (clave `model`) y
pisa al default del env (`DEEPSEEK_MODEL`) para TODOS los threads en su proximo turno.
"""
import logging
import time

from papolo.deepseek import get_client, model_name

from . import db

log = logging.getLogger("papolo-bot")

# Fallback cuando la API no responde: los modelos publicos de DeepSeek.
KNOWN_MODELS = ["deepseek-chat", "deepseek-reasoner"]

MODEL_SETTING_KEY = "model"

_CACHE_TTL = 600.0  # 10 min
_cache: list[str] = []
_cache_ts: float = 0.0


def _fetch() -> list[str]:
    resp = get_client().models.list()
    return sorted({m.id for m in resp.data if getattr(m, "id", None)})


def available_models(force: bool = False) -> list[str]:
    """Modelos disponibles, cacheados. Es blocking (I/O de red): llamalo dentro de
    `asyncio.to_thread` desde codigo async.

    Nunca levanta: si la API falla devuelve el ultimo cache o `KNOWN_MODELS`.
    """
    global _cache, _cache_ts
    if _cache and not force and (time.monotonic() - _cache_ts) < _CACHE_TTL:
        return _cache
    try:
        ids = _fetch()
        if ids:
            _cache = ids
            _cache_ts = time.monotonic()
            return _cache
    except Exception as e:
        log.warning("No se pudo listar modelos de DeepSeek: %s", e)
    return _cache or list(KNOWN_MODELS)


def current_model() -> str:
    """El modelo activo: override persistido en DB, o el default del env del motor."""
    return db.get_setting(MODEL_SETTING_KEY) or model_name()


def set_model(model: str) -> None:
    db.set_setting(MODEL_SETTING_KEY, model)
