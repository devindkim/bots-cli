"""One turn: build the envelope, run the engine's CLI in the bot's folder, record the outcome.

Nothing the model prints is delivered; only its send_* calls are."""

from __future__ import annotations

import json
import logging
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any, cast

from host import botdir, procs, sandbox
from host.config import Config
from host.engines.base import (
    BackgroundTasks,
    Engine,
    Isolating,
    McpServer,
    RateLimited,
    Result,
    Text,
    ToolEnd,
    ToolStart,
    redactor,
)
from host.kinds import FinalStatus, RunStatus
from host.store import Message, Store, render_ask, user_conversation

if TYPE_CHECKING:  # botsmcp and mcp load only in isolated mode
    from host.mcpsock import McpSocket

log = logging.getLogger(__name__)

# Why a run was killed, set before the signal so the outcome is never mistaken for a crash.
STOP, HANG, SHUTDOWN = "stop", "hang", "shutdown"
MAX_RETRY_DELAY = 3600.0


def _render(m: Message, bot_id: str) -> str:
    tags = " (event)" if m.kind == "event" else ""
    to_you = " (to you)" if bot_id in m.recipients and m.kind == "text" else ""
    return f"#{m.id} [{m.sender}]{to_you}{tags} {m.body}{render_ask(m.card)}"


def _copy(src: IO[str], dst: IO[str], redact: Callable[[str], str]) -> None:
    try:
        for line in src:
            dst.write(redact(line))
            dst.flush()
    except ValueError:  # the log closed after the join timed out: the turn is over
        pass


def _feed(proc: subprocess.Popen[str], text: str) -> None:
    """The prompt on stdin, from its own thread: a prompt bigger than the pipe would block
    until the engine reads it, and the turn has to be reading stdout by then."""
    assert proc.stdin is not None
    try:
        proc.stdin.write(text)
        proc.stdin.close()
    except (OSError, ValueError):  # it exited first; its outcome says why
        pass


def _from(store: Store, conv_id: str, first_id: int) -> list[Message]:
    """Every message in a conversation from `first_id` on, the bot's own included."""
    limit = 50
    while True:
        window = store.messages(conv_id, limit)
        if len(window) < limit or window[0].id <= first_id:
            return [m for m in window if m.id >= first_id]
        limit *= 4


def envelope(cfg: Config, store: Store, bot_id: str) -> tuple[str, dict[str, int]]:
    """The prompt for a turn, and the high-water id it delivers in each conversation."""
    bot = store.get_bot(bot_id) or {"name": bot_id, "title": "", "created_at": 0.0}
    unread = store.unread(bot_id)
    lines = [
        (
            "This turn is a fresh session: AGENTS.md here holds who you are, your SOUL, the"
            " skill, and MEMORY.md. Nothing you print is delivered; only send_to_user, send_to_agent,"
            " send_to_group are."
        ),
        "",
        f"Local time: {datetime.now().astimezone().strftime('%A %Y-%m-%d %H:%M %Z')}",
        "",
        "Roster (address bots by id):",
        *(
            f"- {b['id']}: {b['name']}{', ' + b['title'] if b['title'] else ''}"
            + (" (you)" if b["id"] == bot_id else "")
            for b in store.roster()
        ),
    ]
    woke = sorted({(m.sender, c) for c, ms in unread.items() for m in ms if bot_id in m.recipients})
    lines += ["", "Woken by: " + ("; ".join(f"{s} in {c}" for s, c in woke) or "nothing")]

    high_water: dict[str, int] = {}
    for conv_id, msgs in unread.items():
        first = msgs[0].id
        earlier = store.messages(conv_id, cfg.envelope_context, before=first)
        new = _from(store, conv_id, first)
        high_water[conv_id] = new[-1].id
        lines += ["", f"## {conv_id}"]
        if earlier:
            lines += ["(earlier, already seen)", *(_render(m, bot_id) for m in earlier), "(new)"]
        lines += (_render(m, bot_id) for m in new)

    runs = store.list_runs(bot_id, 2)
    previous = runs[1] if len(runs) > 1 else None
    since = previous["started_at"] if previous else bot["created_at"]
    seen = previous["delivered"] if previous else {}
    moved = {c: n for c, n in store.moved_since(bot_id, since, seen).items() if c not in unread}
    if moved:
        lines += ["", "Other conversations that moved since your last turn (read_conversation):"]
        lines += [f"- {c}: {n} new" for c, n in moved.items()]
    return "\n".join(lines) + "\n", high_water


