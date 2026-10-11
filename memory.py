"""Memory logic: load history from SQLite, save turns, count tokens, summarize."""
import os

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

import agent
from db import NOW, get_conn, make_title

# Limits (override in .env). Small defaults so the demo hits them quickly.
MAX_CHAT_TOKENS = int(os.getenv("MAX_CHAT_TOKENS", "4000"))      # hard stop for a chat
CONTEXT_SOFT_LIMIT = int(os.getenv("CONTEXT_SOFT_LIMIT", "1000"))  # summarize above this
KEEP_RECENT = int(os.getenv("KEEP_RECENT", "4"))                 # messages kept verbatim


def count_tokens(text: str) -> int:
    """Rough estimate: about 4 characters per token."""
    return max(1, len(text) // 4)


def _live_rows(conv_id: int):
    """Latest summary (if any) + every normal message after what it covers."""
    with get_conn() as conn:
        summary = conn.execute(
            "SELECT * FROM messages WHERE conversation_id = ? AND is_summary = 1 "
            "ORDER BY id DESC LIMIT 1", (conv_id,),
        ).fetchone()
        after = summary["covers_up_to_id"] if summary else 0
        rows = conn.execute(
            "SELECT * FROM messages WHERE conversation_id = ? AND is_summary = 0 "
            "AND id > ? ORDER BY id", (conv_id, after),
        ).fetchall()
    return summary, rows


def build_history(conv_id: int) -> list:
    """Rows from the DB -> LangChain message objects for the model."""
    summary, rows = _live_rows(conv_id)
    history = []
    if summary:
        history.append(SystemMessage(content="Summary of earlier chat: " + summary["content"]))
    for r in rows:
        if r["role"] == "user":
            history.append(HumanMessage(content=r["content"]))
        elif r["role"] == "assistant":
            history.append(AIMessage(content=r["content"]))
    return history


def save_turn(conv_id: int, user_text: str, reply: str) -> int:
    """Save user message + assistant reply in ONE transaction. Returns new total_tokens."""
    u_tokens, a_tokens = count_tokens(user_text), count_tokens(reply)
    with get_conn() as conn:  # commits on success, rolls back on error
        conn.execute(
            "INSERT INTO messages (conversation_id, role, content, token_count) VALUES (?,?,?,?)",
            (conv_id, "user", user_text, u_tokens),
        )
        conn.execute(
            "INSERT INTO messages (conversation_id, role, content, token_count) VALUES (?,?,?,?)",
            (conv_id, "assistant", reply, a_tokens),
        )
        # Level 2: also set the sidebar title (first message only) and bump updated_at.
        conn.execute(
            "UPDATE conversations SET total_tokens = total_tokens + ?, "
            "title = CASE WHEN title = 'New chat' THEN ? ELSE title END, "
            f"updated_at = {NOW} WHERE id = ?",
            (u_tokens + a_tokens, make_title(user_text), conv_id),
        )
        return conn.execute(
            "SELECT total_tokens FROM conversations WHERE id = ?", (conv_id,)
        ).fetchone()[0]


def maybe_summarize(conv_id: int, llm) -> None:
    """If the live context is too big, replace older messages with a summary row."""
    summary, rows = _live_rows(conv_id)
    context_tokens = sum(r["token_count"] or 0 for r in rows) + (
        summary["token_count"] or 0 if summary else 0
    )
    if context_tokens <= CONTEXT_SOFT_LIMIT or len(rows) <= KEEP_RECENT:
        return

    old = rows[:-KEEP_RECENT]  # summarize everything except the most recent few
    text = ""
    if summary:
        text += "Earlier summary: " + summary["content"] + "\n"
    text += "\n".join(f"{r['role']}: {r['content']}" for r in old)

    new_summary = agent.summarize(llm, text)
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO messages (conversation_id, role, content, token_count, "
            "is_summary, covers_up_to_id) VALUES (?,?,?,?,1,?)",
            (conv_id, "system", new_summary, count_tokens(new_summary), old[-1]["id"]),
        )


def list_messages(conv_id: int) -> list[dict]:
    """What the UI shows: real messages only (summary rows are hidden)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE conversation_id = ? "
            "AND is_summary = 0 ORDER BY id", (conv_id,),
        ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in rows]
