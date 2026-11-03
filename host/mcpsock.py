"""The host's MCP socket for isolated runs: botsmcp in the sandbox sends each tool call here.

The capability is the only identity. The host mints one per run and forgets it when the
run ends. The socket is mode 0600 and a peer of another uid is dropped."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import socket
import struct
import sys
import threading
from pathlib import Path
from typing import Any

import botsmcp
from host.config import Config
from host.store import Store

log = logging.getLogger(__name__)

MAX_REQUEST = 1 << 20  # a tool call's JSON; a send's body is far smaller
_SOL_LOCAL, _LOCAL_PEERCRED = 0, 0x001  # macOS <sys/un.h>: struct xucred of the peer


def peer_uid(conn: socket.socket) -> int | None:
    """The connecting process's uid (macOS LOCAL_PEERCRED, Linux SO_PEERCRED), or None
    when the OS won't say."""
    try:
        if sys.platform == "darwin":
            raw = conn.getsockopt(_SOL_LOCAL, _LOCAL_PEERCRED, 76)
            return int(struct.unpack_from("=II", raw, 0)[1])
        peercred = getattr(socket, "SO_PEERCRED", None)
        if peercred is None:
            return None
        raw = conn.getsockopt(socket.SOL_SOCKET, peercred, struct.calcsize("3i"))
        return int(struct.unpack("3i", raw)[1])  # struct ucred: pid, uid, gid
    except (OSError, struct.error):
        return None


class McpSocket:
    def __init__(self, cfg: Config, store: Store, path: Path) -> None:
        self.cfg, self.store, self.path = cfg, store, path
        self._caps: dict[str, tuple[str, int]] = {}
        self._lock = threading.Lock()
        self._sock: socket.socket | None = None
        botsmcp.attach(cfg, store)

    # -- capabilities ------------------------------------------------------

    def grant(self, cap: str, bot_id: str, run_id: int) -> None:
        with self._lock:
            self._caps[cap] = (bot_id, run_id)

    def revoke(self, cap: str) -> None:
        with self._lock:
            self._caps.pop(cap, None)

    def caller(self, cap: object) -> botsmcp.Caller | None:
        """The caller a capability names, only while its run is live."""
        if not isinstance(cap, str):
            return None
        with self._lock:
            found = self._caps.get(cap)
        if found is None:
            return None
        run = self.store.get_run(found[1])
        if not run or run["status"] != "running" or run["bot_id"] != found[0]:
            return None
        return botsmcp.Caller(found[0], found[1], isolated=True)

    # -- serving ---------------------------------------------------------

    def start(self) -> None:
        folder = self.path.parent
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        st = folder.stat()
        if st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise RuntimeError(f"{folder} must be a 0700 folder of this user for the MCP socket")
        self.path.unlink(missing_ok=True)  # left by a host that died
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old = os.umask(0o177)  # created 0600, never briefly wider
        try:
            sock.bind(str(self.path))
        finally:
            os.umask(old)
        os.chmod(self.path, 0o600)
        sock.listen(64)
        self._sock = sock
        threading.Thread(target=self._accept, name="mcp-socket", daemon=True).start()
        log.info("isolated mode: MCP socket at %s", self.path)

    def stop(self) -> None:
        if self._sock is not None:
            self._sock.close()
            self._sock = None
        self.path.unlink(missing_ok=True)
        if self.path.parent.name.startswith("bots-"):  # the /tmp folder for a long data path
            with contextlib.suppress(OSError):
                self.path.parent.rmdir()

    def _accept(self) -> None:
        while (sock := self._sock) is not None:
            try:
                conn, _ = sock.accept()
            except OSError:
                return
            if peer_uid(conn) != os.getuid():
                log.warning("MCP socket: dropped a peer that is not this user")
                conn.close()
                continue
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        with conn, conn.makefile("rb") as rfile:
            while line := rfile.readline(MAX_REQUEST + 1):
                if len(line) > MAX_REQUEST:
                    return
                try:
                    with self.store.borrowing():
                        reply = self.handle(line)
                    conn.sendall(json.dumps(reply).encode() + b"\n")
                except OSError:
                    return

    def handle(self, line: bytes) -> dict[str, Any]:
        """One request line to one reply. Never raises: the bot gets a reason instead."""
        try:
            req = json.loads(line)
        except ValueError:
            return {"error": "not JSON"}
        if not isinstance(req, dict):
            return {"error": "not a request"}
        caller = self.caller(req.get("cap"))
        if caller is None:
            return {"error": "this run's capability is not valid (the run ended)"}
        args = req.get("args")
        if not isinstance(req.get("tool"), str) or not isinstance(args, dict):
            return {"error": "a request is {cap, tool, args}"}
        try:
            return {"result": botsmcp.call(caller, req["tool"], args)}
        except Exception as e:
            log.exception("MCP socket: %s for %s failed", req["tool"], caller.bot_id)
            return {"error": f"the host could not run {req['tool']}: {e}"}