class Turn:
    """A single run of a bot, executed by `run()` on its own thread."""

    def __init__(
        self,
        cfg: Config,
        store: Store,
        bot_id: str,
        run_id: int,
        engine: Engine,
        clock: Callable[[], float] = time.time,
        mcp_socket: McpSocket | None = None,
    ) -> None:
        """`mcp_socket` is the host's, in isolated mode (host/sandbox.py); None otherwise."""
        self.cfg, self.store, self.engine = cfg, store, engine
        self.mcp_socket = mcp_socket
        self.clock = clock
        self.bot_id, self.run_id = bot_id, run_id
        self.open_tools: dict[str, str] = {}  # tool id -> "name: summary"
        self.background: tuple[str, ...] = ()  # the engine's background jobs, as it last said
        self._hung_waiting = False  # the hang kill came while a background job was running
        self.last_event = clock()
        self.kill_reason: str | None = None
        self.done = threading.Event()
        self._proc: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()
        self._rate_limited: RateLimited | None = None
        self._log_dir = cfg.logs_dir / str(run_id)

    def kill(self, reason: str, sig: int = signal.SIGKILL) -> None:
        """Signal the run's whole process tree (the CLI, its MCP server, its shells)."""
        with self._lock:
            self.kill_reason = self.kill_reason or reason
            proc = self._proc
        if proc is not None and proc.returncode is None:
            procs.kill_tree(proc.pid, sig)

    def kill_if_hung(self, now: float) -> None:
        """Kill the run if it is hung: silent for `idle_timeout` with no tool open, or, while
        the engine waits on a background job, for `background_timeout`, so a job that never
        ends (or one the engine lost track of) still ends the turn. An open tool call is never
        a hang."""
        if self.open_tools:
            return
        waiting = bool(self.background)
        limit = self.cfg.background_timeout if waiting else self.cfg.idle_timeout
        if now - self.last_event <= limit:
            return
        with self._lock:
            if self.kill_reason is None:
                self._hung_waiting = waiting
        self.kill(HANG)

    def run(self) -> None:
        """Every exit sets `done`, even when the host can't open a file: a turn that never
        does blocks its bot until a restart. A run row left `running` is finished by the
        scheduler once `done` is set (settle)."""
        try:
            self._run()
        except Exception as e:
            log.exception("run %s of %s crashed", self.run_id, self.bot_id)
            self.kill("error")
            try:
                self._fail(f"host error: {e}", f"run failed in the host: {e}")
            except Exception:
                log.exception("run %s of %s: cannot record the crash", self.run_id, self.bot_id)
        finally:
            try:
                self._publish({"type": "run_finished", "status": self._status()})
            except Exception:
                log.exception("run %s of %s: cannot publish its end", self.run_id, self.bot_id)
            finally:
                self.store.close_thread()
                self.done.set()

    def settle(self) -> None:
        """Called on the scheduler's thread once `done` is set. A run its own thread could
        not finish (the store unreachable from it) ends failed here, so the bot stops showing
        as working and the scheduler retries it."""
        run = self.store.get_run(self.run_id)
        if run and run["status"] == "running":
            self._fail(
                "host error: the turn ended without finishing its run", "run failed in the host"
            )

    def _publish(self, event: dict[str, Any]) -> None:
        event = {"bot_id": self.bot_id, "run_id": self.run_id, **event}
        self._log_dir.mkdir(parents=True, exist_ok=True)
        with open(self._log_dir / "events.jsonl", "a") as f:
            f.write(json.dumps(event) + "\n")

    def _run(self) -> None:
        cfg, store = self.cfg, self.store
        bot = store.get_bot(self.bot_id)
        assert bot is not None
        folder = botdir.scaffold(cfg, self.bot_id)
        botdir.write_agents_md(cfg, self.bot_id, bot["name"], bot["title"])
        prompt, high_water = envelope(cfg, store, self.bot_id)
        store.add_delivered(self.run_id, high_water)

        if not sandbox.isolated(cfg):
            mcp = McpServer(
                command=sys.executable,
                args=["-m", "botsmcp"],
                env={
                    "BOT_ID": self.bot_id,
                    "BOT_RUN_ID": str(self.run_id),
                    "BOTS_HOME": str(cfg.root),
                },
            )
            argv, env = self.engine.command(prompt, folder, mcp)
            return self._spawn(argv, env, folder, prompt, None)
        engine = self.engine if isinstance(self.engine, Isolating) else None
        if engine is None:
            raise RuntimeError(f"engine {self.engine.name!r} has no isolation profile")
        if self.mcp_socket is None:
            raise RuntimeError("isolated mode without the host's MCP socket")
        cap = sandbox.new_capability()
        dirs = sandbox.make_run_dirs(cfg, self.bot_id, self.run_id, cap)
        self.mcp_socket.grant(cap, self.bot_id, self.run_id)
        try:
            spec = engine.isolation(dirs.home, dirs.tmp)
            mcp = McpServer(
                command=sys.executable,
                args=["-m", "botsmcp"],
                env={
                    "BOTS_MCP_SOCKET": str(self.mcp_socket.path),
                    "BOT_RUN_CAP_FILE": str(dirs.cap_file),
                    "PYTHONDONTWRITEBYTECODE": "1",
                },
            )
            argv, env = engine.command(prompt, folder, mcp, stdin=True)
            env = {**env, **spec.env, "HOME": str(dirs.home), "TMPDIR": f"{dirs.tmp}/"}
            wrapped = sandbox.command(
                cfg, self.bot_id, dirs, self.mcp_socket.path, list(spec.read), argv
            )
            self._spawn(wrapped, env, folder, prompt, prompt, (cap,))
        finally:
            self.mcp_socket.revoke(cap)
            dirs.remove()

    def _spawn(
        self,
        argv: list[str],
        env: dict[str, str],
        folder: Path,
        prompt: str,
        stdin: str | None,
        secret: tuple[str, ...] = (),
    ) -> None:
        """Run the engine to its exit. `stdin`, when given, is written to it and closed.
        `secret` (the run's capability) is cut from everything logged, like the engine keys."""
        redact = redactor(env, secret)
        self._log_dir.mkdir(parents=True, exist_ok=True)
        (self._log_dir / "prompt.txt").write_text(prompt)
        with (
            open(self._log_dir / "stdout.log", "w") as stdout_log,
            open(self._log_dir / "stderr.log", "w") as stderr_log,
        ):
            with self._lock:
                if self.kill_reason:  # stopped or shut down before it started
                    return self._outcome(None)
                self._proc = proc = subprocess.Popen(
                    argv,
                    cwd=folder,
                    env=env,
                    stdin=subprocess.DEVNULL if stdin is None else subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,  # through redact, never straight to the file
                    text=True,
                    errors="replace",
                    start_new_session=True,  # own process group, the root of kill_tree
                )
            if stdin is not None:
                threading.Thread(target=_feed, args=(proc, stdin), daemon=True).start()
            self.store.update_run(self.run_id, pgid=proc.pid)
            self._publish({"type": "run_started"})
            assert proc.stdout is not None and proc.stderr is not None
            stderr = threading.Thread(
                target=_copy, args=(proc.stderr, stderr_log, redact), daemon=True
            )
            stderr.start()
            for line in proc.stdout:
                line = redact(line)
                stdout_log.write(line)
                stdout_log.flush()
                self._consume(line)
            proc.wait()
            # A grandchild holding stderr open must not hold up the outcome.
            stderr.join(timeout=5)
        self._outcome(proc.returncode)

    def _consume(self, line: str) -> None:
        self.last_event = now = self.clock()
        fields: dict[str, Any] = {"last_event_at": now}
        try:
            events = self.engine.parse(line)
        except Exception:  # a CLI update changed its stream: keep the run, the raw line is logged
            log.exception("run %s: cannot parse %r", self.run_id, line[:200])
            events = []
        for ev in events:
            if isinstance(ev, ToolStart):
                self.open_tools[ev.id] = f"{ev.name}: {ev.summary}" if ev.summary else ev.name
                fields["open_tool"] = self.open_tools[ev.id]
                self._publish(
                    {
                        "type": "tool_start",
                        "id": ev.id,
                        "name": ev.name,
                        "summary": ev.summary,
                        "parent_id": ev.parent_id,
                        "at": now,
                    }
                )
            elif isinstance(ev, ToolEnd):
                self.open_tools.pop(ev.id, None)
                fields["open_tool"] = next(reversed(self.open_tools.values()), None)
                self._publish({"type": "tool_end", "id": ev.id, "at": now})
            elif isinstance(ev, Text):
                self._publish({"type": "text", "chunk": ev.chunk})
            elif isinstance(ev, RateLimited):
                self._rate_limited = ev
            elif isinstance(ev, BackgroundTasks):
                self.background = ev.ids
            elif isinstance(ev, Result):
                if ev.session_id:
                    fields["session_id"] = ev.session_id
                if ev.cost is not None:
                    fields["cost"] = ev.cost
        self.store.update_run(self.run_id, **fields)

    def _status(self) -> RunStatus:
        run = self.store.get_run(self.run_id)
        return cast("RunStatus", run["status"]) if run else "failed"

    def _outcome(self, code: int | None) -> None:
        """The final status, in this order: a kill (Stop, hang, shutdown), a clean exit, a
        rate limit seen during the turn, any other exit. A clean exit outranks the limit: a
        turn that finished its work must advance its cursors, or the retry repeats its sends.
        A turn the limit really stopped exits non-zero (the claude CLI exits 1 on a 429)."""
        if self.kill_reason == STOP:
            return self._finish("killed", "stopped", "run stopped by you")
        if self.kill_reason == HANG:
            if self._hung_waiting:
                idle = int(self.cfg.background_timeout)
                why = f"no output for {idle}s while waiting on a background job"
            else:
                why = f"no output for {int(self.cfg.idle_timeout)}s, no tool open"
            return self._fail("hung", f"run killed: {why}")
        if self.kill_reason == SHUTDOWN:
            return self._finish(
                "interrupted", "host shut down", "run interrupted: the host shut down; will retry"
            )
        if code == 0:
            return self._finish("ok", None, None)
        if self._rate_limited:
            # Check again well before the reset: a new login or more credits lifts the limit
            # early. Only the first limited run in a row says so in the chat.
            now = self.clock()
            check = now + self.cfg.rate_limit_backoff
            # A reset already past must not respawn the bot at once.
            reset = max(self._rate_limited.reset_at or check, now + self.cfg.failure_backoff)
            prev = self.store.list_runs(self.bot_id, 2)[1:]
            retry = min(reset, check)
            event = None
            if not (prev and prev[0]["status"] == "rate_limited"):
                until, at = (datetime.fromtimestamp(t).strftime("%H:%M") for t in (reset, retry))
                event = f"rate limited until {until}; trying again at {at}"
            return self._finish("rate_limited", None, event, retry)
        tail = self._stderr_tail()
        self._fail(
            f"exit {code}" + (f": {tail}" if tail else ""),
            f"run failed (exit {code}){': ' + tail if tail else ''}",
        )

    def _fail(self, error: str, event: str) -> None:
        """Failed: the messages stay unread and the turn is retried, backing off each time
        it fails in a row, so a broken engine neither spins nor needs a restart."""
        n = self.store.failures_in_a_row(self.bot_id) + 1
        retry = self.clock() + min(self.cfg.failure_backoff * 2 ** (n - 1), MAX_RETRY_DELAY)
        when = datetime.fromtimestamp(retry).strftime("%H:%M")
        self._finish("failed", error, f"{event}. Retrying at {when}.", retry)

    def _stderr_tail(self) -> str:
        try:
            lines = (self._log_dir / "stderr.log").read_text().strip().splitlines()
        except OSError:
            return ""
        return lines[-1][:300] if lines else ""

    def _finish(
        self,
        status: FinalStatus,
        error: str | None,
        event: str | None,
        retry_after: float | None = None,
    ) -> None:
        self.store.finish_run(self.run_id, status, error=error, retry_after=retry_after)
        if event:
            self.store.post_event(user_conversation(self.bot_id), event)
