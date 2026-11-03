"""`bots serve`: the scheduler in the foreground, clean exit on SIGTERM."""

from __future__ import annotations

import fcntl
import logging
import resource
import signal
from types import FrameType

from host import engine_keys, sandbox
from host.config import OPEN_FILES, Config
from host.scheduler import Scheduler
from host.store import open_store

log = logging.getLogger(__name__)


def serve(cfg: Config) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    raise_open_files()
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    with open(cfg.root / "host.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError(f"another host is already serving {cfg.root}") from None
        run(cfg)


def run(cfg: Config) -> None:
    engine_keys.load(cfg)  # a key file that is open to others refuses the whole host
    sandbox.check(cfg)  # isolation that can't be had here: refuse, never run unisolated
    store = open_store(cfg)
    mcp_socket = None
    if sandbox.isolated(cfg):
        from host.mcpsock import McpSocket  # loads botsmcp, so only when isolated

        sandbox.clear_runs(cfg)  # before recover(): no run of a dead host is still live
        mcp_socket = McpSocket(cfg, store, sandbox.socket_path(cfg))
        mcp_socket.start()
    scheduler = Scheduler(cfg, store, mcp_socket=mcp_socket)
    scheduler.recover()
    log.info("serving %s", cfg.root)

    def on_signal(signum: int, frame: FrameType | None) -> None:
        log.info("signal %s: shutting down", signum)
        scheduler.stop_scheduling()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    scheduler.run_forever()
    scheduler.shutdown()
    if mcp_socket is not None:
        mcp_socket.stop()
    log.info("stopped")


def raise_open_files(want: int = OPEN_FILES) -> int:
    """Raise this process's soft open-files limit to `want`, never past the hard limit and
    never down. Returns the soft limit it ends with."""
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    target = want if hard == resource.RLIM_INFINITY else min(want, hard)
    if soft != resource.RLIM_INFINITY and soft < target:
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
            soft = target
        except (ValueError, OSError) as e:
            log.warning("cannot raise the open-files limit from %s to %s: %s", soft, target, e)
    log.info("open-files limit %s", soft)
    return soft
