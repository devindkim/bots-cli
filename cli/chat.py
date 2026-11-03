"""`bots read`, `bots chat` and `bots watch`: the team in the terminal, from the store."""

from __future__ import annotations

import datetime
import sys
import time
from collections.abc import Callable, Iterator
from typing import Any, TextIO

from host.store import (
    BUDGET_HINT,
    HOST,
    USER,
    Message,
    Store,
    group_conversation,
    render_ask,
    user_conversation,
)

POLL = 0.5  # seconds between looks at the store while a turn runs
HINT_AFTER = 10.0  # seconds with no turn started before chat asks whether the host is up
PAGE = 500  # messages per store read; callers page on until a read comes up short
RUNS = 200  # recent runs chat looks through for how a team's turns ended


def is_send(tool: str) -> bool:
    """A tool call ("name: summary") that sends a message, which chat prints on its own."""
    return tool.split(":", 1)[0].endswith(("send_to_user", "send_to_agent", "send_to_group"))


def names(store: Store) -> dict[str, str]:
    """Sender id -> what the terminal shows; archived bots too, their old messages stay."""
    return {b["id"]: b["name"] for b in store.list_bots(include_archived=True)} | {USER: "you"}


def stamp(at: float, now: float | None = None) -> str:
    """HH:MM today, else the date in front."""
    when = datetime.datetime.fromtimestamp(at)
    today = datetime.datetime.fromtimestamp(now or time.time()).date()
    return when.strftime("%H:%M" if when.date() == today else "%Y-%m-%d %H:%M")


def render(m: Message, who: dict[str, str], now: float | None = None) -> str:
    """One message: time and sender, then the body, later lines indented under it. Events
    (a send to another bot, a failed run) are marked with a dot."""
    body = m.body + render_ask(m.card)
    lines = body.splitlines() or [""]
    if m.kind == "event" or m.sender == HOST:
        head = f"{stamp(m.created_at, now)}  · {lines[0]}"
    else:
        head = f"{stamp(m.created_at, now)}  {who.get(m.sender, m.sender)}: {lines[0]}"
    return "\n".join([head, *(f"       {line}" for line in lines[1:])])


def since(store: Store, conv_id: str, after: int) -> Iterator[Message]:
    """Every message past `after`, oldest first, a page at a time (cursor 0 included), so a
    long backlog or a burst between two polls is printed whole before anything is marked read."""
    while True:
        page = store.messages(conv_id, limit=PAGE, hide_routines=True, after=after)
        yield from page
        if len(page) < PAGE:
            return
        after = page[-1].id


def target(store: Store, name: str) -> str:
    """A bot id (its chat with the user) or a group id, to a conversation id."""
    bot = store.get_bot(name)
    if bot and not bot["archived"]:
        return user_conversation(name)
    conv = store.get_conversation(group_conversation(name))
    if conv and conv["kind"] == "group" and not conv["archived"]:
        return str(conv["id"])
    raise ValueError(f"no bot or group {name!r}")


def title(store: Store, conv_id: str, who: dict[str, str]) -> str:
    if conv_id.startswith("user:"):
        return who.get(conv_id.removeprefix("user:"), conv_id)
    if conv_id.startswith("pair:"):
        return " and ".join(who.get(b, b) for b in conv_id.split(":")[1:])
    conv = store.get_conversation(conv_id)
    return f"group {conv['name']}" if conv else conv_id


def read(store: Store, name: str | None, recent: int, keep: bool, out: TextIO) -> None:
    """With a bot or group: its last `recent` messages, a marker where unread starts.
    Without: every chat with something unread, only the unread part.
    What was shown is marked read, unless `keep`."""
    who = names(store)
    if name:
        conv_id = target(store, name)
        shown = store.messages(conv_id, limit=recent, hide_routines=True)
        cursor = store.cursor(USER, conv_id)
        marked = False
        for m in shown:
            if m.id > cursor and not marked and cursor and m is not shown[0]:
                print("  -- new --", file=out)
                marked = True
            print(render(m, who), file=out)
        if not shown:
            print(f"nothing yet in your chat with {title(store, conv_id, who)}", file=out)
        elif not keep:
            store.mark_read(USER, conv_id, shown[-1].id)
        return
    counts = store.user_unread_counts()
    if not counts:
        print("nothing unread; `bots read <bot>` shows a chat's recent messages", file=out)
        return
    for i, conv_id in enumerate(sorted(counts)):
        unread = list(since(store, conv_id, store.cursor(USER, conv_id)))
        print(
            ("\n" if i else "") + f"== {title(store, conv_id, who)} ({counts[conv_id]})", file=out
        )
        for m in unread:
            print(render(m, who), file=out)
        if unread and not keep:
            store.mark_read(USER, conv_id, unread[-1].id)


