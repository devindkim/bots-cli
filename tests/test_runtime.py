"""Step 0: the scheduler and turns, driven by the fake engine with real MCP calls."""

import dataclasses
import io
import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from cli import chat as terminal
from cli.main import main
from host import botdir
from host.config import Config
from host.engines.base import RateLimited
from host.engines.fake import FakeEngine
from host.scheduler import Scheduler
from host.store import Store, open_store, user_conversation
from host.turn import HANG, SHUTDOWN, STOP, Turn


def make_bot(cfg: Config, store: Store, bot_id: str, script: list[dict[str, Any]]) -> str:
    store.create_bot(bot_id, bot_id.title(), engine="fake")
    script_for(cfg, bot_id, script)
    return user_conversation(bot_id)


def script_for(cfg: Config, bot_id: str, script: list[dict[str, Any]]) -> None:
    folder = botdir.scaffold(cfg, bot_id)
    (folder / "fake_script.json").write_text(json.dumps(script))


def run_until(sched: Scheduler, done: Callable[[], bool], timeout: float = 20) -> None:
    deadline = time.time() + timeout
    while not done():
        assert time.time() < deadline, "timed out"
        sched.tick()
        time.sleep(sched.cfg.tick)


class Clock:
    """Real time plus however far the test has moved it: timestamps the store takes stay in
    order, and waiting out idle_timeout takes no real time."""

    def __init__(self) -> None:
        self.ahead = 0.0

    def __call__(self) -> float:
        return time.time() + self.ahead

    def advance(self, seconds: float) -> None:
        self.ahead += seconds


def open_tool(store: Store, bot_id: str) -> str | None:
    run = store.last_run(bot_id)
    return run["open_tool"] if run else None


def finished(store: Store, bot_id: str, n: int = 1) -> Callable[[], bool]:
    def check() -> bool:
        runs = store.list_runs(bot_id)
        return len(runs) >= n and runs[0]["status"] != "running"

    return check


