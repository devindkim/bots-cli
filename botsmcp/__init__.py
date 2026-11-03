"""The MCP server the CLIs launch over stdio. Each tool is one call into store.py.

Env: BOT_ID, BOT_RUN_ID, BOTS_HOME. Every response ends with the caller's unread count, so a
working bot notices messages that arrive mid-turn. In isolated mode the server can't open
the database and forwards each call over BOTS_MCP_SOCKET with the run's capability."""

from __future__ import annotations

import contextvars
import functools
import hashlib
import inspect
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from mcp.server.mcpserver import MCPServer

from host import config
from host.store import (
    Held,
    Message,
    Rejected,
    Store,
    open_store,
    render_ask,
    user_conversation,
)

BOT_ID = os.environ.get("BOT_ID", "")
RUN_ID = int(os.environ.get("BOT_RUN_ID", "0") or 0)


@functools.cache
def _load_cfg() -> config.Config:
    return config.load()


server = MCPServer("bots")
_store: Store | None = None
_host_cfg: config.Config | None = None


@dataclass(frozen=True)
class Caller:
    """Who a host-side call runs as, from the capability, never from the bot."""

    bot_id: str
    run_id: int
    isolated: bool


_caller: contextvars.ContextVar[Caller | None] = contextvars.ContextVar("caller", default=None)


def _bot() -> str:
    c = _caller.get()
    return c.bot_id if c else BOT_ID


def _run() -> int:
    c = _caller.get()
    return c.run_id if c else RUN_ID


def _isolated() -> bool:
    c = _caller.get()
    return c.isolated if c else False


def cfg() -> config.Config:
    return _host_cfg or _load_cfg()


def store() -> Store:
    global _store
    if _store is None:
        _store = open_store(cfg())
    return _store


def attach(host_cfg: config.Config, host_store: Store) -> None:
    """Run the tools in the host process, on the host's store (host/mcpsock.py)."""
    global _store, _host_cfg
    _host_cfg, _store = host_cfg, host_store


def _seen() -> dict[str, int]:
    run = store().get_run(_run()) if _run() else None
    return run["delivered"] if run else {}


def _footer() -> str:
    n = store().unread_count(_bot(), _seen())
    return f"\n[{n} unread{'; call read_messages' if n else ''}]"


def _nonce(*parts: str) -> str:
    """Same send twice in one run (a retried tool call) is dropped by the host."""
    return hashlib.sha256("\x00".join((str(_run()), *parts)).encode()).hexdigest()


# -- tools: one table, run here or forwarded to the host -------------------

TOOLS: dict[str, Callable[..., str]] = {}


class Remote:
    """The sandboxed side: one connection to the host's socket, one call at a time."""

    def __init__(self, path: str, cap: str) -> None:
        self.path, self.cap = path, cap
        self._sock: socket.socket | None = None
        self._file: Any = None
        self._lock = threading.Lock()

    def call(self, tool: str, args: dict[str, Any]) -> str:
        with self._lock:
            if self._sock is None:
                self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self._sock.connect(self.path)
                self._file = self._sock.makefile("rwb")
            request = {"cap": self.cap, "tool": tool, "args": args}
            self._file.write(json.dumps(request).encode() + b"\n")
            self._file.flush()
            line = self._file.readline()
        if not line:
            return "rejected: the host closed the connection"
        reply = json.loads(line)
        return str(reply["result"]) if "result" in reply else f"rejected: {reply.get('error')}"


_remote: Remote | None = None


def tool(description: str | None = None) -> Callable[[Callable[..., str]], Callable[..., str]]:
    """Register a tool with the MCP server. In isolated mode the registered handler sends the
    call to the host instead of running it; the function itself is what the host runs."""

    def register(fn: Callable[..., str]) -> Callable[..., str]:
        TOOLS[fn.__name__] = fn
        sig = inspect.signature(fn)

        @functools.wraps(fn)
        def handler(*args: Any, **kwargs: Any) -> str:
            if _remote is not None:
                return _remote.call(fn.__name__, dict(sig.bind(*args, **kwargs).arguments))
            return fn(*args, **kwargs)

        server.tool(description=description)(handler)
        return fn

    return register


