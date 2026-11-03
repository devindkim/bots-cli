"""Claude Code: `claude -p ... --output-format stream-json --verbose`, one JSON object per line.

`result` can appear more than once per process. After the last one the CLI stays alive,
silent, until its background jobs finish."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from host.engines.base import (
    BackgroundTasks,
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

_SUMMARY_KEYS = ("command", "file_path", "path", "pattern", "url", "query", "description")


def summarize(tool_input: Any) -> str:
    """A short human string for a tool call: its command, path, or first argument."""
    if not isinstance(tool_input, dict):
        return ""
    for key in _SUMMARY_KEYS:
        if isinstance(tool_input.get(key), str):
            return short(tool_input[key])
    return short(json.dumps(tool_input)) if tool_input else ""


class ClaudeEngine:
    name = "claude"

    def __init__(self, binary: str, model: str | None = None) -> None:
        self.binary = binary
        self.model = model  # None: whatever ~/.claude/settings.json says

    def isolation(self, home: Path, tmp: Path) -> Isolation:
        """Its own config dir (settings, sessions, login) in the bot's engine home. Logged in
        by ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN in the host's env: the keychain is
        closed to the sandbox."""
        env = {
            "CLAUDE_CONFIG_DIR": str(home / ".claude"),
            "CLAUDE_CODE_TMPDIR": str(tmp),  # else /tmp/claude-<uid>, shared by every claude
        }
        return Isolation(env, install_paths(self.binary))

    def command(
        self, prompt: str, cwd: Path, mcp: McpServer, stdin: bool = False
    ) -> tuple[list[str], dict[str, str]]:
        servers = {
            "bots": {"type": "stdio", "command": mcp.command, "args": mcp.args, "env": mcp.env}
        }
        argv = [
            self.binary,
            "-p",
            *([] if stdin else [prompt]),
            "--output-format",
            "stream-json",
            "--verbose",
            "--permission-mode",
            "bypassPermissions",
            "--mcp-config",
            json.dumps({"mcpServers": servers}),
            "--strict-mcp-config",
            *(["--model", self.model] if self.model else []),
        ]
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE" and k not in host_secrets()}
        return argv, env

    def parse(self, line: str) -> list[Event]:
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            return []
        if not isinstance(ev, dict):
            return []
        kind = ev.get("type")
        parent = ev.get("parent_tool_use_id")
        # A dict on assistant/user; a plain string on some system events.
        message = ev.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        blocks = content if isinstance(content, list) else []
        out: list[Event] = []
        if kind == "assistant":
            for b in blocks:
                if b.get("type") == "tool_use":
                    out.append(
                        ToolStart(b["id"], b.get("name", "?"), summarize(b.get("input")), parent)
                    )
                elif b.get("type") == "text" and b.get("text") and not parent:
                    out.append(Text(b["text"]))
        elif kind == "user":
            out += [ToolEnd(b["tool_use_id"]) for b in blocks if b.get("type") == "tool_result"]
        elif kind == "rate_limit_event":
            info = ev.get("rate_limit_info") or {}
            # A rejected window the account is paying overage past still runs the turn.
            if info.get("status") not in (None, "allowed", "allowed_warning") and not info.get(
                "isUsingOverage"
            ):
                reset = info.get("resetsAt")
                out.append(RateLimited(float(reset) if isinstance(reset, int | float) else None))
        elif kind == "system" and ev.get("subtype") == "background_tasks_changed":
            tasks = ev.get("tasks")
            tasks = tasks if isinstance(tasks, list) else []
            ids = (t.get("task_id") for t in tasks if isinstance(t, dict))
            out.append(BackgroundTasks(tuple(str(i) for i in ids if i)))
        elif kind == "result":
            out.append(Result(ev.get("session_id"), ev.get("total_cost_usd")))
        return out