def gone(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return True
    return False


def test_scripted_turn_sends_and_advances_cursor(cfg: Config, store: Store) -> None:
    conv = make_bot(
        cfg,
        store,
        "ada",
        [{"tool": "read_messages", "args": {}}, {"tool": "send_to_user", "args": {"body": "done"}}],
    )
    hello = store.user_send(conv, "hello")
    sched = Scheduler(cfg, store)
    run_until(sched, finished(store, "ada"))

    run = store.list_runs("ada")[0]
    assert run["status"] == "ok" and run["session_id"].startswith("fake-") and run["pgid"]
    reply = store.messages(conv)[-1]
    assert (reply.sender, reply.body, reply.recipients) == ("ada", "done", ["user"])
    assert store.cursor("ada", conv) >= hello.id and store.unread("ada") == {}
    assert store.due_bots() == []
    assert "hello" in (cfg.bot_dir("ada") / "last_prompt.txt").read_text()
    assert (
        (cfg.bot_dir("ada") / "AGENTS.md")
        .read_text()
        .startswith(
            "<!-- generated before every run; edit SOUL.md or MEMORY.md -->\n\n# Ada (id ada)\n"
        )
    )

    logged = (cfg.logs_dir / str(run["id"]) / "events.jsonl").read_text().splitlines()
    seen = [json.loads(line) for line in logged]
    assert [e["type"] for e in seen] == [
        "run_started",
        *["tool_start", "tool_end"] * 2,
        "run_finished",
    ]
    assert seen[1]["name"] == "read_messages" and seen[-1]["status"] == "ok"


def test_each_turn_gets_an_engine_for_the_bots_current_model(cfg: Config, store: Store) -> None:
    conv = make_bot(cfg, store, "ada", [])
    store.update_bot("ada", model="sonnet")
    asked: list[tuple[str, str | None]] = []

    def engines(name: str, model: str | None) -> FakeEngine:
        asked.append((name, model))
        return FakeEngine()

    sched = Scheduler(cfg, store, engines)
    store.user_send(conv, "one")
    run_until(sched, finished(store, "ada"))
    store.update_bot("ada", model="")
    store.user_send(conv, "two")
    run_until(sched, finished(store, "ada", 2))
    assert asked == [("fake", "sonnet"), ("fake", None)]


def test_scripted_hang_is_killed_but_an_open_tool_is_not(cfg: Config, store: Store) -> None:
    # long past idle_timeout with a tool open is no hang; the same silence with none is
    conv = make_bot(
        cfg, store, "ada", [{"open": "Bash", "summary": "npm test", "until": "done"}, {"sleep": 60}]
    )
    store.user_send(conv, "go")
    clock = Clock()
    sched = Scheduler(cfg, store, clock=clock)
    run_until(sched, lambda: open_tool(store, "ada") == "Bash: npm test")
    clock.advance(10 * cfg.idle_timeout)
    sched.tick()
    assert store.list_runs("ada")[0]["status"] == "running"

    (cfg.bot_dir("ada") / "done").touch()
    run_until(sched, lambda: open_tool(store, "ada") is None)
    clock.advance(cfg.idle_timeout + 1)
    run_until(sched, finished(store, "ada"))

    run = store.list_runs("ada")[0]
    assert (run["status"], run["error"]) == ("failed", "hung")
    assert gone(run["pgid"])
    assert "tool_end" in (cfg.logs_dir / str(run["id"]) / "events.jsonl").read_text()
    assert "no output" in store.messages(conv)[-1].body


def background(sched: Scheduler, bot_id: str) -> tuple[str, ...] | None:
    turn = sched._turns.get(bot_id)
    return turn.background if turn else None


def test_a_background_job_holds_off_the_idle_kill_until_it_ends(cfg: Config, store: Store) -> None:
    # the engine is silent while it waits on a background shell: no hang; once the set is
    # empty again, the same silence is
    conv = make_bot(
        cfg,
        store,
        "ada",
        [{"background": ["b1"]}, {"until": "done"}, {"background": []}, {"sleep": 60}],
    )
    store.user_send(conv, "go")
    clock = Clock()
    sched = Scheduler(cfg, store, clock=clock)
    run_until(sched, lambda: background(sched, "ada") == ("b1",))
    clock.advance(10 * cfg.idle_timeout)
    sched.tick()
    assert sched._turns["ada"].kill_reason is None

    (cfg.bot_dir("ada") / "done").touch()
    run_until(sched, lambda: background(sched, "ada") == ())
    clock.advance(cfg.idle_timeout + 1)
    run_until(sched, finished(store, "ada"))

    run = store.list_runs("ada")[0]
    assert (run["status"], run["error"]) == ("failed", "hung")
    assert "no output for 2s, no tool open" in store.messages(conv)[-1].body


def test_a_background_job_that_never_ends_is_killed_at_its_ceiling(
    cfg: Config, store: Store
) -> None:
    conv = make_bot(cfg, store, "ada", [{"background": ["b1"]}, {"sleep": 60}])
    store.user_send(conv, "go")
    clock = Clock()
    sched = Scheduler(cfg, store, clock=clock)
    run_until(sched, lambda: background(sched, "ada") == ("b1",))
    clock.advance(cfg.background_timeout - 5)
    sched.tick()
    assert sched._turns["ada"].kill_reason is None

    clock.advance(10)
    run_until(sched, finished(store, "ada"))
    run = store.list_runs("ada")[0]
    assert (run["status"], run["error"]) == ("failed", "hung")
    assert gone(run["pgid"])
    assert (
        "run killed: no output for 7800s while waiting on a background job"
        in store.messages(conv)[-1].body
    )


def test_failure_retries_on_its_own_with_backoff(cfg: Config, store: Store) -> None:
    conv = make_bot(cfg, store, "ada", [{"exit": 3}])
    store.user_send(conv, "hello")
    sched = Scheduler(cfg, store)
    run_until(sched, finished(store, "ada"))

    run = store.list_runs("ada")[0]
    assert (run["status"], run["error"]) == ("failed", "exit 3")
    assert run["retry_after"] > run["ended_at"]
    assert store.due_bots() == []  # backing off
    assert "run failed (exit 3). Retrying at" in store.messages(conv)[-1].body
    assert [m.body for m in store.unread("ada")[conv] if m.sender == "user"] == ["hello"]

    run_until(sched, finished(store, "ada", 2))  # fails again: the wait doubles
    second = store.list_runs("ada")[0]
    assert second["retry_after"] - second["ended_at"] > run["retry_after"] - run["ended_at"]

    script_for(cfg, "ada", [])  # a turn that says nothing
    run_until(sched, finished(store, "ada", 3))
    assert store.list_runs("ada")[0]["status"] == "ok"
    assert "finished without replying" not in [m.body for m in store.messages(conv)]
    assert store.unread("ada") == {}  # the silent turn still consumed "hello"
    assert store.due_bots() == []


def test_rate_limit_retries_before_the_reset_and_says_so_once(cfg: Config, store: Store) -> None:
    """A new login or more credits can lift the limit long before the window resets."""
    cfg = dataclasses.replace(cfg, rate_limit_backoff=0.2)
    reset = time.time() + 86400
    conv = make_bot(cfg, store, "ada", [{"rate_limit": reset}, {"exit": 1}])  # as claude on a 429
    store.user_send(conv, "hello")
    sched = Scheduler(cfg, store)
    run_until(sched, finished(store, "ada"))

    run = store.list_runs("ada")[0]
    assert run["status"] == "rate_limited"
    assert run["retry_after"] <= run["ended_at"] + 1  # not the reset a day away
    assert "rate limited until" in store.messages(conv)[-1].body

    run_until(sched, finished(store, "ada", 2))  # still limited: no second note
    notes = [m.body for m in store.messages(conv) if m.body.startswith("rate limited")]
    assert len(notes) == 1

    script_for(cfg, "ada", [])  # the limit lifted
    run_until(sched, finished(store, "ada", 3))
    assert store.list_runs("ada")[0]["status"] == "ok"
    assert store.unread("ada") == {}


def test_a_turn_that_exits_cleanly_after_a_rate_limit_is_ok(cfg: Config, store: Store) -> None:
    """Claude can report a rejected window and still finish the turn. Recorded rate_limited,
    its messages stayed unread and the retry sent everything again."""
    conv = make_bot(
        cfg,
        store,
        "ada",
        [{"rate_limit": time.time() + 86400}, {"tool": "send_to_user", "args": {"body": "done"}}],
    )
    store.user_send(conv, "hello")
    sched = Scheduler(cfg, store)
    run_until(sched, finished(store, "ada"))

    run = store.list_runs("ada")[0]
    assert (run["status"], run["retry_after"]) == ("ok", None)
    assert store.unread("ada") == {}
    assert store.due_bots() == []  # no retry to repeat the send
    assert [m.body for m in store.messages(conv) if m.sender == "ada"] == ["done"]
    assert not any(m.body.startswith("rate limited") for m in store.messages(conv))


# Every (kill reason, rate limit seen, exit code) a turn can end with, and the status it gets.
# A kill decides first, then a clean exit, then the rate limit, then any other exit.
OUTCOMES = [
    (STOP, False, 0, "killed"),
    (STOP, True, 0, "killed"),
    (STOP, False, 1, "killed"),
    (STOP, True, 1, "killed"),
    (STOP, False, None, "killed"),
    (HANG, False, 0, "failed"),
    (HANG, True, 0, "failed"),
    (HANG, False, 1, "failed"),
    (HANG, True, 1, "failed"),
    (HANG, True, None, "failed"),
    (SHUTDOWN, False, 0, "interrupted"),
    (SHUTDOWN, True, 0, "interrupted"),
    (SHUTDOWN, False, 1, "interrupted"),
    (SHUTDOWN, True, 1, "interrupted"),
    (SHUTDOWN, True, None, "interrupted"),
    (None, False, 0, "ok"),
    (None, True, 0, "ok"),
    (None, False, 1, "failed"),
    (None, True, 1, "rate_limited"),
    (None, True, -9, "rate_limited"),
]


@pytest.mark.parametrize(("kill_reason", "limited", "code", "status"), OUTCOMES)
def test_outcome_precedence(
    cfg: Config, store: Store, kill_reason: str | None, limited: bool, code: int | None, status: str
) -> None:
    store.create_bot("ada", "Ada", engine="fake")
    turn = Turn(cfg, store, "ada", store.start_run("ada"), FakeEngine())
    turn.kill_reason = kill_reason
    turn._rate_limited = RateLimited(None) if limited else None
    turn._outcome(code)

    run = store.get_run(turn.run_id)
    assert run and run["status"] == status


def test_dead_hosts_run_is_interrupted_on_start(cfg: Config, store: Store) -> None:
    conv = make_bot(cfg, store, "ada", [])
    store.user_send(conv, "hello")
    run_id = store.start_run("ada")
    orphan = subprocess.Popen(["sleep", "60"], start_new_session=True)
    store.update_run(run_id, pgid=orphan.pid)

    Scheduler(cfg, store).recover()
    assert orphan.wait(timeout=5) == -signal.SIGKILL
    run = store.get_run(run_id)
    assert run and run["status"] == "interrupted"
    assert "interrupted" in store.messages(conv)[-1].body
    assert store.due_bots() == ["ada"]  # not blocked: the message is redelivered


def test_a_rate_limit_whose_reset_has_passed_backs_off_and_chat_says_so(cfg: Config) -> None:
    cfg = dataclasses.replace(cfg, failure_backoff=60)
    store = open_store(cfg)
    make_bot(cfg, store, "ada", [{"rate_limit": 1767225600}, {"exit": 1}])
    sched = Scheduler(cfg, store)
    host = threading.Thread(target=sched.run_forever)
    host.start()
    out = io.StringIO()
    try:
        terminal.chat(open_store(cfg), "ada", "go", out=out, poll=0.02)
    finally:
        sched.stop_scheduling()
        host.join()
        sched.shutdown()
    assert [r["status"] for r in store.list_runs("ada")] == ["rate_limited"]
    retry = store.list_runs("ada")[0]["retry_after"]
    assert retry > time.time() + 30
    assert f"turn rate_limited; it retries at {terminal.stamp(retry)}" in out.getvalue()


def test_a_run_stopped_before_the_host_died_is_killed_on_start(cfg: Config, store: Store) -> None:
    conv = make_bot(cfg, store, "ada", [])
    store.user_send(conv, "hello")
    run_id = store.start_run("ada")
    store.add_delivered(run_id, {conv: store.messages(conv, limit=1)[-1].id})
    assert store.request_stop("ada") == run_id

    Scheduler(cfg, store).recover()
    run = store.get_run(run_id)
    assert run and run["status"] == "killed"
    assert store.due_bots() == []  # the stopped work does not run again


def test_boot_time_is_read_on_linux_and_macos(tmp_path: Path) -> None:
    """Unknown, it is "now", and recover() then kills nothing: a dead host's engines would
    keep running on Linux, where macOS's sysctl doesn't exist."""
    from host.scheduler import _boot_time

    stat = tmp_path / "stat"
    stat.write_text("cpu  1 2 3\nbtime 1793670000\nprocesses 42\n")
    assert _boot_time(str(stat)) == 1793670000
    if sys.platform == "darwin":  # no /proc/stat: sysctl
        assert _boot_time(str(tmp_path / "none")) < time.time() - 60


def out_of_files(*args: Any, **kwargs: Any) -> None:
    if threading.current_thread().name.startswith("turn-"):
        raise OSError(24, "Too many open files")


def test_a_turn_that_cannot_publish_still_ends_and_frees_its_bot(
    cfg: Config, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The host out of open files: events.jsonl won't open, at the start and in `finally`."""
    conv = make_bot(cfg, store, "ada", [])
    store.user_send(conv, "hello")
    sched = Scheduler(cfg, store)
    with monkeypatch.context() as m:
        m.setattr(Turn, "_publish", lambda self, event: out_of_files())
        run_until(sched, finished(store, "ada"))
        run_until(sched, lambda: not sched._turns)  # done was set: the bot isn't held

    run = store.list_runs("ada")[0]
    assert (run["status"], run["error"]) == ("failed", "host error: [Errno 24] Too many open files")
    run_until(sched, finished(store, "ada", 2))  # retried once the backoff passes
    assert store.list_runs("ada")[0]["status"] == "ok"


def test_a_run_its_turn_cannot_finish_is_finished_by_the_scheduler(
    cfg: Config, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not even the store opens on the turn's thread: the scheduler's thread ends the run."""
    conv = make_bot(cfg, store, "ada", [])
    store.user_send(conv, "hello")
    sched = Scheduler(cfg, store)
    finish_run = Store.finish_run

    def finish_off_the_turns_thread(*args: Any, **kwargs: Any) -> None:
        out_of_files()
        finish_run(*args, **kwargs)

    with monkeypatch.context() as m:
        m.setattr(Turn, "_publish", lambda self, event: out_of_files())
        m.setattr(Store, "finish_run", finish_off_the_turns_thread)
        run_until(sched, finished(store, "ada"))

    run = store.list_runs("ada")[0]
    assert (run["status"], run["error"]) == (
        "failed",
        "host error: the turn ended without finishing its run",
    )
    assert "run failed in the host. Retrying at" in store.messages(conv)[-1].body
    run_until(sched, finished(store, "ada", 2))
    assert store.list_runs("ada")[0]["status"] == "ok"


def test_sigterm_kills_run_group_and_exits_fast(cfg: Config, store: Store) -> None:
    (cfg.root / "config.toml").write_text('tick = "0.05"\n')
    conv = make_bot(
        cfg, store, "ada", [{"tool": "list_agents", "args": {}}, {"open": "wait", "sleep": 60}]
    )
    store.user_send(conv, "hello")
    host = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from host.config import load; from host.serve import serve; serve(load())",
        ],
        env={**os.environ, "BOTS_HOME": str(cfg.root)},
    )
    try:
        deadline = time.time() + 20
        while not ((run := store.last_run("ada")) and run["open_tool"] == "wait"):
            assert time.time() < deadline and host.poll() is None, "run never got going"
            time.sleep(0.05)
        started = time.time()
        host.send_signal(signal.SIGTERM)
        assert host.wait(timeout=15) == 0
        assert time.time() - started < 15
    finally:
        host.kill()
    run = store.last_run("ada")
    assert run and run["status"] == "interrupted" and gone(run["pgid"])
    assert store.due_bots() == ["ada"]


def test_bots_stop_kills_the_whole_tree_and_drops_the_turn(
    cfg: Config, store: Store, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("BOTS_HOME", str(cfg.root))
    conv = make_bot(cfg, store, "ada", [{"open": "Bash", "sleep": 30, "detached": True}])
    hello = store.user_send(conv, "go")
    sched = Scheduler(cfg, store)
    pid_file = cfg.bot_dir("ada") / "child.pid"
    run_until(sched, pid_file.exists)
    ticking = threading.Thread(target=run_until, args=(sched, finished(store, "ada")), daemon=True)
    ticking.start()

    assert main(["stop", "ada"]) == 0
    ticking.join()

    assert capsys.readouterr().out == "stopped ada's turn\n"
    assert store.list_runs("ada")[0]["status"] == "killed"
    assert store.cursor("ada", conv) >= hello.id
    assert store.bot_state("ada") == "idle" and store.due_bots() == []  # not rerun
    child = int(pid_file.read_text())
    deadline = time.time() + 5
    while not gone(child) and time.time() < deadline:  # killed, awaiting reaping by init
        time.sleep(0.1)
    assert gone(child), "the bot's detached shell outlived the run"


def test_bots_stop_says_when_there_is_no_turn_to_stop(
    cfg: Config, store: Store, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("BOTS_HOME", str(cfg.root))
    make_bot(cfg, store, "ada", [])

    assert main(["stop", "ada"]) == 0

    assert capsys.readouterr().out == "ada had no running turn\n"


def test_bots_stop_fails_when_no_host_picks_it_up(
    cfg: Config, store: Store, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("BOTS_HOME", str(cfg.root))
    monkeypatch.setattr("cli.main.STOP_WAIT", 0.1)
    make_bot(cfg, store, "ada", [])
    store.start_run("ada")

    assert main(["stop", "ada"]) == 1

    assert "is the host running?" in capsys.readouterr().err


def test_an_invalid_routine_does_not_stop_bot_turns(cfg: Config, store: Store) -> None:
    conv = make_bot(cfg, store, "ada", [{"tool": "send_to_user", "args": {"body": "alive"}}])
    routine = store.create_routine("ada", "Broken", "* * * * *", "hello")
    store.db.execute("UPDATE routines SET cron = ? WHERE id = ?", ("0 0 31 2 *", routine))
    store.user_send(conv, "go")
    sched = Scheduler(cfg, store)
    run_until(sched, finished(store, "ada"))
    assert store.list_runs("ada")[0]["status"] == "ok"
    assert store.messages(conv)[-1].body == "alive"
    assert store.list_routines()[0]["paused"] == 1


def test_chat_waits_for_the_whole_team_and_prints_the_final_reply(
    cfg: Config, store: Store
) -> None:
    ask = {"tool": "send_to_agent", "args": {"agent_id": "bob", "body": "what is 17 x 23?"}}
    make_bot(cfg, store, "chief", [ask, {"shell": "cp next.json fake_script.json"}])
    reply = [{"tool": "send_to_user", "args": {"body": "391, from bob"}}]
    (cfg.bot_dir("chief") / "next.json").write_text(json.dumps(reply))
    answer = {"tool": "send_to_agent", "args": {"agent_id": "chief", "body": "391"}}
    make_bot(cfg, store, "bob", [answer])
    sched = Scheduler(cfg, store)
    host = threading.Thread(target=sched.run_forever)
    host.start()
    out = io.StringIO()
    try:
        terminal.chat(open_store(cfg), "chief", "ask bob", out=out, poll=0.02)
    finally:
        sched.stop_scheduling()
        host.join()
        sched.shutdown()
    assert "Chief: 391, from bob" in out.getvalue()
    assert [r["status"] for r in store.list_runs("chief")] == ["ok", "ok"]


def test_chat_rechecks_due_after_a_running_bot_hands_off_between_snapshots(
    cfg: Config, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_bot(
        cfg,
        store,
        "ada",
        [
            {"open": "wait", "until": "release"},
            {"shell": "cp next.json fake_script.json"},
            {"tool": "send_to_agent", "args": {"agent_id": "bob", "body": "finish the task"}},
        ],
    )
    (cfg.bot_dir("ada") / "next.json").write_text(
        json.dumps(
            [
                {"tool": "send_to_user", "args": {"body": "final reply"}},
            ]
        )
    )
    make_bot(
        cfg,
        store,
        "bob",
        [
            {"tool": "send_to_agent", "args": {"agent_id": "ada", "body": "finished"}},
        ],
    )
    sched = Scheduler(cfg, store)
    observing = open_store(cfg)
    real_due = observing.due_bots
    released = False

    def due(now: float | None = None) -> list[str]:
        nonlocal released
        result = real_due(now)
        if not released and not result and store.running_run("ada"):
            released = True
            (cfg.bot_dir("ada") / "release").touch()
            deadline = time.time() + 10
            while not finished(store, "ada")():
                assert time.time() < deadline
                time.sleep(0.01)
        return result

    def tick(_: float) -> None:
        sched.tick()
        if not released:
            deadline = time.time() + 10
            while open_tool(store, "ada") != "wait":
                assert time.time() < deadline
                time.sleep(0.01)

    monkeypatch.setattr(observing, "due_bots", due)
    out = io.StringIO()
    try:
        terminal.chat(observing, "ada", "go", out=out, sleep=tick, poll=0)
        assert store.due_bots() == [], "chat returned while Bob still needs to run"
        assert "Ada: final reply" in out.getvalue()
    finally:
        sched.shutdown()


def test_chat_prints_a_delegated_bots_final_reply(cfg: Config, store: Store) -> None:
    make_bot(
        cfg,
        store,
        "ada",
        [
            {
                "tool": "send_to_agent",
                "args": {"agent_id": "bob", "body": "finish and tell the user"},
            },
        ],
    )
    make_bot(
        cfg,
        store,
        "bob",
        [
            {"tool": "send_to_user", "args": {"body": "delegated final reply"}},
        ],
    )
    sched = Scheduler(cfg, store)
    host = threading.Thread(target=sched.run_forever)
    host.start()
    out = io.StringIO()
    try:
        terminal.chat(open_store(cfg), "ada", "go", out=out, poll=0.02)
        assert "delegated final reply" in out.getvalue()
    finally:
        sched.stop_scheduling()
        host.join()
        sched.shutdown()


def test_two_bots_waking_each_other_stop_at_the_daily_budget(cfg: Config) -> None:
    """No streak cap: a bot-to-bot loop runs until the money runs out, and no further."""
    cfg = dataclasses.replace(cfg, daily_budget=1.0, max_concurrent=2)
    store = open_store(cfg)
    for me, other in (("ada", "bob"), ("bob", "ada")):
        send = {"tool": "send_to_agent", "args": {"agent_id": other, "body": "your turn"}}
        make_bot(cfg, store, me, [{"tool": "read_messages", "args": {}}, send, {"cost": 0.3}])
    store.send_to_agent("ada", "bob", "start")
    sched = Scheduler(cfg, store)
    run_until(sched, lambda: store.over_budget() and not store.running_runs())
    started = len(store.list_runs("ada") + store.list_runs("bob"))
    for _ in range(20):  # the loop is still due, but nothing more starts
        sched.tick()
    assert len(store.list_runs("ada") + store.list_runs("bob")) == started
    assert store.bot_state("ada") == "over_budget" or store.bot_state("bob") == "over_budget"
    # cost arrives only when a turn ends, so turns already running when the budget is
    # crossed still finish: at most max_concurrent of them past it
    assert 1.0 <= store.spent_today() <= 1.0 + cfg.max_concurrent * 0.3 + 1e-9


@pytest.mark.parametrize("group", [False, True])
@pytest.mark.parametrize("read", [False, True])
def test_silent_success_posts_nothing_and_does_not_wake_again(
    cfg: Config, store: Store, group: bool, read: bool
) -> None:
    conv = make_bot(cfg, store, "ada", [{"tool": "read_messages", "args": {}}] if read else [])
    if group:
        make_bot(cfg, store, "bob", [])
        store.update_bot("bob", paused=True)
        conv = store.create_group("qa", "QA", ["ada", "bob"])
    hello = store.user_send(conv, "hello")
    before = [m.id for m in store.messages(conv)]
    sched = Scheduler(cfg, store)
    run_until(sched, finished(store, "ada"))

    run = store.list_runs("ada")[0]
    assert run["status"] == "ok"
    assert [m.id for m in store.messages(conv)] == before
    assert store.cursor("ada", conv) >= hello.id
    assert store.unread("ada") == {}
    assert store.due_bots() == []
    for _ in range(3):
        sched.tick()
    assert len(store.list_runs("ada")) == 1
    logged = (cfg.logs_dir / str(run["id"]) / "events.jsonl").read_text().splitlines()
    seen = [json.loads(line) for line in logged]
    assert seen[-1]["type"] == "run_finished"
    assert seen[-1]["status"] == "ok"
