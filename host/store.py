"""Every operation. The only code that touches SQLite.

One connection per thread, WAL, and BEGIN IMMEDIATE around anything that reads then writes."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import CroniterError, croniter

from host.config import Config
from host.kinds import (
    FINAL_STATUSES,
    BotState,
    ConversationKind,
    FinalStatus,
    MessageKind,
)


def local_zone(localtime: str = "/etc/localtime") -> tzinfo | None:
    """The host's IANA zone: TZ if set, else the zoneinfo name /etc/localtime links to.

    None means fall back to `.astimezone()`, a fixed offset that is wrong across a clock
    change. That is also the answer for a TZ that isn't a zone name (POSIX rules like
    "EST5EDT,M3.2.0,M11.1.0"): the process follows TZ, so the link would be the wrong zone.
    """
    name = os.environ.get("TZ")
    if name is None:
        try:
            target = os.readlink(localtime)
        except OSError:
            return None
        _, sep, name = target.partition("zoneinfo/")
        if not sep:
            return None
    try:
        return ZoneInfo(name.removeprefix(":"))
    except (ZoneInfoNotFoundError, ValueError):
        return None


def next_routine_at(cron: str, base: float) -> float:
    """Validate by finding an actual occurrence, using local wall time.

    croniter needs a real zone to cross a clock change: with a fixed offset the first
    routine after it fires an hour early (fall back) or late (spring forward). Across
    the change it acts like cron: a time in the skipped hour fires once when the clock
    jumps past it, and a fixed hour inside the repeated hour fires only the first time
    round (fold=1 is the second); routines on every hour fire in both.
    """
    try:
        zone = local_zone()
        start = (
            datetime.fromtimestamp(base, tz=zone)
            if zone
            else datetime.fromtimestamp(base).astimezone()
        )
        it = croniter(cron, start)
        nxt = it.get_next(datetime)
        while nxt.fold and it.expanded[1] != ["*"]:
            nxt = it.get_next(datetime)
        return nxt.timestamp()
    except (CroniterError, ValueError, TypeError, OverflowError) as exc:
        raise ValueError(f"bad cron {cron!r}: no reachable next occurrence") from exc


USER = "user"
HOST = "host"

# Cursors advance on ok, and on killed: Stop drops the turn, and only a new message wakes
# the bot again. failed, rate_limited and interrupted leave them, so the turn is redone
# (failed and rate_limited once retry_after has passed).
ADVANCES: tuple[FinalStatus, ...] = ("ok", "killed")

# Connections the MCP socket's short-lived threads share (Store.borrowing). SQLite can't close
# a connection's files while another connection in the process holds a lock on them, and in
# WAL mode the host's long-lived ones always do: every connection the host ever had open at
# once keeps its files for the life of the process. The pool caps what a burst can add.
POOL = 8

_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
NAME_MAX = 60
BUDGET_HINT = "or raise daily_budget in config.toml and restart the host"

SCHEMA = """
CREATE TABLE IF NOT EXISTS bots (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    engine TEXT NOT NULL,         -- claude | codex | fake | a plugin's (host/engines)
    model TEXT,                   -- NULL: the engine CLI's own default
    paused INTEGER NOT NULL DEFAULT 0 CHECK (paused IN (0, 1)),
    archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1)),
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,          -- user:<bot> | pair:<a>:<b> | group:<id>
    kind TEXT NOT NULL CHECK (kind IN ('user', 'pair', 'group')),
    name TEXT NOT NULL DEFAULT '',
    members TEXT NOT NULL,        -- JSON list of bot ids; I am implicitly in user: and group:
    archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1)),  -- wakes no one, accepts nothing
    created_at REAL NOT NULL,
    CHECK (id LIKE kind || ':%')
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    sender TEXT NOT NULL,         -- user | <bot id> | routine:<id> | host
    recipients TEXT NOT NULL,     -- JSON list of bot ids and/or 'user', computed at insert
    body TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'text' CHECK (kind IN ('text', 'event')),
    card TEXT,                    -- JSON: {"ask": [...]} on a question
    nonce TEXT UNIQUE,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_conversation ON messages (conversation_id, id);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id TEXT NOT NULL,
    session_id TEXT,
    started_at REAL NOT NULL,
    ended_at REAL,
    status TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'ok', 'failed', 'rate_limited', 'killed', 'interrupted')),
    cost REAL,
    last_event_at REAL,
    open_tool TEXT,               -- "name: summary" of the tool call awaiting a result
    pgid INTEGER,                 -- the engine's process group, for stop and crash recovery
    stop_requested INTEGER NOT NULL DEFAULT 0 CHECK (stop_requested IN (0, 1)),  -- `bots stop`
    retry_after REAL,             -- failed, rate_limited: not due before this time
    delivered TEXT NOT NULL DEFAULT '{}',  -- JSON {conversation: high-water id}; cursors on ok
    error TEXT,
    CHECK ((status = 'running') = (ended_at IS NULL)),
    CHECK (retry_after IS NULL OR status IN ('failed', 'rate_limited'))
);
CREATE INDEX IF NOT EXISTS runs_bot ON runs (bot_id, id);
CREATE TABLE IF NOT EXISTS cursors (
    recipient TEXT NOT NULL,      -- a bot id, or 'user'
    conversation_id TEXT NOT NULL,
    last_id INTEGER NOT NULL,
    PRIMARY KEY (recipient, conversation_id)
);
CREATE TABLE IF NOT EXISTS routines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id TEXT NOT NULL,
    name TEXT NOT NULL,
    cron TEXT NOT NULL,           -- local time
    prompt TEXT NOT NULL,
    paused INTEGER NOT NULL DEFAULT 0 CHECK (paused IN (0, 1)),
    last_fired_at REAL,
    created_at REAL NOT NULL
);
"""


# A bot's question with options, rendered under the message; the user answers in plain text.
ASK_QUESTIONS = (1, 4)
ASK_OPTIONS = (2, 6)
ASK_Q_MAX, ASK_LABEL_MAX, ASK_DETAIL_MAX = 200, 60, 120
_QUESTION_KEYS, _OPTION_KEYS = {"q", "options", "multi"}, {"label", "detail", "recommended"}


def _text(value: Any, what: str, most: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{what} must be a string")
    text = " ".join(value.split())
    if not text:
        raise ValueError(f"{what} is empty")
    if len(text) > most:
        raise ValueError(f"{what} is over {most} characters ({len(text)})")
    return text


def _unknown(item: dict[str, Any], known: set[str], what: str) -> None:
    if extra := sorted(set(item) - known):
        raise ValueError(f"{what} has unknown field {extra[0]!r}; use {', '.join(sorted(known))}")


def _check_option(option: Any, at: str, where: str, seen: set[str]) -> dict[str, Any]:
    """One option tidied; `seen` holds the question's labels so far, for repeats."""
    if not isinstance(option, dict):
        raise TypeError(f"{at} must be an object with a label")
    _unknown(option, _OPTION_KEYS, at)
    label = _text(option.get("label"), f"{at} label", ASK_LABEL_MAX)
    key = label.casefold().rstrip(".…:!? ")
    if key in seen:
        raise ValueError(f"{where} offers {label!r} twice")
    seen.add(key)
    out: dict[str, Any] = {"label": label}
    if option.get("detail") not in (None, ""):
        out["detail"] = _text(option["detail"], f"{at} detail", ASK_DETAIL_MAX)
    recommended = option.get("recommended", False)
    if not isinstance(recommended, bool):
        raise TypeError(f"{at} recommended must be true or false")
    if recommended:
        out["recommended"] = True
    return out


