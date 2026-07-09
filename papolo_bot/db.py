import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    uuid TEXT PRIMARY KEY,
    thread_id INTEGER UNIQUE NOT NULL,
    guild_id INTEGER,
    channel_id INTEGER,
    created_by INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_uuid TEXT NOT NULL,
    discord_msg_id INTEGER,
    author_id INTEGER,
    author_name TEXT,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    reply_to_msg_id INTEGER,
    created_at TEXT NOT NULL,
    FOREIGN KEY (conversation_uuid) REFERENCES conversations(uuid)
);

CREATE INDEX IF NOT EXISTS idx_messages_conversation
    ON messages(conversation_uuid);

CREATE TABLE IF NOT EXISTS agent_state (
    conversation_uuid TEXT PRIMARY KEY,
    messages_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (conversation_uuid) REFERENCES conversations(uuid)
);

CREATE TABLE IF NOT EXISTS deployments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_uuid TEXT NOT NULL,
    github_repo_name TEXT,
    github_repo_url TEXT,
    coolify_app_uuid TEXT,
    preview_url TEXT,
    status TEXT NOT NULL,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (conversation_uuid) REFERENCES conversations(uuid)
);

CREATE INDEX IF NOT EXISTS idx_deployments_conv
    ON deployments(conversation_uuid);

CREATE INDEX IF NOT EXISTS idx_deployments_app
    ON deployments(coolify_app_uuid);
