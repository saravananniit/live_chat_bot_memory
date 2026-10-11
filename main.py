"""FastAPI app, Level 3: login, per-user conversations, model + API key from the UI.

Convention kept from Level 1: the JSON endpoints always answer HTTP 200 with a "status"
field, so the UI never sees a raw error. Statuses:
  ok               everything fine
  auth_required    not logged in (UI goes to the login page)
  settings_error   missing/rejected API key or unknown model (fix it in Settings; chat stays open)
  rate_limited     Groq rate limit (wait and retry; chat stays open)
  limit_reached    chat is full (start a new chat)
  error            anything else (start a new chat)
"""
import logging
import os
import re
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # must run before importing modules that read env vars

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import agent
import auth
import db
import memory

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("chat")

HERE = Path(__file__).parent
COOKIE = "session"
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "0") == "1"   # set to 1 when served over HTTPS
MODEL_RE = re.compile(r"^[A-Za-z0-9._:/\-]{1,100}$")

NEW_CHAT_MSG = "This chat has reached its limit. Please start a new chat."
ERROR_MSG = "Something went wrong. Please start a new chat."
LOGIN_MSG = "Please log in to continue."

app = FastAPI(title="YourBot - LangChain memory chat")
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")   # shared stylesheet
db.init_db()


class Credentials(BaseModel):
    email: str
    password: str
    name: str = ""         # optional display name, used by sign-up


class ChatIn(BaseModel):
    conversation_id: int
    message: str
    api_key: str = ""      # typed in the UI, used for this request only, never stored
    model: str = ""
    web_search: bool = True   # Settings switch: lets the agent use DuckDuckGo search tools


def friendly(status: str, message: str, **extra) -> JSONResponse:
    """Always HTTP 200 with a status field, so the UI never sees a raw error."""
    return JSONResponse({"status": status, "message": message, **extra})


def current_user(request: Request):
    return auth.user_for_token(request.cookies.get(COOKIE))


def with_session_cookie(resp: JSONResponse, token: str) -> JSONResponse:
    resp.set_cookie(COOKIE, token, max_age=auth.SESSION_DAYS * 86400,
                    httponly=True, samesite="lax", secure=COOKIE_SECURE, path="/")
    return resp


@app.exception_handler(RequestValidationError)
async def bad_request(_: Request, __: RequestValidationError):
    return friendly("error", ERROR_MSG)


@app.exception_handler(Exception)
async def anything_else(_: Request, exc: Exception):
    log.exception("unhandled error: %s", exc)
    return friendly("error", ERROR_MSG)


# ------------------------------------------------------------------ pages
@app.get("/")
def home(request: Request):
    if current_user(request) is None:
        return RedirectResponse("/login", status_code=302)
    return FileResponse(HERE / "index.html")


@app.get("/login")
def login_page(request: Request):
    if current_user(request) is not None:
        return RedirectResponse("/", status_code=302)
    return FileResponse(HERE / "login.html")


# ------------------------------------------------------------------ auth
@app.post("/auth/register")
def register(body: Credentials):
    email = body.email.strip().lower()
    problem = auth.validate_credentials(email, body.password)
    if problem:
        return friendly("error", problem)
    user_id = auth.create_user(email, body.password, body.name)
    if user_id is None:
        return friendly("error", "That email is already registered. Please log in.")
    return with_session_cookie(
        friendly("ok", "Account created.", email=email, name=auth.clean_name(body.name, email)),
        auth.create_session(user_id))


@app.post("/auth/login")
def login(body: Credentials):
    email = body.email.strip().lower()
    user = auth.authenticate(email, body.password)
    if user is None:   # same message for unknown email and wrong password
        return friendly("error", "Invalid email or password.")
    return with_session_cookie(friendly("ok", "Logged in.", email=email, name=user["name"]),
                               auth.create_session(user["id"]))


@app.post("/auth/logout")
def logout(request: Request):
    auth.delete_session(request.cookies.get(COOKIE))
    resp = friendly("ok", "Logged out.")
    resp.delete_cookie(COOKIE, path="/")
    return resp


@app.get("/auth/me")
def me(request: Request):
    user = current_user(request)
    if user is None:
        return friendly("auth_required", LOGIN_MSG)
    return {"status": "ok", "email": user["email"], "name": user["name"] or user["email"].split("@")[0]}