def call(caller: Caller, name: str, args: dict[str, Any]) -> str:
    """Host side: run tool `name` as `caller`. Bad arguments are the bot's mistake, so they
    come back as a refusal, not an exception."""
    fn = TOOLS.get(name)
    if fn is None:
        return f"rejected: no tool {name!r}"
    try:
        inspect.signature(fn).bind(**args)
    except TypeError as e:
        return f"rejected: {e}"
    token = _caller.set(caller)
    try:
        return fn(**args)
    finally:
        _caller.reset(token)


def _sent(msg: Message | None) -> str:
    return ("duplicate, already sent" if msg is None else f"sent (message {msg.id})") + _footer()


def _render(messages: list[Message]) -> str:
    return "\n".join(
        f"#{m.id} [{m.sender}{' (event)' if m.kind == 'event' else ''}] {m.body}"
        + render_ask(m.card)
        for m in messages
    )


def _notify(title: str, body: str) -> None:
    """A desktop notification: osascript on macOS, notify-send where it is installed (most
    Linux desktops), else none. Never waits on it, never fails the send."""
    if sys.platform == "darwin":
        script = f"display notification {_applescript(body[:200])} with title {_applescript(title)}"
        argv = ["osascript", "-e", script]
    elif notify_send := shutil.which("notify-send"):
        argv = [notify_send, "--app-name=bots", "--", title, body[:200]]
    else:
        return
    try:
        subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def _applescript(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


ASK_DOC = """ask (optional) lists options under the message, for a choice the user can make
    in a word: [{"q": "Restart the host?", "options": [{"label": "When idle",
    "detail": "optional, one line", "recommended": true}, {"label": "Now"}], "multi": false}].
    1-4 questions, 2-6 options each; label up to 60 chars, detail 120, q 200; at most one
    recommended; multi lets them pick several. The answer arrives as an ordinary message."""

Ask = list[dict[str, Any]] | None


def _ask_key(ask: Ask) -> list[str]:
    """The ask as a nonce part; none without one."""
    return [json.dumps(ask, sort_keys=True)] if ask is not None else []


@tool(
    description=f"""Message the user. Only sends are delivered; nothing you print
    reaches anyone. To ask something you cannot settle yourself, ask here, then end your turn.
    {ASK_DOC}"""
)
def send_to_user(body: str, ask: Ask = None) -> str:
    try:
        msg = store().send_to_user(_bot(), body, _nonce("user", body, *_ask_key(ask)), ask)
    except (ValueError, TypeError) as e:
        return f"rejected: {e}; nothing was sent" + _footer()
    if msg and cfg().notify:
        bot = store().get_bot(_bot())
        _notify(bot["name"] if bot else _bot(), body)
    return _sent(msg)


@tool()
def send_to_agent(agent_id: str, body: str) -> str:
    """Message another bot by id (see list_agents). It is woken to handle it."""
    try:
        return _sent(store().send_to_agent(_bot(), agent_id, body, _nonce(agent_id, body)))
    except Rejected as e:
        return f"rejected: {e}" + _footer()


@tool(
    description=f"""Post in a group you belong to. @mention the bots who should act;
    plain text, 1-3 sentences. If others posted since you last read, nothing is sent and you
    get their messages instead. {ASK_DOC}"""
)
def send_to_group(group_id: str, body: str, ask: Ask = None) -> str:
    nonce = _nonce(group_id, body, *_ask_key(ask))
    try:
        return _sent(store().send_to_group(_bot(), group_id, body, _run(), nonce, ask))
    except Held as e:
        return (
            "not sent: others posted while you were working:\n"
            + _render(e.missed)
            + "\n\nIf they covered it, send nothing. Otherwise send only what you add."
            + _footer()
        )
    except (Rejected, ValueError, TypeError) as e:
        return f"rejected: {e}" + _footer()


@tool()
def read_messages() -> str:
    """Your unread messages, across all your conversations. Marks them delivered for this turn."""
    unread = store().unread(_bot(), _seen())
    if not unread:
        return "no unread messages"
    if _run():
        store().add_delivered(_run(), {c: ms[-1].id for c, ms in unread.items()})
    return "\n\n".join(f"## {conv}\n{_render(ms)}" for conv, ms in unread.items())


@tool()
def read_conversation(conversation_id: str, limit: int = 30, before: int | None = None) -> str:
    """Read any conversation: user:<bot>, pair:<a>:<b> (ids sorted), or group:<id>.
    Returns `limit` messages ending just before message id `before` (default: the latest)."""
    conv = store().get_conversation(conversation_id)
    if not conv or (_isolated() and _bot() not in conv["members"]):
        # Isolated mode: only conversations the bot is in. Same answer either way, so a
        # refusal doesn't tell it which other conversations exist.
        return f"no conversation {conversation_id!r}"
    msgs = store().messages(conversation_id, min(limit, 200), before)
    return (_render(msgs) or "(empty)") + _footer()


@tool()
def list_agents() -> str:
    """Every bot: id, name, title. Address them by id."""
    return "\n".join(f"{b['id']}: {b['name']}, {b['title']}" for b in store().roster()) + _footer()


@tool()
def list_groups() -> str:
    """Every group: id, name, members."""
    groups = store().list_groups()
    if _isolated():
        groups = [g for g in groups if _bot() in g["members"]]
    lines = [f"{g['id']}: {g['name']} ({', '.join(g['members'])})" for g in groups]
    return ("\n".join(lines) or "no groups") + _footer()


@tool()
def create_routine(name: str, cron: str, prompt: str) -> str:
    """Schedule yourself: at each cron time (5-field, local time) `prompt` arrives as a message."""
    try:
        rid = store().create_routine(_bot(), name, cron, prompt)
    except ValueError as e:
        return f"rejected: {e}"
    return f"routine {rid} created" + _footer()


@tool()
def list_routines() -> str:
    """Your routines."""
    rows = store().list_routines(_bot())
    lines = [
        f"{r['id']}: {r['name']} [{r['cron']}]{' paused' if r['paused'] else ''}: {r['prompt']}"
        for r in rows
    ]
    return ("\n".join(lines) or "no routines") + _footer()


def _own_routine(routine_id: int) -> dict[str, Any] | str:
    """The caller's routine, or why not: a bot changes only its own."""
    r = next((r for r in store().list_routines() if r["id"] == routine_id), None)
    if r is None:
        return f"rejected: no routine {routine_id}"
    if r["bot_id"] != _bot():
        return f"rejected: routine {routine_id} is {r['bot_id']}'s, not yours"
    return r


def _changed(r: dict[str, Any], verb: str, **fields: Any) -> str:
    try:
        if fields:
            store().update_routine(r["id"], **fields)
        else:
            store().delete_routine(r["id"])
    except ValueError as e:
        return f"rejected: {e}"
    store().post_event(user_conversation(_bot()), f"{verb.capitalize()} routine ⏱ {r['name']}")
    return f"routine {r['id']} {verb}" + _footer()


@tool()
def delete_routine(routine_id: int) -> str:
    """Delete one of your routines for good (pause_routine keeps it)."""
    r = _own_routine(routine_id)
    return r if isinstance(r, str) else _changed(r, "deleted")


@tool()
def pause_routine(routine_id: int) -> str:
    """Stop one of your routines firing until resume_routine."""
    r = _own_routine(routine_id)
    return r if isinstance(r, str) else _changed(r, "paused", paused=1)


@tool()
def resume_routine(routine_id: int) -> str:
    """Let one of your paused routines fire again."""
    r = _own_routine(routine_id)
    return r if isinstance(r, str) else _changed(r, "resumed", paused=0)


@tool()
def update_routine(
    routine_id: int, cron: str | None = None, prompt: str | None = None, name: str | None = None
) -> str:
    """Change one of your routines: any of cron (5-field, local time), prompt, name."""
    r = _own_routine(routine_id)
    if isinstance(r, str):
        return r
    fields = {
        k: v for k, v in (("cron", cron), ("prompt", prompt), ("name", name)) if v is not None
    }
    if not fields:
        return "rejected: give cron, prompt or name to change"
    return _changed(r, "changed", **fields)


def main() -> None:
    global _remote
    if path := os.environ.get("BOTS_MCP_SOCKET"):
        cap_file = os.environ.get("BOT_RUN_CAP_FILE", "")
        try:
            with open(cap_file) as f:
                _remote = Remote(path, f.read().strip())
        except OSError as e:
            raise SystemExit(f"cannot read the run's capability: {e}") from None
    elif not BOT_ID:
        raise SystemExit("BOT_ID is not set")
    server.run("stdio")
