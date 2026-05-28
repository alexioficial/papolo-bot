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
