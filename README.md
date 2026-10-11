# YourBot: the Level 3 chat app in the Stitch "YourBot" design, plus web search

This stitches two things together:
- the **Level 3 backend**: login, per-user conversations, SQLite memory, summarizing, token limits, a LangChain agent on Groq;
- the **YourBot design** from the Stitch export ("Warm Editorial Minimalism": terracotta accent, light and dark themes),
  now wired to the real API instead of demo data.

New on top of that: the agent can **search the web with DuckDuckGo** (web and news).

## Run
```bash
pip install -r requirements.txt
cp .env.example .env        # optional: only limits live here
uvicorn main:app --reload
```
Open http://127.0.0.1:8000, create an account, then open **Model settings** (bottom of the sidebar)
and paste your Groq API key. The model defaults to `openai/gpt-oss-20b`.

## What you get
| Screen / feature | Where |
|---|---|
| Log in / Create account (name, email, password, confirm), show-password eye, dismissible error | `login.html` |
| Empty state with suggested prompts (one of them asks for today's news) | `index.html` |
| Sidebar: New chat, search, history grouped Today / Yesterday / Previous 7 days / Older, "full" badge | `index.html` |
| Model settings: model name, Groq API key, web-search switch | `index.html` |
| Typing dots while the agent works, code blocks with Copy, per-reply Copy | `index.html` |
| "Your session has expired" dialog when the login cookie runs out | `index.html` |
| Mobile drawer, light/dark theme (remembered) | `index.html` + `static/yourbot.css` |

Design items from the mockups that are **not** included because they have no backend behind them:
Discover / Archive / Vault navigation, Prompt Library, Share, thumbs up/down, regenerate, attachments, voice input,
the "Pro" badge, and the "unsaved drafts cached" line in the expired-session dialog.

## Agent tools
| Tool | What it does |
|---|---|
| `web_search` | DuckDuckGo web search (top 4 results: title, snippet, link) |
| `news_search` | DuckDuckGo news search (adds source and date) |
| `calculator` | Safe arithmetic |
| `get_current_time` | Current date and time |

- Search needs no API key (it uses the `ddgs` package) but does need internet access from the machine running the app.
- If DuckDuckGo is unreachable or rate-limits you, the tool returns a short "search unavailable" note and the bot
  answers without it. The chat is never closed because of a failed search.
- Results are trimmed (4 results, about 280 characters each) because search text goes back to the model and counts toward
  Groq's tokens-per-minute limit. If you hit the limit, turn **web search** off in Model settings.
- Search results are treated as untrusted text: the system prompt tells the model not to follow instructions found in them.
- After a reply that used tools, a small note ("Searched the web", "Used the calculator") appears under it.
  It is shown for live replies only; it is not saved in the database.

## Files
| File | Job |
|---|---|
| `login.html`, `index.html` | The two pages |
| `static/yourbot.css` | Shared design system (tokens, components, dark theme) |
| `main.py` | Pages, auth endpoints, per-user conversation endpoints, chat |
| `auth.py` | Password hashing (scrypt), users, login sessions |
| `db.py` | Schema, migration, per-user queries |
| `memory.py` | History, token counting, summarizing |
| `agent.py` | LangChain model, agent and tools (built per request from the UI key + model) |

## Database
```
users          id, email (unique), name, password_hash, created_at          <- name is new here
sessions       id, user_id, token_hash, created_at, expires_at
conversations  id, user_id, status, total_tokens, title, created_at, updated_at
messages       id, conversation_id, role, content, token_count, is_summary, covers_up_to_id, created_at
```
An existing Level 1, 2 or 3 `chat.db` is upgraded automatically on startup (adds `users.name`, filled from the email,
and the earlier columns). By default, the database is resolved next to `db.py`, regardless of the server's launch
directory; set `DB_PATH` to use a different database. Chats from before Level 3 have no owner and stay hidden.

## Security notes
- Passwords: scrypt with a random salt. Login cookie: `HttpOnly`, `SameSite=Lax`; only a SHA-256 hash of the token is stored.
- Every conversation query is filtered by user. Someone else's chat looks like a chat that does not exist.
- The Groq key is kept in your browser's `localStorage` (per account email), sent with each message, and never stored
  or logged by the server. Use HTTPS and set `COOKIE_SECURE=1` when you deploy.
- Not included: password reset, email verification, login throttling.

## Error behavior
| Situation | What the user sees | Chat |
|---|---|---|
| No key / key rejected / unknown model | Banner or Model settings opens, message put back in the box | stays open |
| Groq rate limit | "Please wait a few seconds and try again" | stays open |
| Session expired | "Your session has expired" dialog | stays open |
| Token limit reached | Banner with a **Start new chat** button | closed |
| Any other failure | "Something went wrong. Please start a new chat." | closed |
