"""The loop: fire routines, start due bots up to max_concurrent, kill hangs, shut down cleanly."""

from __future__ import annotations

import logging
import re
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from host import procs
from host.config import Config
from host.engines import get_engine
from host.engines.base import Engine
from host.store import Store, user_conversation
from host.turn import SHUTDOWN, STOP, Turn

if TYPE_CHECKING:  # botsmcp and mcp load only in isolated mode
    from host.mcpsock import McpSocket

log = logging.getLogger(__name__)


class Scheduler:
    def __init__(
        self,
        cfg: Config,
        store: Store,
        engines: Callable[[str, str | None], Engine] | None = None,
        clock: Callable[[], float] = time.time,
        mcp_socket: McpSocket | None = None,
    ) -> None:
        """`engines` maps an engine name and the bot's model to a fresh engine; one per turn.
        `clock` is what hangs, retries and due times are measured by; tests move it forward."""
        self.cfg, self.store, self.clock = cfg, store, clock
        self.mcp_socket = mcp_socket  # isolated mode's (host/sandbox.py), else None
        self.engines = engines or (lambda name, model: get_engine(name, cfg, model))
        self.started_at = clock()
        self._turns: dict[str, Turn] = {}
        self._lock = threading.Lock()
        self._stopping = threading.Event()

    def recover(self) -> None:
        """Runs still `running` belong to a host that died. Kill what is left of them. Their
        messages are redelivered, unless the user had asked to stop the run."""
        booted = _boot_time()
        for run in self.store.running_runs():
            if run["pgid"] and run["started_at"] > booted:  # else the pgid may be reused
                procs.kill_tree(run["pgid"], signal.SIGKILL)
            if run["stop_requested"]:
                self.store.finish_run(run["id"], "killed", error="stopped")
                self.store.post_event(user_conversation(run["bot_id"]), "run stopped by you")
                continue
            self.store.finish_run(run["id"], "interrupted", error="host restarted")
            self.store.post_event(
                user_conversation(run["bot_id"]),
                "run interrupted: the host restarted mid-turn; the messages will be redelivered",
            )

    def tick(self) -> None:
        now = self.clock()
        self.store.fire_due_routines(self.started_at, now)
        with self._lock:
            for bot_id, turn in list(self._turns.items()):
                if turn.done.is_set():
                    try:
                        turn.settle()
                    except Exception:  # kept, so the next tick tries again
                        log.exception("run %s of %s: cannot settle", turn.run_id, bot_id)
                        continue
                    del self._turns[bot_id]
            stopped = {r["id"] for r in self.store.running_runs() if r["stop_requested"]}
            for turn in self._turns.values():
                if turn.run_id in stopped:
                    turn.kill(STOP)
                else:
                    turn.kill_if_hung(now)
            if self._stopping.is_set():
                return
            for bot_id in self.store.due_bots(now):
                if len(self._turns) >= self.cfg.max_concurrent:
                    break
                if bot_id not in self._turns:
                    self._start(bot_id)

    def _start(self, bot_id: str) -> None:
        bot = self.store.get_bot(bot_id)
        assert bot is not None
        engine = self.engines(bot["engine"], bot["model"])
        run_id = self.store.start_run(bot_id)
        turn = Turn(self.cfg, self.store, bot_id, run_id, engine, self.clock, self.mcp_socket)
        self._turns[bot_id] = turn
        threading.Thread(target=turn.run, name=f"turn-{bot_id}-{run_id}", daemon=True).start()
        log.info("started run %s for %s", run_id, bot_id)

    def run_forever(self) -> None:
        """Tick until stop_scheduling() or shutdown()."""
        while not self._stopping.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("tick failed")
            self._stopping.wait(self.cfg.tick)

    def stop_scheduling(self) -> None:
        """Safe from a signal handler: run_forever returns at its next wake."""
        self._stopping.set()

    def shutdown(self, grace: float = 10) -> None:
        """SIGTERM every run's process group, SIGKILL what is left after `grace`, mark them
        interrupted. Bounded so launchd's exit timeout is never hit."""
        self._stopping.set()
        with self._lock:
            turns = list(self._turns.values())
        for turn in turns:
            turn.kill(SHUTDOWN, signal.SIGTERM)
        deadline = time.time() + grace
        for turn in turns:
            turn.done.wait(max(0.0, deadline - time.time()))
        for turn in turns:
            if not turn.done.is_set():
                turn.kill(SHUTDOWN, signal.SIGKILL)
        for turn in turns:
            turn.done.wait(2)
        for run in self.store.running_runs():  # a turn thread that never got to finish
            self.store.finish_run(run["id"], "interrupted", error="host shut down")


def _boot_time(proc_stat: str = "/proc/stat") -> float:
    """When the machine booted (macOS sysctl, Linux /proc/stat); a pgid stored before then
    names someone else's process. Unknown: now, so nothing is killed."""
    try:
        with open(proc_stat) as f:
            for line in f:
                if line.startswith("btime "):
                    return float(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    try:
        out = subprocess.run(
            ["sysctl", "-n", "kern.boottime"], check=False, capture_output=True, text=True
        )
        match = re.search(r"sec = (\d+)", out.stdout)
        return float(match.group(1)) if match else time.time()
    except OSError:
        return time.time()
