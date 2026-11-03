"""Codex: `codex exec --json`, one JSON event per line.

It blocks on an open stdin, so the host gives it /dev/null. Our MCP server is added with -c
overrides on top of ~/.codex/config.toml."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from host.engines.base import (
    Event,
    Isolation,
    McpServer,
    RateLimited,
    Result,
    Text,
    ToolEnd,
    ToolStart,
    host_secrets,
    install_paths,
    short,
)

_TOOL_ITEMS = ("command_execution", "mcp_tool_call", "file_change", "web_search", "todo_list")


def _toml(value: str) -> str:
    return json.dumps(value)  # a JSON string is a valid TOML basic string


def describe(item: dict[str, Any]) -> tuple[str, str]:
    """(name, summary) for a tool-like item."""
    kind = item.get("type", "?")
    if kind == "command_execution":
        return "shell", short(str(item.get("command", "")))
    if kind == "mcp_tool_call":
        return f"{item.get('server', '?')}.{item.get('tool', '?')}", short(
            json.dumps(item.get("arguments") or {})
        )
    if kind == "file_change":
        return "edit", short(", ".join(c.get("path", "") for c in item.get("changes") or []))
    if kind == "web_search":
        return "web_search", short(str(item.get("query", "")))
    return kind, ""


class CodexEngine:
    name = "codex"

    def __init__(self, binary: str, model: str | None = None) -> None:
        self.binary = binary
        self.model = model  # None: whatever ~/.codex/config.toml or the CLI defaults to
        self._started: set[str] = set()  # one engine per turn, so per-run state is fine

    def isolation(self, home: Path, tmp: Path) -> Isolation:
        """Its own CODEX_HOME (config, sessions, auth.json) in the bot's engine home: log in
        once per bot (`codex login --with-api-key` with CODEX_HOME set), or set CODEX_API_KEY
        in the host's env. Its install is the npm package the binary links into."""
        (home / ".codex").mkdir(exist_ok=True)  # codex refuses a CODEX_HOME that isn't there
        return Isolation({"CODEX_HOME": str(home / ".codex")}, install_paths(self.binary, 2))

    def command(
        self, prompt: str, cwd: Path, mcp: McpServer, stdin: bool = False
    ) -> tuple[list[str], dict[str, str]]:
        env = ", ".join(f"{k} = {_toml(v)}" for k, v in mcp.env.items())
        argv = [
            self.binary,
            "exec",
            "--json",
            "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",
            "-C",
            str(cwd),
            "-c",
            f"mcp_servers.bots.command={_toml(mcp.command)}",
            "-c",
            f"mcp_servers.bots.args=[{', '.join(_toml(a) for a in mcp.args)}]",
            "-c",
            f"mcp_servers.bots.env={{{env}}}",
            *(["-m", self.model] if self.model else []),
            "-" if stdin else prompt,  # "-": read the prompt from stdin
        ]
        return argv, {k: v for k, v in os.environ.items() if k not in host_secrets()}

    def parse(self, line: str) -> list[Event]:
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            return []
        if not isinstance(ev, dict):
            return []
        kind = ev.get("type")
        item = ev.get("item") or {}
        item_id = str(item.get("id", ""))
        if kind == "thread.started":
            return [Result(ev.get("thread_id"))]
        if kind == "item.started" and item.get("type") in _TOOL_ITEMS:
            self._started.add(item_id)
            return [ToolStart(item_id, *describe(item))]
        if kind == "item.completed":
            if item.get("type") == "agent_message" and item.get("text"):
                return [Text(item["text"])]
            if item.get("type") in _TOOL_ITEMS:
                start = [] if item_id in self._started else [ToolStart(item_id, *describe(item))]
                return [*start, ToolEnd(item_id)]
        if kind == "turn.completed":
            return [Result(None)]
        if kind in ("turn.failed", "error"):
            message = str((ev.get("error") or {}).get("message") or ev.get("message") or "")
            if "limit" in message.lower():
                return [RateLimited(None)]
        return []
