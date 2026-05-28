import logging
import os
import subprocess
from pathlib import Path

from papolo import Agent

from . import db

log = logging.getLogger("papolo-bot")


def _workspace_root() -> Path:
    root = Path(os.environ.get("PAPOLO_WORKSPACE_ROOT", "./data/workspaces"))
    root.mkdir(parents=True, exist_ok=True)
    return root


def _ensure_workspace(conversation_uuid: str) -> str:
    ws = _workspace_root() / conversation_uuid
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
    if saved:
        return Agent(messages=saved, workspace_dir=workspace_dir)
    return Agent(workspace_dir=workspace_dir)


def persist_agent(conversation_uuid: str, agent: Agent) -> None:
    db.save_agent_state(conversation_uuid, agent.messages)
