"""`bots read` and `bots chat` (cli/chat.py): replies in the terminal."""

import io
import time
from collections.abc import Callable, Iterator

import pytest

from cli import chat
from cli.main import main
from host.config import Config
from host.store import USER, Store

RETRY = time.time() + 600


@pytest.fixture(autouse=True)
def bots_home(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOTS_HOME", str(cfg.root))


@pytest.fixture
def ada(store: Store) -> Store:
    store.create_bot("ada", "Ada")
    store.create_bot("bo", "Bo")
    return store


def start(store: Store) -> int:
    """A run that was given everything in Ada's chat so far, as turn.py's envelope is."""
    run = store.start_run("ada")
    store.add_delivered(run, {"user:ada": store.messages("user:ada", limit=1)[-1].id})
    return run


def turn(store: Store, *steps: Callable[[int], object]) -> Callable[[float], None]:
    """A stand-in for the host, driven by chat's poll sleeps: the first sleep starts a run,
    each later one plays the next step on it, and the last ends it ok."""
    plan: Iterator[Callable[[int], object]] = iter(steps)
    run: list[int] = []

    def sleep(_: float) -> None:
        if not run:
            run.append(start(store))
            return
        step = next(plan, None)
        if step is None:
            store.finish_run(run[0], "ok")
        else:
            step(run[0])

    return sleep


def test_messages_after_keeps_the_oldest_new_ones(ada: Store) -> None:
    first = ada.send_to_user("ada", "one")
    assert first is not None
    for body in ("two", "three", "four"):
        ada.send_to_user("ada", body)
    got = ada.messages("user:ada", limit=2, after=first.id)
    assert [m.body for m in got] == ["two", "three"]  # none skipped, oldest first
    assert [m.body for m in ada.messages("user:ada", limit=2)] == ["three", "four"]
    oldest = ada.messages("user:ada", limit=2, after=0)  # cursor 0 pages from the start too
    assert oldest[-1].body == "one"


def test_read_shows_every_unread_chat_then_nothing(
    ada: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    ada.send_to_user("ada", "desks: three picks\nsee shared/desks.md")
    ada.send_to_user("bo", "done")
    assert main(["read"]) == 0
    out = capsys.readouterr().out
    assert "== Ada (1)" in out and "== Bo (1)" in out
    assert "Ada: desks: three picks\n       see shared/desks.md" in out  # later lines indented
    assert ada.user_unread_counts() == {}
    assert main(["read"]) == 0
    assert capsys.readouterr().out.startswith("nothing unread")


def test_read_one_bot_marks_where_new_starts(
    ada: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    ada.user_send("user:ada", "find a desk")
    ada.send_to_user("ada", "on it")
    ada.mark_read(USER, "user:ada")
    ada.send_to_user(
        "ada", "found one", ask=[{"q": "Buy it?", "options": [{"label": "Yes"}, {"label": "No"}]}]
    )
    assert main(["read", "ada", "--keep-unread"]) == 0
    out = capsys.readouterr().out
    assert out.index("on it") < out.index("-- new --") < out.index("found one")
    assert "you: find a desk" in out
    assert "[ask: Buy it?: A) Yes | B) No]" in out  # a question card, answerable in words
    assert ada.user_unread_counts() == {"user:ada": 1}  # --keep-unread
    assert main(["read", "ada", "-n", "1"]) == 0
    assert capsys.readouterr().out.split("  ", 1)[1] == (  # only the last message
        "Ada: found one\n       [ask: Buy it?: A) Yes | B) No]\n"
    )
    assert ada.user_unread_counts() == {}
    assert main(["read", "nobody"]) == 1
    assert "no bot or group 'nobody'" in capsys.readouterr().err


def test_chat_prints_the_reply_and_tools_until_the_turn_ends(ada: Store) -> None:
    out = io.StringIO()
    sleep = turn(
        ada,
        lambda run: ada.update_run(run, open_tool="Bash: ls shared"),
        lambda run: ada.send_to_user("ada", "three desks in shared/desks.md"),
        lambda run: ada.post_event("user:ada", "-> bo: check prices"),
    )
    chat.chat(ada, "ada", "find a desk", out=out, sleep=sleep, poll=0)
    lines = out.getvalue().splitlines()
    assert [line.split("  ", 1)[1] for line in lines] == [
        "... Bash: ls shared",
        "Ada: three desks in shared/desks.md",
        "· -> bo: check prices",
    ]
    assert ada.user_unread_counts() == {}  # what chat showed counts as read


def test_chat_does_not_print_a_send_tool_since_the_send_itself_is_printed(ada: Store) -> None:
    out = io.StringIO()
    sleep = turn(
        ada,
        lambda run: ada.update_run(run, open_tool="mcp__bots__send_to_user: done"),
        lambda run: ada.send_to_user("ada", "done"),
    )
    chat.chat(ada, "ada", "go", out=out, sleep=sleep, poll=0)
    assert [line.split("  ", 1)[1] for line in out.getvalue().splitlines()] == ["Ada: done"]


def test_chat_waits_for_the_turn_after_a_running_one(ada: Store) -> None:
    """A turn already running when I send never read my message: chat follows the next."""
    busy = ada.start_run("ada")
    out = io.StringIO()
    calls: list[int] = []

    def sleep(_: float) -> None:
        calls.append(1)
        if len(calls) == 1:
            ada.send_to_user("ada", "old reply")
            ada.finish_run(busy, "ok")  # my message is still unread: the bot is due again
        elif len(calls) == 2:
            run = start(ada)
            ada.send_to_user("ada", "new reply")
            ada.finish_run(run, "ok")

    chat.chat(ada, "ada", "and a chair", out=out, sleep=sleep, poll=0)
    assert len(calls) == 2
    assert out.getvalue().splitlines()[-1].endswith("Ada: new reply")


def test_chat_waits_out_a_turn_that_started_before_my_message_landed(ada: Store) -> None:
    """A run that starts between chat's look at the runs and the send ends without my
    message, so the bot is due again at once: chat keeps watching for that next turn."""
    out = io.StringIO()
    calls: list[int] = []

    def sleep(_: float) -> None:
        calls.append(1)
        if len(calls) == 1:
            run = ada.start_run("ada")
            mine = ada.messages("user:ada", limit=1)[-1].id
            ada.add_delivered(run, {"user:ada": mine - 1})
            ada.send_to_user("ada", "old reply")
            ada.finish_run(run, "ok")
        elif len(calls) == 2:
            run = start(ada)
            ada.send_to_user("ada", "new reply")
            ada.finish_run(run, "ok")

    chat.chat(ada, "ada", "and a chair", out=out, sleep=sleep, poll=0)
    assert len(calls) == 2
    assert out.getvalue().splitlines()[-1].endswith("Ada: new reply")


def test_chat_says_when_the_turn_failed(ada: Store) -> None:
    out = io.StringIO()

    def sleep(_: float) -> None:
        run = ada.start_run("ada")
        ada.finish_run(run, "failed", error="exit 1", retry_after=RETRY)

    chat.chat(ada, "ada", "hi", out=out, sleep=sleep, poll=0)
    assert out.getvalue() == f"  turn failed: exit 1; it retries at {chat.stamp(RETRY)}\n"


def test_chat_with_no_host_hints_and_ctrl_c_stops_watching(ada: Store) -> None:
    out = io.StringIO()
    naps: list[float] = []

    def sleep(seconds: float) -> None:
        naps.append(seconds)
        if len(naps) == 3:
            raise KeyboardInterrupt

    chat.chat(ada, "ada", "hi", out=out, sleep=sleep, hint_after=0)
    assert out.getvalue() == (
        "  no turn has started yet; is `bots serve` running?\n"  # said once, not per poll
        "\n  stopped watching; `bots read ada` shows the reply later\n"
    )
    assert ada.messages("user:ada")[-1].body == "hi"


def test_chat_with_a_paused_bot_does_not_wait(ada: Store) -> None:
    ada.update_bot("ada", paused=1)
    out = io.StringIO()
    chat.chat(ada, "ada", "hi", out=out, sleep=pytest.fail)
    assert out.getvalue() == "  Ada is paused; it reads this when resumed\n"


def test_chat_prints_what_another_bot_tells_the_user_once(ada: Store) -> None:
    def tell(_: int) -> None:
        ada.send_to_user("bo", "done, from Bo")

    out = io.StringIO()
    chat.chat(ada, "ada", "go", out=out, sleep=turn(ada, tell), poll=0)
    assert out.getvalue().count("Bo: done, from Bo") == 1
    assert ada.user_unread_counts() == {}


def test_chat_names_a_paused_teammate_holding_work(ada: Store) -> None:
    ada.update_bot("bo", paused=True)

    def hand_off(_: int) -> None:
        ada.send_to_agent("ada", "bo", "build it")

    out = io.StringIO()
    chat.chat(ada, "ada", "go", out=out, sleep=turn(ada, hand_off), poll=0)
    assert "Bo is paused" in out.getvalue()


def test_chat_says_a_bot_was_already_backing_off(ada: Store) -> None:
    retry = time.time() + 600
    ada.finish_run(ada.start_run("ada"), "failed", error="exit 3", retry_after=retry)
    out = io.StringIO()
    chat.chat(ada, "ada", "again", out=out, poll=0)
    assert out.getvalue() == f"  Ada is waiting to retry at {chat.stamp(retry)}\n"


def test_send_and_chat_reach_a_group(ada: Store, capsys: pytest.CaptureFixture[str]) -> None:
    ada.create_group("team", "Team", ["ada", "bo"])
    assert main(["send", "team", "@ada plan it"]) == 0
    assert ada.messages("group:team")[-1].body == "@ada plan it"
    ada.mark_read("ada", "group:team")
    out = io.StringIO()

    def sleep(_: float) -> None:
        run = ada.start_run("ada")
        ada.add_delivered(run, {"group:team": ada.messages("group:team", limit=1)[-1].id})
        ada.post("group:team", "ada", "planned")
        ada.finish_run(run, "ok")

    chat.chat(ada, "team", "@ada plan it", out=out, sleep=sleep, poll=0)
    assert out.getvalue().splitlines()[-1].endswith("Ada: planned")


def test_chat_with_a_group_says_which_member_failed(ada: Store) -> None:
    ada.create_group("team", "Team", ["ada", "bo"])
    out = io.StringIO()

    def sleep(_: float) -> None:
        run = ada.start_run("ada")
        ada.finish_run(run, "failed", error="exit 1", retry_after=RETRY)
        bo = ada.start_run("bo")
        ada.finish_run(bo, "ok")
        ada.mark_read("bo", "group:team")

    chat.chat(ada, "team", "@ada go", out=out, sleep=sleep, poll=0)
    assert out.getvalue() == f"  Ada: turn failed: exit 1; it retries at {chat.stamp(RETRY)}\n"


def test_watch_follows_every_conversation_and_run_without_marking_read(ada: Store) -> None:
    ada.send_to_user("ada", "before watching")
    out = io.StringIO()
    naps = 0

    def sleep(_: float) -> None:
        nonlocal naps
        naps += 1
        if naps > 1:
            raise KeyboardInterrupt
        run = ada.start_run("ada")
        ada.send_to_agent("ada", "bo", "check prices")
        ada.send_to_user("ada", "all done")
        ada.finish_run(run, "ok")

    unread = ada.user_unread_counts()
    chat.watch(ada, out, poll=0, sleep=sleep)
    text = out.getvalue()
    assert "before watching" not in text
    assert "Ada: check prices" in text and "Ada: all done" in text
    assert "Ada started run 1" in text and "Ada run 1 ok" in text
    assert ada.user_unread_counts()["user:ada"] > unread["user:ada"]


def test_inspect_without_a_run_lists_recent_ones(
    ada: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    run = ada.start_run("ada")
    ada.update_run(run, cost=0.25)
    ada.finish_run(run, "ok")
    assert main(["inspect"]) == 0
    assert capsys.readouterr().out.split()[:4] == [str(run), "ada", "ok", "$0.25"]


def test_chat_loop_until_eof(ada: Store) -> None:
    ada.send_to_user("ada", "morning")
    said = iter(["", "hello"])
    out = io.StringIO()

    def ask(prompt: str) -> str:
        try:
            return next(said)
        except StopIteration:
            raise EOFError from None

    chat.chat(ada, "ada", None, out=out, ask=ask, sleep=turn(ada), poll=0)
    text = out.getvalue()
    assert "Ada: morning" in text  # the chat's recent messages first, as opening it shows
    assert "chatting with Ada; Ctrl-D to leave" in text
    assert [m.body for m in [m for m in ada.messages("user:ada") if m.kind == "text"]] == [
        "morning",
        "hello",
    ]  # blank: not sent


def test_chat_refuses_an_unknown_bot(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["chat", "nobody", "hi"]) == 1
    assert "no bot or group 'nobody'" in capsys.readouterr().err


def test_chat_drains_every_message_before_returning_after_a_turn(ada: Store) -> None:
    out = io.StringIO()
    calls = 0

    def sleep(_: float) -> None:
        nonlocal calls
        calls += 1
        assert calls == 1
        run = start(ada)
        for i in range(1001):
            ada.send_to_user("ada", f"reply {i}")
        ada.finish_run(run, "ok")

    chat.chat(ada, "ada", "give me all results", out=out, sleep=sleep, poll=0)
    assert "Ada: reply 1000\n" in out.getvalue()
    assert ada.user_unread_counts() == {}


def test_read_does_not_mark_an_unprinted_backlog_as_read(ada: Store) -> None:
    # Seed a backlog without paying a filesystem sync for every individual send.
    with ada._tx():
        for i in range(10001):
            ada.send_to_user("ada", f"backlog {i}")
    out = io.StringIO()
    chat.read(ada, None, 20, False, out)
    assert "Ada: backlog 0\n" in out.getvalue()
    assert "Ada: backlog 10000\n" in out.getvalue()
    assert ada.user_unread_counts() == {}
