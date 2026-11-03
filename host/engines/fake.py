"""The scripted engine: runs fake_cli.py, which plays bots/<id>/fake_script.json."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

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
)


class FakeEngine:
    name = "fake"

    def isolation(self, home: Path, tmp: Path) -> Isolation:
        return Isolation({})  # runs on the host's own Python, which the sandbox lets it read

    def command(
        self, prompt: str, cwd: Path, mcp: McpServer, stdin: bool = False
    ) -> tuple[list[str], dict[str, str]]:
        argv = [sys.executable, "-m", "host.engines.fake_cli", "--mcp", json.dumps(asdict(mcp))]
        return [*argv, "-" if stdin else prompt], dict(os.environ)

    def parse(self, line: str) -> list[Event]:
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            return []
        match ev.get("e"):
            case "tool_start":
                return [ToolStart(ev["id"], ev["name"], ev.get("summary", ""))]
            case "tool_end":
                return [ToolEnd(ev["id"])]
            case "text":
                return [Text(ev["chunk"])]
            case "rate_limited":
                return [RateLimited(ev.get("reset_at"))]
            case "background":
                return [BackgroundTasks(tuple(ev["ids"]))]
            case "result":
                return [Result(ev.get("session_id"), cost=ev.get("cost"))]
        return []