def check_ask(ask: Any) -> list[dict[str, Any]]:
    """A bot's `ask` tidied (whitespace collapsed, defaults filled), or ValueError/TypeError
    saying what is wrong, worded for the bot that sent it."""
    if not isinstance(ask, list):
        raise TypeError("ask must be a list of questions")
    low, high = ASK_QUESTIONS
    if not low <= len(ask) <= high:
        raise ValueError(f"ask needs {low} to {high} questions, not {len(ask)}")
    questions: list[dict[str, Any]] = []
    for i, item in enumerate(ask, 1):
        where = f"question {i}"
        if not isinstance(item, dict):
            raise TypeError(f"{where} must be an object with q and options")
        _unknown(item, _QUESTION_KEYS, where)
        q = _text(item.get("q"), f"{where} q", ASK_Q_MAX)
        multi = item.get("multi", False)
        if not isinstance(multi, bool):
            raise TypeError(f"{where} multi must be true or false")
        options = item.get("options")
        if not isinstance(options, list):
            raise TypeError(f"{where} options must be a list")
        low, high = ASK_OPTIONS
        if not low <= len(options) <= high:
            raise ValueError(f"{where} needs {low} to {high} options, not {len(options)}")
        tidy: list[dict[str, Any]] = []
        seen: set[str] = set()
        for j, option in enumerate(options, 1):
            tidy.append(_check_option(option, f"{where} option {j}", where, seen))
        if sum("recommended" in o for o in tidy) > 1:
            raise ValueError(f"{where} recommends more than one option")
        questions.append({"q": q, "options": tidy, "multi": multi})
    return questions


def render_ask(card: dict[str, Any] | None) -> str:
    """A question's options as bots read them back: one line per question after the body."""
    if not card or "ask" not in card:
        return ""
    lines = []
    for question in card["ask"]:
        options = " | ".join(
            f"{chr(65 + i)}) {o['label']}{' (recommended)' if o.get('recommended') else ''}"
            for i, o in enumerate(question["options"])
        )
        pick = " (pick any)" if question.get("multi") else ""
        lines.append(f"\n[ask: {question['q']}{pick}: {options}]")
    return "".join(lines)


class Rejected(Exception):
    """A send the host refuses. The message is shown to the bot as the tool's error."""


class Held(Rejected):
    """A group send held back because others spoke first; carries what the bot missed."""

    def __init__(self, missed: list[Message]) -> None:
        super().__init__("others posted while you were working")
        self.missed = missed


@dataclass(frozen=True)
class Message:
    id: int
    conversation_id: str
    sender: str
    recipients: list[str]
    body: str
    kind: MessageKind
    created_at: float
    card: dict[str, Any] | None = None


def user_conversation(bot_id: str) -> str:
    return f"user:{bot_id}"


def pair_conversation(a: str, b: str) -> str:
    x, y = sorted((a, b))
    return f"pair:{x}:{y}"


def group_conversation(group_id: str) -> str:
    return f"group:{group_id}"