"""

_db_path: Path | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_db(path: str | None = None) -> Path:
    global _db_path
    if path is None:
        path = os.environ.get("PAPOLO_DB_PATH", "./data/papolo.sqlite")
    _db_path = Path(path)
    _db_path.parent.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)
    return _db_path


def db_path() -> Path:
    if _db_path is None:
        return init_db()
    return _db_path


@contextmanager
def connect():
    conn = sqlite3.connect(str(db_path()))
    conn.row_factory = sqlite3.Row
    # WAL + busy_timeout: el bot abre conexiones desde varios threads/coroutines
    # (agente en ThreadPoolExecutor + handlers async). Sin esto, escrituras
    # concurrentes tiran "database is locked".
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def create_conversation(
    uuid: str,
    thread_id: int,
    guild_id: int | None,
    channel_id: int | None,
    created_by: int | None,
) -> None:
    with connect() as c:
        c.execute(
            "INSERT INTO conversations (uuid, thread_id, guild_id, channel_id, created_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (uuid, thread_id, guild_id, channel_id, created_by, _now()),
        )


def get_conversation_by_thread(thread_id: int) -> dict | None:
    with connect() as c:
        row = c.execute(
            "SELECT * FROM conversations WHERE thread_id = ?", (thread_id,)
        ).fetchone()
    return dict(row) if row else None


def save_message(
    conversation_uuid: str,
    role: str,
    content: str,
    discord_msg_id: int | None = None,
    author_id: int | None = None,
    author_name: str | None = None,
    reply_to_msg_id: int | None = None,
) -> None:
    with connect() as c:
        c.execute(
            "INSERT INTO messages "
            "(conversation_uuid, discord_msg_id, author_id, author_name, role, content, reply_to_msg_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                conversation_uuid,
                discord_msg_id,
                author_id,
                author_name,
                role,
                content,
                reply_to_msg_id,
                _now(),
            ),
        )


def get_messages(conversation_uuid: str) -> list[dict]:
    """Mensajes del lado Discord (los visibles al usuario)."""
    with connect() as c:
        rows = c.execute(
            "SELECT * FROM messages WHERE conversation_uuid = ? ORDER BY id ASC",
            (conversation_uuid,),
        ).fetchall()
    return [dict(r) for r in rows]


def load_agent_state(conversation_uuid: str) -> list[dict] | None:
    with connect() as c:
        row = c.execute(
            "SELECT messages_json FROM agent_state WHERE conversation_uuid = ?",
            (conversation_uuid,),
        ).fetchone()
    if not row:
        return None
    return json.loads(row["messages_json"])


def save_agent_state(conversation_uuid: str, messages: list[dict]) -> None:
    payload = json.dumps(messages, ensure_ascii=False)
    with connect() as c:
        c.execute(
            "INSERT INTO agent_state (conversation_uuid, messages_json, updated_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(conversation_uuid) DO UPDATE SET "
            "messages_json = excluded.messages_json, updated_at = excluded.updated_at",
            (conversation_uuid, payload, _now()),
        )


def delete_agent_state(conversation_uuid: str) -> None:
    with connect() as c:
        c.execute(
            "DELETE FROM agent_state WHERE conversation_uuid = ?",
            (conversation_uuid,),
        )


# --- Deployments ---

def count_repos_for_conv(conversation_uuid: str) -> int:
    with connect() as c:
        row = c.execute(
            "SELECT COUNT(*) AS n FROM deployments "
            "WHERE conversation_uuid = ? AND github_repo_name IS NOT NULL "
            "AND status != 'destroyed'",
            (conversation_uuid,),
        ).fetchone()
    return int(row["n"]) if row else 0


def create_deployment(conversation_uuid: str, **fields) -> int:
    cols = ["conversation_uuid", "status", "created_at", "updated_at"]
    vals = [conversation_uuid, fields.get("status", "pending"), _now(), _now()]
    for k in ("github_repo_name", "github_repo_url", "coolify_app_uuid",
              "preview_url", "last_error"):
        if k in fields:
            cols.append(k)
            vals.append(fields[k])
    with connect() as c:
        cur = c.execute(
            f"INSERT INTO deployments ({', '.join(cols)}) "
            f"VALUES ({', '.join(['?'] * len(vals))})",
            tuple(vals),
        )
        return cur.lastrowid


def update_active_deployment(conversation_uuid: str, **fields) -> None:
    """Actualiza el ultimo deployment activo de la conversacion, o crea uno si no existe."""
    if not fields:
        return
    with connect() as c:
        row = c.execute(
            "SELECT id FROM deployments WHERE conversation_uuid = ? "
            "AND status != 'destroyed' ORDER BY id DESC LIMIT 1",
            (conversation_uuid,),
        ).fetchone()
        if row:
            set_parts = ", ".join(f"{k} = ?" for k in fields)
            vals = list(fields.values()) + [_now(), row["id"]]
            c.execute(
                f"UPDATE deployments SET {set_parts}, updated_at = ? WHERE id = ?",
                tuple(vals),
            )
        else:
            create_deployment(conversation_uuid, **fields)


def get_active_deployment(conversation_uuid: str) -> dict | None:
    with connect() as c:
        row = c.execute(
            "SELECT * FROM deployments WHERE conversation_uuid = ? "
            "AND status != 'destroyed' ORDER BY id DESC LIMIT 1",
            (conversation_uuid,),
        ).fetchone()
    return dict(row) if row else None


def get_deployments_by_conv(conversation_uuid: str) -> list[dict]:
    with connect() as c:
        rows = c.execute(
            "SELECT * FROM deployments WHERE conversation_uuid = ? ORDER BY id DESC",
            (conversation_uuid,),
        ).fetchall()
    return [dict(r) for r in rows]


def handle_deploy_event(event: str, payload: dict):
    """Callback que pasamos al modulo papolo.deploy. Persiste eventos en SQLite.

    Eventos esperados:
      - count_repos -> int
      - repo_created, app_created, deploy_triggered, repo_deleted, app_destroyed
    """
    conv = payload.get("conversation_uuid")
    if event == "count_repos" and conv:
        return count_repos_for_conv(conv)
    if not conv:
        return None
    if event == "repo_created":
        create_deployment(
            conv,
            status="pending",
            github_repo_name=payload.get("github_repo_name"),
            github_repo_url=payload.get("github_repo_url"),
        )
    elif event == "app_created":
        update_active_deployment(
            conv,
            status="building",
            coolify_app_uuid=payload.get("coolify_app_uuid"),
            preview_url=payload.get("preview_url"),
        )
    elif event == "deploy_triggered":
        update_active_deployment(conv, status="building")
    elif event == "repo_deleted":
        update_active_deployment(conv, status="destroyed")
    elif event == "app_destroyed":
        update_active_deployment(conv, status="destroyed")
    return None
