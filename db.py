"""SQLite setup. Level 3 = Level 2 + users, sessions, and conversations that belong to a user.

New in Level 3:
  users          -> email + password hash
  sessions       -> one row per logged-in browser (only a HASH of the cookie token is stored)
  conversations.user_id -> who owns the chat; every query below filters by it
"""
import os
import sqlite3
from pathlib import Path

DB_PATH = os.getenv("DB_PATH") or str(Path(__file__).resolve().parent / "chat.db")

# Current time with milliseconds, so two chats updated in the same second still sort correctly.
NOW = "strftime('%Y-%m-%d %H:%M:%f', 'now')"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT NOT NULL UNIQUE COLLATE NOCASE,
    name          TEXT NOT NULL DEFAULT '',                  -- display name shown in the sidebar
    password_hash TEXT NOT NULL,
    created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sessions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,   -- Level 3
    status       TEXT    NOT NULL DEFAULT 'active'
                 CHECK (status IN ('active', 'closed')),
    total_tokens INTEGER NOT NULL DEFAULT 0,
    title        TEXT    NOT NULL DEFAULT 'New chat',
    created_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL
                    REFERENCES conversations(id) ON DELETE CASCADE,
    role            TEXT    NOT NULL
                    CHECK (role IN ('user', 'assistant', 'system')),
    content         TEXT    NOT NULL,
    token_count     INTEGER,
    is_summary      INTEGER NOT NULL DEFAULT 0
                    CHECK (is_summary IN (0, 1)),
    covers_up_to_id INTEGER,
    created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (is_summary = 1 OR covers_up_to_id IS NULL)
);

CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages (conversation_id, id);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions (user_id);
"""


def get_conn() -> sqlite3.Connection:
    """Open a connection. Foreign keys must be switched on for EVERY connection."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Upgrade an older database in place. Level 1 and Level 2 databases are both handled."""
    # --- from Level 3: display name on users ---
    user_cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
    if "name" not in user_cols:
        conn.execute("ALTER TABLE users ADD COLUMN name TEXT NOT NULL DEFAULT ''")
        conn.execute("UPDATE users SET name = substr(email, 1, instr(email, '@') - 1) WHERE name = ''")

    cols = {r["name"] for r in conn.execute("PRAGMA table_info(conversations)")}
    # --- from Level 1: sidebar columns ---
    if "title" not in cols:
        conn.execute("ALTER TABLE conversations ADD COLUMN title TEXT NOT NULL DEFAULT 'New chat'")
        conn.execute(
            "UPDATE conversations SET title = COALESCE((SELECT "
            "CASE WHEN length(content) > 40 THEN rtrim(substr(content, 1, 40)) || '…' ELSE content END "
            "FROM messages m WHERE m.conversation_id = conversations.id AND m.role = 'user' "
            "ORDER BY m.id LIMIT 1), 'New chat')"
        )
    if "updated_at" not in cols:
        conn.execute("ALTER TABLE conversations ADD COLUMN updated_at TIMESTAMP")
        conn.execute(
            "UPDATE conversations SET updated_at = COALESCE((SELECT MAX(created_at) "
            "FROM messages m WHERE m.conversation_id = conversations.id), created_at)"
        )
    # --- from Level 2: owner column ---
    if "user_id" not in cols:
        # Old chats have no owner. They stay in the file but nobody can see them.
        conn.execute("ALTER TABLE conversations ADD COLUMN user_id INTEGER REFERENCES users(id) ON DELETE CASCADE")


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_conversations_user_updated "
            "ON conversations (user_id, updated_at DESC)"
        )


def make_title(text: str, limit: int = 40) -> str:
    """Turn the first user message into a short sidebar title."""
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


# ------------------------------------------------------------------ conversations
# Every function takes user_id. A chat that belongs to someone else behaves exactly
# like a chat that does not exist, so ids cannot be probed.

def create_conversation(user_id: int) -> int:
    """Create a chat for this user. Reuse their empty active chat if they have one."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT c.id FROM conversations c WHERE c.user_id = ? AND c.status = 'active' "
            "AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.conversation_id = c.id) "
            "ORDER BY c.id DESC LIMIT 1", (user_id,),
        ).fetchone()
        if row:
            return row["id"]
        cur = conn.execute(
            f"INSERT INTO conversations (user_id, updated_at) VALUES (?, {NOW})", (user_id,)
        )
        return cur.lastrowid


def get_conversation(conv_id: int, user_id: int):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM conversations WHERE id = ? AND user_id = ?", (conv_id, user_id)
        ).fetchone()


def close_conversation(conv_id: int) -> None:
    """Call only after get_conversation(conv_id, user_id) succeeded."""
    with get_conn() as conn:
        conn.execute("UPDATE conversations SET status = 'closed' WHERE id = ?", (conv_id,))


def list_conversations(user_id: int, limit: int = 50) -> list[dict]:
    """Sidebar data for ONE user: chats with at least one message, newest activity first."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT c.id, c.title, c.status, c.total_tokens, c.updated_at "
            "FROM conversations c "
            "WHERE c.user_id = ? "
            "AND EXISTS (SELECT 1 FROM messages m WHERE m.conversation_id = c.id) "
            "ORDER BY c.updated_at DESC, c.id DESC LIMIT ?", (user_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]
