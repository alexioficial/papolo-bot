import asyncio
import logging
import os
import subprocess
from pathlib import Path

from papolo import Agent
from papolo import deploy as papolo_deploy

from . import confirmations, db
from .models import current_model

log = logging.getLogger("papolo-bot")

# Inyectado por bot.py al inicio: necesario para postear confirmaciones desde
# threads sync (las tools de deploy corren en ThreadPoolExecutor).
_bot = None
_loop = None


def bind_bot(bot, loop: asyncio.AbstractEventLoop) -> None:
    global _bot, _loop
    _bot = bot
    _loop = loop


def _workspace_root() -> Path:
    root = Path(os.environ.get("PAPOLO_WORKSPACE_ROOT", "./data/workspaces"))
    root.mkdir(parents=True, exist_ok=True)
    return root


def workspace_path(conversation_uuid: str) -> Path:
    return _workspace_root() / conversation_uuid


def _ensure_workspace(conversation_uuid: str) -> str:
    ws = workspace_path(conversation_uuid)
    new = not ws.exists()
    ws.mkdir(parents=True, exist_ok=True)

    if new or not (ws / ".git").exists():
        try:
            subprocess.run(
                ["git", "init", "-q", "-b", "main"],
                cwd=ws, check=True, capture_output=True, text=True,
            )
            subprocess.run(
                ["git", "config", "user.email", "papolo@bot.local"],
                cwd=ws, check=True, capture_output=True, text=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Papolo"],
                cwd=ws, check=True, capture_output=True, text=True,
            )
            subprocess.run(
                ["git", "commit", "--allow-empty", "-q", "-m", "init workspace"],
                cwd=ws, check=True, capture_output=True, text=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            log.warning("No se pudo inicializar git en %s: %s", ws, e)

    return str(ws.resolve())


def get_or_create_agent(conversation_uuid: str) -> Agent:
    workspace_dir = _ensure_workspace(conversation_uuid)
    saved = db.load_agent_state(conversation_uuid)
    # El modelo se resuelve fresco en cada turno (el bot crea un Agent nuevo por turno),
    # asi que cambiarlo con /papolo-model aplica en el proximo mensaje de cualquier thread.
    kwargs = dict(
        workspace_dir=workspace_dir,
        conversation_uuid=conversation_uuid,
        model=current_model(),
    )
    if saved:
        kwargs["messages"] = saved
    return Agent(**kwargs)


def persist_agent(conversation_uuid: str, agent: Agent) -> None:
    db.save_agent_state(conversation_uuid, agent.messages)


# --- Wiring de deploy callbacks ---

def _post_to_thread_sync(thread_id: int, content: str) -> None:
    """Llamado desde threads sync (deploy tools). Schedula en el event loop."""
    if not (_bot and _loop and thread_id):
        log.warning("No se puede postear: bot/loop/thread_id no disponibles")
        return

    async def _send():
        try:
            ch = _bot.get_channel(thread_id) or await _bot.fetch_channel(thread_id)
            if ch is not None:
                await ch.send(content[:1900])
        except Exception as e:
            log.warning("Fallo posteo a thread %s: %s", thread_id, e)

    try:
        asyncio.run_coroutine_threadsafe(_send(), _loop)
    except Exception as e:
        log.warning("run_coroutine_threadsafe fallo: %s", e)


def _get_thread_id(conversation_uuid: str) -> int | None:
    with db.connect() as c:
        row = c.execute(
            "SELECT thread_id FROM conversations WHERE uuid = ?",
            (conversation_uuid,),
        ).fetchone()
    return int(row["thread_id"]) if row else None


def _confirmation_callback(conv_uuid: str, action: str, target: str,
                            confirm_token: str) -> tuple:
    if confirm_token == "ASK":
        code = confirmations.request(conv_uuid, action, target)
        thread_id = _get_thread_id(conv_uuid)
        action_human = {
            "delete_repo": "borrar el repo de GitHub",
            "destroy_app": "destruir la app de Coolify",
        }.get(action, action)
        msg = (
            f"⚠️ Papolo quiere **{action_human}** `{target}`.\n"
            f"Para autorizar, escribi: `confirmar {code}` (vale 5 min)."
        )
        if thread_id:
            _post_to_thread_sync(thread_id, msg)
        return ("PENDING", f"Esperando 'confirmar {code}' del usuario en el thread")
    if confirmations.consume(conv_uuid, action, target, confirm_token):
        return ("OK", confirm_token)
    return ("INVALID", "token invalido o expirado")


# Registrar callbacks al import — corre una vez por proceso.
papolo_deploy.set_db_callback(db.handle_deploy_event)
papolo_deploy.set_confirmation_callback(_confirmation_callback)