def _model(value: Any) -> str | None:
    """A bot's model: a name passed to its CLI as is, or None (blank clears it) for the default."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("model must be a string")
    return value.strip() or None


def _engine(value: Any) -> str:
    """A bot's engine name. Which ones are loaded depends on config.toml's plugins, so the
    CLI checks that (host/engines engine_names); the store only refuses junk."""
    if not isinstance(value, str) or not _SLUG.match(value):
        raise ValueError(f"bad engine name {value!r}")
    return value


def _flag(value: Any, what: str) -> int:
    """A 0/1 column: true, false, 0 or 1 in, 0 or 1 out (the column's CHECK takes nothing else)."""
    if value not in (0, 1) or not isinstance(value, (bool, int)):
        raise TypeError(f"{what} must be true or false")
    return int(value)


def _name(value: Any, what: str = "name") -> str:
    """A display name: whitespace collapsed, non-empty, at most NAME_MAX characters."""
    if not isinstance(value, str):
        raise TypeError(f"{what} must be a string")
    name = " ".join(value.split())
    if not name:
        raise ValueError(f"{what} is required")
    if len(name) > NAME_MAX:
        raise ValueError(f"{what} is longer than {NAME_MAX} characters")
    return name


def _message(row: sqlite3.Row) -> Message:
    return Message(
        id=row["id"],
        conversation_id=row["conversation_id"],
        sender=row["sender"],
        recipients=json.loads(row["recipients"]),
        body=row["body"],
        kind=row["kind"],
        created_at=row["created_at"],
        card=json.loads(row["card"]) if row["card"] else None,
    )


class Store:
    def __init__(
        self, path: Path | str, daily_budget: float = 0.0, group_sends_per_turn: int = 3
    ) -> None:
        self.path = str(path)
        self.daily_budget = daily_budget
        self.group_sends_per_turn = group_sends_per_turn
        self._local = threading.local()
        self._slots = threading.BoundedSemaphore(POOL)
        self._idle: list[sqlite3.Connection] = []
        self._idle_lock = threading.Lock()
        self.db.executescript(SCHEMA)

    # -- plumbing --------------------------------------------------------

    @property
    def db(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._borrow() if getattr(self._local, "borrowing", False) else self._open()
            self._local.conn = conn
        return conn

    def _open(self) -> sqlite3.Connection:
        # check_same_thread off: a pooled connection moves between threads, one at a time.
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _borrow(self) -> sqlite3.Connection:
        if not self._slots.acquire(timeout=30):  # the same patience as a busy database
            raise sqlite3.OperationalError(f"all {POOL} request connections are busy")
        try:
            with self._idle_lock:
                if self._idle:
                    return self._idle.pop()
            return self._open()
        except BaseException:
            self._slots.release()
            raise

    def close_thread(self) -> None:
        """Close this thread's connection. Threads that end must call it: nothing else does."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    @contextmanager
    def borrowing(self) -> Iterator[None]:
        """For short-lived threads. A connection this thread takes comes from the POOL and goes
        back at the end, so a burst waits its turn for one instead of opening one each.
        Long-lived threads (turns) keep their own."""
        self._local.borrowing = True
        try:
            yield
        finally:
            self._local.borrowing = False
            conn, self._local.conn = getattr(self._local, "conn", None), None
            if conn is not None:
                try:
                    if conn.in_transaction:  # _tx rolls back; this is only a backstop
                        conn.rollback()
                except sqlite3.Error:
                    conn.close()
                else:
                    with self._idle_lock:
                        self._idle.append(conn)
                finally:
                    self._slots.release()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        db = self.db
        if db.in_transaction:  # nested: join the outer transaction
            yield db
            return
        db.execute("BEGIN IMMEDIATE")
        try:
            yield db
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")

    # -- bots ------------------------------------------------------------

    def create_bot(
        self,
        bot_id: str,
        name: str,
        title: str = "",
        engine: str = "claude",
        model: str | None = None,
    ) -> str:
        """Blank id: the host picks the next free bot-<n>. Ids never follow names:
        an id is in every stored message."""
        if bot_id and (not _SLUG.match(bot_id) or bot_id in (USER, HOST)):
            raise ValueError(f"bad bot id {bot_id!r}: lowercase letters, digits, - and _")
        engine = _engine(engine)
        model = _model(model)
        name = _name(name)
        now = time.time()
        with self._tx() as db:
            bot_id = bot_id or self._next_bot_id(db)
            if db.execute("SELECT 1 FROM bots WHERE id = ?", (bot_id,)).fetchone():
                raise ValueError(f"bot {bot_id!r} exists")
            if db.execute(
                "SELECT 1 FROM conversations WHERE id = ?", (group_conversation(bot_id),)
            ).fetchone():
                raise ValueError(f"{bot_id!r} is a group")
            self._check_name_free(name, bot_id)
            db.execute(
                "INSERT INTO bots (id, name, title, engine, model, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (bot_id, name, title, engine, model, now),
            )
            self._ensure_conversation(user_conversation(bot_id), "user", [bot_id])
            self.post_event(user_conversation(bot_id), f"created {name} ({engine})")
        return bot_id

    @staticmethod
    def _next_bot_id(db: sqlite3.Connection) -> str:
        """One past the highest bot-<n>, archived bots included, so an id is never reused."""
        rows = db.execute("SELECT id FROM bots WHERE id GLOB 'bot-[0-9]*'").fetchall()
        taken = [int(r[0][4:]) for r in rows if r[0][4:].isdigit()]
        return f"bot-{max(taken, default=0) + 1}"

    def get_bot(self, bot_id: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM bots WHERE id = ?", (bot_id,)).fetchone()
        return dict(row) if row else None

    def _active_bot(self, bot_id: str) -> dict[str, Any]:
        bot = self.get_bot(bot_id)
        if not bot or bot["archived"]:
            raise ValueError(f"no bot {bot_id!r}")
        return bot

    def list_bots(self, include_archived: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM bots" + ("" if include_archived else " WHERE archived = 0")
        return [dict(r) for r in self.db.execute(sql + " ORDER BY created_at")]

    def update_bot(self, bot_id: str, **fields: Any) -> None:
        allowed = {"model", "paused"}
        if not fields.keys() <= allowed:
            raise ValueError(f"cannot update {set(fields) - allowed}")
        if "model" in fields:
            fields["model"] = _model(fields["model"])
        if "paused" in fields:
            fields["paused"] = _flag(fields["paused"], "paused")
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self._tx() as db:
            if self.get_bot(bot_id) is None:
                raise ValueError(f"no bot {bot_id!r}")
            db.execute(f"UPDATE bots SET {sets} WHERE id = ?", (*fields.values(), bot_id))

    def _check_name_free(self, name: str, bot_id: str) -> None:
        """Bot names are unique, ignoring case, so @Name is unambiguous.
        Archived bots count too: restoring one must not bring back a duplicate."""
        for row in self.db.execute("SELECT id, name FROM bots WHERE id != ?", (bot_id,)):
            if row["name"].casefold() == name.casefold():
                raise ValueError(f"name {name!r} is taken by {row['id']}")

    def roster(self) -> list[dict[str, Any]]:
        """Active bots as other bots see them: id, name, title."""
        return [{"id": b["id"], "name": b["name"], "title": b["title"]} for b in self.list_bots()]

    def bot_state(self, bot_id: str) -> BotState:
        """working | queued | over_budget | idle. Derived, never stored.

        queued: due, but every run slot is taken; it starts when one frees up.
        """
        last = self.last_run(bot_id)
        if last and last["status"] == "running":
            return "working"
        bot = self.get_bot(bot_id)
        if bot and self._due(bot, time.time(), budget=False):
            return "over_budget" if self.over_budget() else "queued"
        return "idle"

    # -- budget ----------------------------------------------------------

    # One budget for all bots together. Engines that report no cost (Codex) spend nothing.

    def spent_today(self, bot_id: str | None = None, now: float | None = None) -> float:
        """Engine-reported cost of runs since local midnight: one bot's, or every bot's.

        A run is charged when it ends (engines report cost only then), so one in flight at
        midnight counts against the new day."""
        day = datetime.fromtimestamp(now or time.time())
        midnight = day.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        sql = "SELECT COALESCE(SUM(cost), 0) FROM runs WHERE (ended_at IS NULL OR ended_at >= ?)"
        args: tuple[Any, ...] = (midnight,)
        if bot_id:
            sql, args = sql + " AND bot_id = ?", (*args, bot_id)
        return float(self.db.execute(sql, args).fetchone()[0])

    def over_budget(self, now: float | None = None) -> bool:
        return self.daily_budget > 0 and self.spent_today(now=now) >= self.daily_budget

    # -- conversations ---------------------------------------------------

    def _ensure_conversation(
        self, conv_id: str, kind: ConversationKind, members: list[str], name: str = ""
    ) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO conversations (id, kind, name, members, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (conv_id, kind, name, json.dumps(members), time.time()),
        )

    def create_group(self, group_id: str, name: str, members: list[str]) -> str:
        if not _SLUG.match(group_id):
            raise ValueError(f"bad group id {group_id!r}")
        self._check_members(members)
        name = _name(name or group_id, "group name")
        conv_id = group_conversation(group_id)
        with self._tx() as db:
            if db.execute("SELECT 1 FROM conversations WHERE id = ?", (conv_id,)).fetchone():
                raise ValueError(f"group {group_id!r} exists")
            if db.execute("SELECT 1 FROM bots WHERE id = ?", (group_id,)).fetchone():
                raise ValueError(f"{group_id!r} is a bot")
            self._ensure_conversation(conv_id, "group", sorted(set(members)), name)
            self.post_event(conv_id, f"group {name} created: {', '.join(sorted(set(members)))}")
        return conv_id

    def _check_members(self, members: list[str]) -> None:
        """A group's members: at least two, all active bots."""
        if unknown := set(members) - {b["id"] for b in self.list_bots()}:
            raise ValueError(f"unknown bots: {sorted(unknown)}")
        if len(set(members)) < 2:
            raise ValueError("a group needs at least two bots")

    def archive_bot(self, bot_id: str) -> None:
        """Removing a bot: out of the roster and every group, routines paused, its chat with the
        user closed. Folder and messages are kept. Stop its run first."""
        self._active_bot(bot_id)
        with self._tx() as db:
            db.execute("UPDATE bots SET archived = 1 WHERE id = ?", (bot_id,))
            db.execute("UPDATE routines SET paused = 1 WHERE bot_id = ?", (bot_id,))
            for group in self.list_groups():
                if bot_id in group["members"]:
                    rest = [b for b in group["members"] if b != bot_id]
                    db.execute(
                        "UPDATE conversations SET members = ? WHERE id = ?",
                        (json.dumps(rest), group["id"]),
                    )
                    self.post_event(group["id"], f"{bot_id} left (archived)")
            self.post_event(user_conversation(bot_id), "archived")
            self.archive_conversation(user_conversation(bot_id))

    def unarchive_bot(self, bot_id: str) -> None:
        """Bring an archived bot back: in the roster, its chat with the user open again. It does
        not rejoin groups and its routines stay paused."""
        bot = self.get_bot(bot_id)
        if not bot or not bot["archived"]:
            raise ValueError(f"no archived bot {bot_id!r}")
        conv_id = user_conversation(bot_id)
        with self._tx() as db:
            db.execute("UPDATE bots SET archived = 0 WHERE id = ?", (bot_id,))
            db.execute("UPDATE conversations SET archived = 0 WHERE id = ?", (conv_id,))
            self.post_event(conv_id, "restored (routines stay paused)")

    def archive_conversation(self, conv_id: str) -> None:
        with self._tx() as db:
            db.execute("UPDATE conversations SET archived = 1 WHERE id = ?", (conv_id,))

    def get_conversation(self, conv_id: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM conversations WHERE id = ?", (conv_id,)).fetchone()
        if not row:
            return None
        return {**dict(row), "members": json.loads(row["members"])}

    def list_groups(self) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT * FROM conversations WHERE kind = 'group' AND archived = 0 ORDER BY created_at"
        )
        return [{**dict(r), "members": json.loads(r["members"])} for r in rows]

    def conversations_of(self, bot_id: str) -> list[str]:
        rows = self.db.execute(
            """SELECT c.id FROM conversations c, json_each(c.members) m
               WHERE m.value = ? AND c.archived = 0 ORDER BY c.created_at""",
            (bot_id,),
        )
        return [r["id"] for r in rows]

    # -- messages --------------------------------------------------------

    def _recipients(
        self, conv: dict[str, Any], sender: str, body: str, kind: MessageKind
    ) -> list[str]:
        if kind == "event":
            return [USER]
        members: list[str] = conv["members"]
        if conv["kind"] == "user":
            return [USER] if sender == members[0] else [members[0]]
        if conv["kind"] == "pair":
            return [m for m in members if m != sender]
        said = self._mentioned(members, body)
        named = [m for m in members if m in said and m != sender]
        if sender == USER:
            return named or members
        return [USER, *named]

    def _mentioned(self, members: list[str], body: str) -> set[str]:
        """Members @-mentioned in body by id or by name, as ids. Only a one-word name that
        no other member has as its name or id counts, so @Name can never pick the wrong bot.
        Each @ resolves to one member: the longest id or name it spells, so @Ada.Lovelace
        means the bot named that, not also the bot whose id is ada."""
        targets = {m: m for m in members}
        names: dict[str, list[str]] = {}
        spellings: dict[str, str] = {}  # name as written -> its casefolded key
        for bot_id in members:
            bot = self.get_bot(bot_id)
            if bot and len(bot["name"].split()) == 1:
                names.setdefault(bot["name"].casefold(), []).append(bot_id)
                spellings[bot["name"]] = bot["name"].casefold()
        for key, ids in names.items():
            if len(ids) == 1 and key not in members:
                targets[key] = ids[0]
        if not targets:
            return set()
        # Casefolding can respell a name (Straße -> strasse) in a way IGNORECASE does not
        # undo, so the regex also needs each name as written; lookup stays casefolded.
        written = {s for s, key in spellings.items() if key in targets}
        # longest first: alternation is ordered
        words = sorted(set(targets) | written, key=len, reverse=True)
        alternation = "|".join(re.escape(w) for w in words)
        pattern = re.compile(r"(?<![\w.])@(" + alternation + r")(?![\w-])", re.IGNORECASE)
        found = (targets.get(m.group(1).casefold()) for m in pattern.finditer(body))
        return {bot_id for bot_id in found if bot_id}

    def post(
        self,
        conv_id: str,
        sender: str,
        body: str,
        kind: MessageKind = "text",
        nonce: str | None = None,
        card: dict[str, Any] | None = None,
    ) -> Message | None:
        """Insert a message with its recipients computed. None if the nonce was seen before."""
        with self._tx() as db:
            conv = self.get_conversation(conv_id)
            if conv is None:
                raise ValueError(f"no conversation {conv_id!r}")
            if nonce and db.execute("SELECT 1 FROM messages WHERE nonce = ?", (nonce,)).fetchone():
                return None
            recipients = self._recipients(conv, sender, body, kind)
            cur = db.execute(
                """INSERT INTO messages
                   (conversation_id, sender, recipients, body, kind, card, nonce, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    conv_id,
                    sender,
                    json.dumps(recipients),
                    body,
                    kind,
                    json.dumps(card) if card is not None else None,
                    nonce,
                    time.time(),
                ),
            )
            return self.get_message(int(cur.lastrowid or 0))

    def post_event(self, conv_id: str, body: str) -> Message | None:
        return self.post(conv_id, HOST, body, kind="event")

    def get_message(self, message_id: int) -> Message | None:
        row = self.db.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        return _message(row) if row else None

    def messages(
        self,
        conv_id: str,
        limit: int = 50,
        before: int | None = None,
        hide_routines: bool = False,
        after: int | None = None,
    ) -> list[Message]:
        """A window of `limit` messages ending just before `before` (or at the latest), oldest first.

        hide_routines leaves out routine:<id> triggers, in SQL so they do not eat the window.
        Only the terminal asks for that: a bot must still see the trigger that woke it.
        after (0 included) keeps only ids past it (`bots read`, `bots chat`), so the window is the
        oldest `limit` of those: page forward by passing the last id back until a page comes up short.
        """
        where = " AND sender NOT LIKE 'routine:%'" if hide_routines else ""
        params: list[Any] = [conv_id, before if before is not None else 2**62]
        if after is not None:
            where += " AND id > ?"
            params.append(after)
        order = "DESC" if after is None else "ASC"
        rows = self.db.execute(
            f"""SELECT * FROM messages WHERE conversation_id = ? AND id < ?{where}
               ORDER BY id {order} LIMIT ?""",
            (*params, limit),
        ).fetchall()
        return [_message(r) for r in (reversed(rows) if after is None else rows)]

    def last_message_id(self) -> int:
        return int(self.db.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()[0])

    def messages_after(self, after: int, limit: int) -> list[Message]:
        """Across every conversation, oldest first."""
        rows = self.db.execute(
            "SELECT * FROM messages WHERE id > ? ORDER BY id LIMIT ?", (after, limit)
        ).fetchall()
        return [_message(r) for r in rows]

    def moved_since(
        self, bot_id: str, since: float, seen: dict[str, int] | None = None
    ) -> dict[str, int]:
        """Per conversation, how many texts others posted after `since` that the bot was not
        given (`seen`: high-water ids already delivered). For the envelope. Events are left
        out: they are addressed to the user, and most echo a send the bot can already see."""
        seen = seen or {}
        rows = self.db.execute(
            "SELECT conversation_id, id FROM messages WHERE created_at > ? AND sender != ?"
            " AND kind = 'text' ORDER BY id",
            (since, bot_id),
        )
        moved: dict[str, int] = {}
        for r in rows:
            if r["id"] > seen.get(r["conversation_id"], 0):
                moved[r["conversation_id"]] = moved.get(r["conversation_id"], 0) + 1
        return moved

    def user_send(self, conv_id: str, body: str) -> Message:
        """A message from the user. Reading a conversation just written in marks it read."""
        conv = self.get_conversation(conv_id)
        if conv is None or conv["kind"] == "pair" or conv["archived"]:
            raise ValueError(f"cannot send to {conv_id!r}")
        msg = self.post(conv_id, USER, body)
        assert msg is not None
        self.mark_read(USER, conv_id, msg.id)
        return msg

    # -- sends from bots (the rules the host enforces) -------------------

    def send_to_user(
        self, bot_id: str, body: str, nonce: str | None = None, ask: Any = None
    ) -> Message | None:
        card = {"ask": check_ask(ask)} if ask is not None else None
        return self.post(user_conversation(bot_id), bot_id, body, nonce=nonce, card=card)

    def send_to_agent(
        self, bot_id: str, target: str, body: str, nonce: str | None = None
    ) -> Message | None:
        if target == bot_id or target not in {b["id"] for b in self.list_bots()}:
            raise Rejected(f"unknown agent {target!r}; use list_agents for ids")
        conv_id = pair_conversation(bot_id, target)
        with self._tx() as db:
            if nonce and db.execute("SELECT 1 FROM messages WHERE nonce = ?", (nonce,)).fetchone():
                return None
            self._ensure_conversation(conv_id, "pair", sorted((bot_id, target)))
            msg = self.post(conv_id, bot_id, body, nonce=nonce)
            if msg:
                first = body.strip().splitlines()[0] if body.strip() else ""
                self.post_event(user_conversation(bot_id), f"-> {target}: {first[:120]}")
        return msg

    def send_to_group(
        self,
        bot_id: str,
        group_id: str,
        body: str,
        run_id: int | None,
        nonce: str | None = None,
        ask: Any = None,
    ) -> Message | None:
        """Post in a group. Held, not posted, if someone spoke there since the bot last read:
        bots in a group work in parallel, and this is how the second one avoids repeating
        the first. The missed messages count as delivered, so posting again goes through."""
        card = {"ask": check_ask(ask)} if ask is not None else None
        conv_id = group_id if group_id.startswith("group:") else group_conversation(group_id)
        conv = self.get_conversation(conv_id)
        if conv is None or conv["archived"] or bot_id not in conv["members"]:
            raise Rejected(f"you are not in group {group_id!r}; use list_groups")
        run = self.get_run(run_id) if run_id else None
        started = run["started_at"] if run else 0.0
        seen = max(self.cursor(bot_id, conv_id), run["delivered"].get(conv_id, 0) if run else 0)
        with self._tx() as db:
            if nonce and db.execute("SELECT 1 FROM messages WHERE nonce = ?", (nonce,)).fetchone():
                return None
            sent = db.execute(
                "SELECT COUNT(*) FROM messages WHERE conversation_id = ? AND sender = ?"
                " AND created_at >= ?",
                (conv_id, bot_id, started),
            ).fetchone()[0]
            if sent >= self.group_sends_per_turn:
                raise Rejected(f"at most {self.group_sends_per_turn} group messages per turn")
            missed = [
                _message(r)
                for r in db.execute(
                    "SELECT * FROM messages WHERE conversation_id = ? AND id > ? AND sender != ?"
                    " AND kind = 'text' ORDER BY id",
                    (conv_id, seen, bot_id),
                )
            ]
            if not run or not missed:
                return self.post(conv_id, bot_id, body, nonce=nonce, card=card)
        self.add_delivered(run["id"], {conv_id: missed[-1].id})
        raise Held(missed)

    # -- cursors, unread -------------------------------------------------

    def cursor(self, recipient: str, conv_id: str) -> int:
        row = self.db.execute(
            "SELECT last_id FROM cursors WHERE recipient = ? AND conversation_id = ?",
            (recipient, conv_id),
        ).fetchone()
        return int(row["last_id"]) if row else 0

    def mark_read(self, recipient: str, conv_id: str, upto: int | None = None) -> None:
        """Advance a cursor (never backwards). upto=None means the latest message."""
        with self._tx() as db:
            if upto is None:
                upto = db.execute(
                    "SELECT COALESCE(MAX(id), 0) FROM messages WHERE conversation_id = ?",
                    (conv_id,),
                ).fetchone()[0]
            db.execute(
                """INSERT INTO cursors (recipient, conversation_id, last_id) VALUES (?, ?, ?)
                   ON CONFLICT (recipient, conversation_id)
                   DO UPDATE SET last_id = MAX(last_id, excluded.last_id)""",
                (recipient, conv_id, upto),
            )

    def unread(self, bot_id: str, seen: dict[str, int] | None = None) -> dict[str, list[Message]]:
        """Everything after the bot's cursor in each of its conversations, not sent by itself.

        `seen` overrides cursors with what the current run was already given.
        """
        seen = seen or {}
        out: dict[str, list[Message]] = {}
        for conv_id in self.conversations_of(bot_id):
            after = max(self.cursor(bot_id, conv_id), seen.get(conv_id, 0))
            rows = self.db.execute(
                "SELECT * FROM messages WHERE conversation_id = ? AND id > ? AND sender != ?"
                " ORDER BY id",
                (conv_id, after, bot_id),
            ).fetchall()
            if rows:
                out[conv_id] = [_message(r) for r in rows]
        return out

    def unread_count(self, bot_id: str, seen: dict[str, int] | None = None) -> int:
        return sum(len(v) for v in self.unread(bot_id, seen).values())

    def _addressed_unread(self, recipient: str) -> list[Message]:
        """Unread messages with `recipient` in their to-list, in live conversations it is in.

        Membership matters: a bot removed from a group must not stay due on messages
        there that its turn would never deliver.
        """
        rows = self.db.execute(
            """SELECT m.* FROM messages m, json_each(m.recipients) r
               JOIN conversations v ON v.id = m.conversation_id AND v.archived = 0
               LEFT JOIN cursors c ON c.recipient = ? AND c.conversation_id = m.conversation_id
               WHERE r.value = ? AND m.id > COALESCE(c.last_id, 0)
               AND (r.value = 'user' OR EXISTS (
                   SELECT 1 FROM json_each(v.members) x WHERE x.value = r.value))
               ORDER BY m.id""",
            (recipient, recipient),
        ).fetchall()
        return [_message(r) for r in rows]

    def user_unread_counts(self) -> dict[str, int]:
        """Per conversation, bot texts the user has not seen. Host events and group posts that
        @-mention another bot do not count."""
        counts: dict[str, int] = {}
        for m in self._addressed_unread(USER):
            if m.kind == "text" and m.recipients == [USER]:
                counts[m.conversation_id] = counts.get(m.conversation_id, 0) + 1
        return counts

    # -- runs ------------------------------------------------------------

    def start_run(self, bot_id: str) -> int:
        now = time.time()
        with self._tx() as db:
            if self.running_run(bot_id):
                raise RuntimeError(f"{bot_id} already has a run")
            cur = db.execute(
                "INSERT INTO runs (bot_id, started_at, last_event_at) VALUES (?, ?, ?)",
                (bot_id, now, now),
            )
            return int(cur.lastrowid or 0)

    def get_run(self, run_id: int) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return {**dict(row), "delivered": json.loads(row["delivered"])} if row else None

    def last_run(self, bot_id: str) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT id FROM runs WHERE bot_id = ? ORDER BY id DESC LIMIT 1", (bot_id,)
        ).fetchone()
        return self.get_run(row["id"]) if row else None

    def running_run(self, bot_id: str) -> dict[str, Any] | None:
        last = self.last_run(bot_id)
        return last if last and last["status"] == "running" else None

    def running_runs(self) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT id FROM runs WHERE status = 'running' ORDER BY id")
        return [r for r in (self.get_run(row["id"]) for row in rows.fetchall()) if r]

    def list_runs(self, bot_id: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        """Newest first: one bot's, or every bot's."""
        where, args = ("WHERE bot_id = ?", (bot_id,)) if bot_id else ("", ())
        rows = self.db.execute(
            f"SELECT id FROM runs {where} ORDER BY id DESC LIMIT ?", (*args, limit)
        ).fetchall()
        return [r for r in (self.get_run(row["id"]) for row in rows) if r]

    def request_stop(self, bot_id: str) -> int | None:
        """Ask the host to kill the bot's live run; the id of that run, or None if it has none.
        The scheduler owns the process, so the mark in the database is how anyone else asks."""
        with self._tx() as db:
            run = self.running_run(bot_id)
            if run is None:
                return None
            db.execute("UPDATE runs SET stop_requested = 1 WHERE id = ?", (run["id"],))
            return int(run["id"])

    def update_run(self, run_id: int, **fields: Any) -> None:
        allowed = {
            "session_id",
            "cost",
            "last_event_at",
            "open_tool",
            "pgid",
            "retry_after",
            "error",
        }
        if not fields.keys() <= allowed:
            raise ValueError(f"cannot update {set(fields) - allowed}")
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self._tx() as db:
            db.execute(f"UPDATE runs SET {sets} WHERE id = ?", (*fields.values(), run_id))

    def add_delivered(self, run_id: int, high_water: dict[str, int]) -> None:
        """Record what this run has been given; cursors move there only if the run ends ok."""
        with self._tx() as db:
            run = self.get_run(run_id)
            assert run is not None
            merged = dict(run["delivered"])
            for conv_id, mid in high_water.items():
                merged[conv_id] = max(merged.get(conv_id, 0), mid)
            db.execute("UPDATE runs SET delivered = ? WHERE id = ?", (json.dumps(merged), run_id))

    def failures_in_a_row(self, bot_id: str) -> int:
        """Failed runs since the last one that was not, for retry backoff."""
        n = 0
        for run in self.list_runs(bot_id, 50):
            if run["status"] == "running":
                continue
            if run["status"] != "failed":
                break
            n += 1
        return n

    def finish_run(
        self,
        run_id: int,
        status: FinalStatus,
        error: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        """End a run. ok and killed advance the bot's cursors to everything it was delivered.
        The run that takes the bots over the daily budget says so in its bot's chat, once."""
        if status not in FINAL_STATUSES:  # a typed caller can't get here; the CLI and tests can
            raise ValueError(f"bad final status {status!r}")
        with self._tx() as db:
            run = self.get_run(run_id)
            assert run is not None
            if run["status"] != "running":
                return  # already finished (e.g. stopped and reaped)
            db.execute(
                "UPDATE runs SET status = ?, ended_at = ?, error = ?, retry_after = ?,"
                " open_tool = NULL WHERE id = ?",
                (status, time.time(), error, retry_after, run_id),
            )
            if status in ADVANCES:
                for conv_id, mid in run["delivered"].items():
                    self.mark_read(run["bot_id"], conv_id, mid)
            spent = self.spent_today()
            if self.daily_budget > 0 and spent - (run["cost"] or 0) < self.daily_budget <= spent:
                self.post_event(
                    user_conversation(run["bot_id"]),
                    f"daily budget of ${self.daily_budget:.2f} reached;"
                    f" every bot resumes at midnight ({BUDGET_HINT})",
                )

    def due_bots(self, now: float | None = None) -> list[str]:
        """Bots that should run now, in priority order: oldest unread from the user, then from a
        bot, then from a routine; oldest first within each."""
        now = now or time.time()
        keys = {b["id"]: self._due(b, now) for b in self.list_bots()}
        return sorted((b for b, k in keys.items() if k), key=lambda b: keys[b] or (0, 0))

    def activity(self, now: float | None = None) -> tuple[list[str], list[dict[str, Any]]]:
        """Due bots and live runs from one snapshot: a turn handing work on between two
        separate reads would look like a settled team."""
        db = self.db
        db.execute("BEGIN")
        try:
            return self.due_bots(now), self.running_runs()
        finally:
            db.execute("COMMIT")

    def stalled(self, now: float | None = None) -> list[tuple[str, float | None]]:
        """Bots with work they won't start yet: (id, when it retries), None if paused."""
        now = now or time.time()
        found: list[tuple[str, float | None]] = []
        for bot in self.list_bots():
            if not self._work(bot["id"]):
                continue
            last = self.last_run(bot["id"])
            retry = last["retry_after"] if last else None
            if bot["paused"]:
                found.append((bot["id"], None))
            elif retry and retry > now:
                found.append((bot["id"], retry))
        return found

    def _work(self, bot_id: str) -> list[Message]:
        return [m for m in self._addressed_unread(bot_id) if m.sender != bot_id]

    def _due(self, bot: dict[str, Any], now: float, budget: bool = True) -> tuple[int, int] | None:
        """(lane, oldest message id) if the bot should run, else None.

        Due: not paused or archived, no run active, not waiting to retry, the bots under
        their daily budget (unless `budget` is False), and an unread message addressed to it.
        """
        if bot["paused"] or bot["archived"]:
            return None
        last = self.last_run(bot["id"])
        if last and last["status"] == "running":
            return None
        if last and (last["retry_after"] or 0) > now:
            return None
        if budget and self.over_budget(now):
            return None
        addressed = self._work(bot["id"])
        if not addressed:
            return None

        def lane(m: Message) -> int:
            return 0 if m.sender == USER else 2 if m.sender.startswith("routine:") else 1

        first = min(addressed, key=lambda m: (lane(m), m.id))
        return lane(first), first.id

    # -- routines --------------------------------------------------------

    def create_routine(self, bot_id: str, name: str, cron: str, prompt: str) -> int:
        next_routine_at(cron, time.time())
        self._active_bot(bot_id)
        with self._tx() as db:
            cur = db.execute(
                "INSERT INTO routines (bot_id, name, cron, prompt, created_at) VALUES (?, ?, ?, ?, ?)",
                (bot_id, name, cron, prompt, time.time()),
            )
            self.post_event(user_conversation(bot_id), f"Created routine ⏱ {name}")
            return int(cur.lastrowid or 0)

    def list_routines(self, bot_id: str | None = None) -> list[dict[str, Any]]:
        if bot_id:
            rows = self.db.execute("SELECT * FROM routines WHERE bot_id = ? ORDER BY id", (bot_id,))
        else:
            rows = self.db.execute("SELECT * FROM routines ORDER BY bot_id, id")
        return [dict(r) for r in rows]

    def update_routine(self, routine_id: int, **fields: Any) -> None:
        allowed = {"name", "cron", "prompt", "paused"}
        if not fields.keys() <= allowed:
            raise ValueError(f"cannot update {set(fields) - allowed}")
        if not fields:
            raise ValueError("nothing to update")
        if "cron" in fields:
            next_routine_at(fields["cron"], time.time())
        if "paused" in fields:
            fields["paused"] = _flag(fields["paused"], "paused")
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self._tx() as db:
            db.execute(f"UPDATE routines SET {sets} WHERE id = ?", (*fields.values(), routine_id))

    def delete_routine(self, routine_id: int) -> None:
        with self._tx() as db:
            db.execute("DELETE FROM routines WHERE id = ?", (routine_id,))

    def fire_due_routines(self, since: float, now: float | None = None) -> list[int]:
        """Fire every unpaused routine with a scheduled time in (max(last fired, since), now].

        A firing posts the prompt from routine:<id> into the bot's user conversation,
        where it is an ordinary unread message. Missed firings collapse into one;
        `since` (the host's start time) makes firings missed while it was down skip.
        """
        now = now or time.time()
        fired = []
        for r in self.list_routines():
            if r["paused"]:
                continue
            bot = self.get_bot(r["bot_id"])
            if not bot or bot["archived"]:
                continue
            base = max(r["last_fired_at"] or r["created_at"], since)
            try:
                next_at = next_routine_at(r["cron"], base)
            except ValueError:
                self.update_routine(r["id"], paused=True)
                self.post_event(
                    user_conversation(r["bot_id"]),
                    f"routine {r['name']} paused: invalid cron {r['cron']!r}; edit its schedule",
                )
                continue
            if next_at > now:
                continue
            with self._tx() as db:
                db.execute("UPDATE routines SET last_fired_at = ? WHERE id = ?", (now, r["id"]))
                self.post(user_conversation(r["bot_id"]), f"routine:{r['id']}", r["prompt"])
            fired.append(r["id"])
        return fired


def open_store(cfg: Config) -> Store:
    """The data directory is ~/.bots, outside the repo, so on a fresh machine the first
    command (often `bots new`, before `bots serve`) has to make it."""
    cfg.root.mkdir(parents=True, exist_ok=True)
    return Store(cfg.db_path, cfg.daily_budget, cfg.group_sends_per_turn)
