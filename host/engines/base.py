"""The engine contract. One adapter per CLI.

command(prompt, cwd, mcp)   -> argv, env
parse(stdout_line)          -> events
isolation(home, tmp)        optional: what it needs to run sandboxed (Isolating)"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from host import plugins


@dataclass(frozen=True)
class ToolStart:
    id: str
    name: str
    summary: str
    parent_id: str | None = None  # set for subagent calls; not the main thread


@dataclass(frozen=True)
class ToolEnd:
    id: str


@dataclass(frozen=True)
class Text:
    chunk: str


@dataclass(frozen=True)
class RateLimited:
    reset_at: float | None  # unix time, or None when the engine did not say


@dataclass(frozen=True)
class BackgroundTasks:
    """The engine's whole current set of background jobs (shells, subagents), each time it
    changes; empty when none is left. While one runs, the engine waits on it in silence."""

    ids: tuple[str, ...]


@dataclass(frozen=True)
class Result:
    session_id: str | None
    cost: float | None = None


# Credentials a kernel CLI may get in its env.
CHILD_SECRETS = frozenset(
    {
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
    }
)


def host_secrets() -> frozenset[str]:
    """Keys the host holds for a plugin's engine (Plugin.host_secrets). No CLI inherits
    them: a bot's shell would see them."""
    return plugins.host_secrets()


def secrets() -> frozenset[str]:
    """Every credential name. Their values are cut from a CLI's output before the host logs,
    parses or publishes a line, so a CLI that echoes its token leaks it nowhere."""
    return CHILD_SECRETS | plugins.host_secrets()


REDACTED = "[redacted]"


def redactor(env: dict[str, str], extra: tuple[str, ...] = ()) -> Callable[[str], str]:
    """Replaces every credential value in `env`, and each of `extra`, with REDACTED. Values
    under 8 characters are left alone: no real key is that short, and cutting them would
    mangle ordinary text."""
    names = secrets()
    found = {v for k, v in env.items() if k in names} | set(extra)
    values = sorted({v for v in found if len(v) >= 8}, key=len)
    values.reverse()  # longest first, so a key that contains another is cut whole

    def redact(text: str) -> str:
        for v in values:
            text = text.replace(v, REDACTED)
        return text

    return redact


Event = ToolStart | ToolEnd | Text | RateLimited | BackgroundTasks | Result


def short(s: str, n: int = 120) -> str:
    """s on one line, cut to n characters with an ellipsis: a tool call's summary."""
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


@dataclass(frozen=True)
class McpServer:
    """How the CLI launches botsmcp over stdio."""

    command: str
    args: list[str]
    env: dict[str, str]


class Engine(Protocol):
    name: str

    def command(
        self, prompt: str, cwd: Path, mcp: McpServer
    ) -> tuple[list[str], dict[str, str]]: ...

    def parse(self, line: str) -> list[Event]: ...


@dataclass(frozen=True)
class Isolation:
    """What an engine needs to run inside the sandbox (host/sandbox.py)."""

    env: dict[str, str]  # where its state lives under the bot's engine home
    read: tuple[Path, ...] = ()  # its install, when that is under $HOME


@runtime_checkable
class Isolating(Engine, Protocol):
    """An engine that can run isolated. Its prompt goes on stdin there (`stdin=True`), never
    in argv, which any process of the user can read (KERN_PROCARGS2), sandboxed or not."""

    def isolation(self, home: Path, tmp: Path) -> Isolation: ...

    def command(
        self, prompt: str, cwd: Path, mcp: McpServer, stdin: bool = False
    ) -> tuple[list[str], dict[str, str]]: ...


def install_paths(binary: str, levels: int = 1) -> tuple[Path, ...]:
    """An engine's binary as found (often a link, like ~/.local/bin/claude) and the folder
    it is installed in, links resolved, `levels` up: what the sandbox must let it read."""
    found = Path(shutil.which(binary) or binary)
    return found, Path(os.path.realpath(found)).parents[levels - 1]