def follow(
    store: Store,
    conv_id: str,
    after: int,
    baseline: int,
    out: TextIO,
    poll: float = POLL,
    hint_after: float = HINT_AFTER,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Print the chat past message `after` until the team settles: no bot is due and no run
    is live, so work handed from bot to bot is followed too. A paused, over-budget or
    backing-off bot is not due, so it can't hold chat open. `baseline` is the newest run
    before the send."""
    who = names(store)
    started = time.monotonic()
    hinted = False
    tools: dict[int, str | None] = {}
    settled = False
    newest = after

    def tag(bot_id: str) -> str:
        return "" if conv_id == user_conversation(bot_id) else f"{who.get(bot_id, bot_id)}: "

    while True:
        for m in since(store, conv_id, after):
            if m.sender != USER or m.kind != "text":  # the user's own line is on screen already
                print(render(m, who), file=out, flush=True)
            after = m.id
        store.mark_read(USER, conv_id, after)
        newest = print_replies(store, conv_id, newest, who, out)
        if settled:  # after one more look at the chat: a turn's last send lands before it ends
            return
        due, live = store.activity()
        for run in live:
            tool = run["open_tool"]
            if tool and tool != tools.get(run["id"]) and not is_send(tool):
                print(f"  ... {tag(run['bot_id'])}{tool}", file=out, flush=True)
            tools[run["id"]] = tool
        if due or live:
            runs = store.list_runs(limit=1)
            ran = runs and runs[0]["id"] > baseline
            if not hinted and not ran and time.monotonic() - started > hint_after:
                print("  no turn has started yet; is `bots serve` running?", file=out, flush=True)
                hinted = True
            sleep(poll)
            continue
        settled = True
        report(store, conv_id, baseline, who, tag, out)


def print_replies(store: Store, conv_id: str, after: int, who: dict[str, str], out: TextIO) -> int:
    """Print what bots told the user outside this chat since message `after`, so a delegated
    reply shows before chat returns. Returns the newest message looked at."""
    replied = set()
    while page := store.messages_after(after, PAGE):
        after = page[-1].id
        replied |= {m.conversation_id for m in page if m.kind == "text" and m.recipients == [USER]}
    for other in sorted(replied - {conv_id}):
        unread = list(since(store, other, store.cursor(USER, other)))
        for m in unread:
            if m.kind == "text":
                print(render(m, who), file=out, flush=True)
        if unread:
            store.mark_read(USER, other, unread[-1].id)
    return after


def report(
    store: Store,
    conv_id: str,
    baseline: int,
    who: dict[str, str],
    tag: Callable[[str], str],
    out: TextIO,
) -> None:
    """Why a settled team may not have answered: a failed turn, a held bot, the budget."""
    latest = {r["bot_id"]: r for r in reversed(store.list_runs(limit=RUNS)) if r["id"] > baseline}
    for run in latest.values():
        if run["status"] != "ok":
            error = f": {run['error']}" if run.get("error") else ""
            retry = run.get("retry_after")
            again = f"; it retries at {stamp(retry)}" if retry else "; it retries on its own"
            again = "" if run["status"] == "killed" else again
            print(f"  {tag(run['bot_id'])}turn {run['status']}{error}{again}", file=out)
    for bot_id, retry in store.stalled():
        if retry is None:
            print(f"  {who.get(bot_id, bot_id)} is paused; it reads this when resumed", file=out)
        elif bot_id not in latest:
            print(f"  {who.get(bot_id, bot_id)} is waiting to retry at {stamp(retry)}", file=out)
    if any(store.bot_state(b["id"]) == "over_budget" for b in store.list_bots()):
        print(f"  over the daily budget; this waits for midnight ({BUDGET_HINT})", file=out)


def watch(
    store: Store,
    out: TextIO,
    poll: float = POLL,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Every message in every conversation and each run's start and end, until Ctrl-C.
    Reads only: nothing is marked read."""
    who = names(store)
    after = store.last_message_id()
    seen = {r["id"]: r["status"] for r in store.list_runs(limit=RUNS)}
    try:
        while True:
            for m in store.messages_after(after, PAGE):
                after = m.id
                if m.kind != "event":  # events echo the sends shown here and the runs below
                    print(f"[{title(store, m.conversation_id, who)}] {render(m, who)}", file=out)
            for run in reversed(store.list_runs(limit=RUNS)):
                if seen.get(run["id"]) == run["status"]:
                    continue
                name = who.get(run["bot_id"], run["bot_id"])
                if run["id"] not in seen:
                    print(f"  {name} started run {run['id']}", file=out)
                if run["status"] != "running":
                    print(f"  {name} run {run['id']} {run['status']}", file=out)
                seen[run["id"]] = run["status"]
            out.flush()
            sleep(poll)
    except KeyboardInterrupt:
        print(file=out)


def chat(
    store: Store,
    name: str,
    text: str | None,
    out: TextIO = sys.stdout,
    ask: Callable[[str], str] = input,
    **follow_args: Any,
) -> None:
    """Send `text` to a bot or group and follow the reply; without text, a prompt loop until
    EOF or Ctrl-C. Ctrl-C during a turn stops watching (the turn goes on) and returns to the
    prompt."""
    conv_id = target(store, name)

    def say(body: str) -> None:
        newest = store.list_runs(limit=1)
        msg = store.user_send(conv_id, body)
        try:
            follow(store, conv_id, msg.id, newest[0]["id"] if newest else 0, out, **follow_args)
        except KeyboardInterrupt:
            print(f"\n  stopped watching; `bots read {name}` shows the reply later", file=out)

    if text is not None:
        say(text)
        return
    read(store, name, 10, False, out)
    print(f"chatting with {title(store, conv_id, names(store))}; Ctrl-D to leave", file=out)
    while True:
        try:
            line = ask("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print(file=out)
            return
        if line:
            say(line)