# ------------------------------------------------------------------ conversations (per user)
@app.get("/conversations")
def list_conversations(request: Request):
    user = current_user(request)
    if user is None:
        return friendly("auth_required", LOGIN_MSG, conversations=[])
    try:
        return {"status": "ok", "conversations": db.list_conversations(user["id"])}
    except Exception as exc:
        log.exception("list failed: %s", exc)
        return {"status": "error", "conversations": []}


@app.post("/conversations")
def new_conversation(request: Request):
    user = current_user(request)
    if user is None:
        return friendly("auth_required", LOGIN_MSG)
    return {"status": "ok", "conversation_id": db.create_conversation(user["id"])}


@app.get("/conversations/{conv_id}/messages")
def get_messages(conv_id: int, request: Request):
    user = current_user(request)
    if user is None:
        return friendly("auth_required", LOGIN_MSG)
    conv = db.get_conversation(conv_id, user["id"])   # None if it is not this user's chat
    if conv is None:
        return friendly("error", ERROR_MSG)
    return {
        "status": "ok" if conv["status"] == "active" else "limit_reached",
        "messages": memory.list_messages(conv_id),
        "title": conv["title"],
        "total_tokens": conv["total_tokens"],
        "max_tokens": memory.MAX_CHAT_TOKENS,
        "message": "" if conv["status"] == "active" else NEW_CHAT_MSG,
    }


# ------------------------------------------------------------------ chat
@app.post("/chat")
def chat(body: ChatIn, request: Request):
    user = current_user(request)
    if user is None:
        return friendly("auth_required", LOGIN_MSG)

    text = body.message.strip()
    conv = db.get_conversation(body.conversation_id, user["id"])

    # 1. unknown / not-yours conversation, or empty message
    if conv is None or not text:
        return friendly("error", ERROR_MSG)
    if conv["status"] == "closed":
        return friendly("limit_reached", NEW_CHAT_MSG)

    # 2. settings typed in the UI (these are fixable, so the chat stays open)
    api_key = body.api_key.strip()
    model = body.model.strip() or agent.DEFAULT_MODEL
    if not api_key or len(api_key) > 500:
        return friendly("settings_error", "Please add your Groq API key in Settings.")
    if not MODEL_RE.match(model):
        return friendly("settings_error", "That model name does not look right. Please check it in Settings.")

    # 3. hard limit: refuse BEFORE calling the model if this message won't fit
    if conv["total_tokens"] + memory.count_tokens(text) >= memory.MAX_CHAT_TOKENS:
        db.close_conversation(conv["id"])
        return friendly("limit_reached", NEW_CHAT_MSG)

    # 4. normal turn: load history -> run agent -> save both messages
    try:
        llm = agent.make_llm(api_key, model)
        history = memory.build_history(conv["id"])
        reply, tools_used = agent.run_agent(llm, history, text, web=body.web_search)
        if not reply:
            raise RuntimeError("empty reply from model")
        total = memory.save_turn(conv["id"], text, reply)
    except Exception as exc:
        kind = agent.classify_error(exc)
        if kind == "key":
            return friendly("settings_error", "Groq rejected the API key. Please check it in Settings.")
        if kind == "model":
            return friendly("settings_error", "Groq could not find that model. Please check the model name in Settings.")
        if kind == "rate":
            return friendly("rate_limited", "Groq rate limit reached. Please wait a few seconds and try again.")
        log.exception("chat failed: %s", exc)     # the API key is never logged
        db.close_conversation(conv["id"])
        return friendly("error", ERROR_MSG)

    # 5. keep the context small (failure here must not break the chat)
    try:
        memory.maybe_summarize(conv["id"], llm)
    except Exception as exc:
        log.warning("summarize skipped: %s", type(exc).__name__)

    # 6. if this turn used up the budget, show the reply but close the chat
    if total >= memory.MAX_CHAT_TOKENS:
        db.close_conversation(conv["id"])
        return friendly("limit_reached", NEW_CHAT_MSG, reply=reply, tools=tools_used,
                        total_tokens=total, max_tokens=memory.MAX_CHAT_TOKENS)

    return {"status": "ok", "reply": reply, "tools": tools_used, "total_tokens": total,
            "max_tokens": memory.MAX_CHAT_TOKENS}
