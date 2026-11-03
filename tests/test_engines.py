"""Parsing sample stream lines from each real CLI (shapes from notes/research.md)."""

import json
from pathlib import Path

from host.config import Config
from host.engines import engine_names
from host.engines.base import (
    BackgroundTasks,
    McpServer,
    RateLimited,
    Result,
    Text,
    ToolEnd,
    ToolStart,
)
from host.engines.claude import ClaudeEngine
from host.engines.codex import CodexEngine

MCP = McpServer("/usr/bin/python3", ["-m", "botsmcp"], {"BOT_ID": "ada", "BOT_RUN_ID": "1"})


def lines(*events: dict[str, object]) -> list[str]:
    return [json.dumps(e) for e in events]


def test_claude_parse_and_command(tmp_path: Path) -> None:
    engine = ClaudeEngine("/bin/claude")
    stream = lines(
        {"type": "system", "subtype": "init", "session_id": "s1"},
        {
            "type": "assistant",
            "parent_tool_use_id": None,
            "message": {
                "content": [
                    {"type": "text", "text": "Looking."},
                    {
                        "type": "tool_use",
                        "id": "tu1",
                        "name": "Bash",
                        "input": {"command": "ls -la"},
                    },
                ]
            },
        },
        {
            "type": "user",
            "parent_tool_use_id": None,
            "message": {"content": [{"type": "tool_result", "tool_use_id": "tu1", "content": "x"}]},
        },
        {
            "type": "assistant",
            "parent_tool_use_id": "agent1",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tu2",
                        "name": "Read",
                        "input": {"file_path": "/a.py"},
                    }
                ]
            },
        },
        {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "resetsAt": 1}},
        {
            "type": "rate_limit_event",
            "rate_limit_info": {"status": "rejected", "resetsAt": 1767225600},
        },
        {
            "type": "rate_limit_event",
            "rate_limit_info": {"status": "rejected", "resetsAt": 2, "isUsingOverage": True},
        },
        {
            "type": "result",
            "session_id": "s1",
            "total_cost_usd": 0.12,
            "usage": {"input_tokens": 5},
        },
    )
    events = [ev for line in [*stream, "not json"] for ev in engine.parse(line)]
    assert events == [
        Text("Looking."),
        ToolStart("tu1", "Bash", "ls -la"),
        ToolEnd("tu1"),
        ToolStart("tu2", "Read", "/a.py", parent_id="agent1"),
        RateLimited(1767225600.0),
        Result("s1", 0.12),
    ]

    argv, env = engine.command("hi", tmp_path, MCP)
    assert argv[:3] == ["/bin/claude", "-p", "hi"] and "--strict-mcp-config" in argv
    config = json.loads(argv[argv.index("--mcp-config") + 1])
    assert config["mcpServers"]["bots"]["env"]["BOT_ID"] == "ada"
    assert "CLAUDECODE" not in env


def test_codex_parse_and_command(tmp_path: Path) -> None:
    engine = CodexEngine("/bin/codex")
    cmd = {
        "id": "i1",
        "type": "command_execution",
        "command": "bash -lc ls",
        "status": "in_progress",
    }
    stream = lines(
        {"type": "thread.started", "thread_id": "th1"},
        {"type": "turn.started"},
        {"type": "item.started", "item": cmd},
        {"type": "item.completed", "item": {**cmd, "status": "completed", "exit_code": 0}},
        {
            "type": "item.completed",
            "item": {"id": "i2", "type": "file_change", "changes": [{"path": "a.py"}]},
        },
        {"type": "item.completed", "item": {"id": "i3", "type": "agent_message", "text": "Done."}},
        {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 2}},
    )
    events = [ev for line in stream for ev in engine.parse(line)]
    assert events == [
        Result("th1"),
        ToolStart("i1", "shell", "bash -lc ls"),
        ToolEnd("i1"),
        ToolStart("i2", "edit", "a.py"),
        ToolEnd("i2"),
        Text("Done."),
        Result(None),
    ]

    argv, _ = engine.command("hi", tmp_path, MCP)
    assert argv[:2] == ["/bin/codex", "exec"] and argv[-1] == "hi"
    assert 'mcp_servers.bots.env={BOT_ID = "ada", BOT_RUN_ID = "1"}' in argv


def test_the_kernel_engines_are_loaded_without_plugins(cfg: Config) -> None:
    assert engine_names(cfg) == ("claude", "codex", "fake")


def test_claude_parse_ignores_system_events_with_string_message() -> None:
    from host.engines.claude import ClaudeEngine

    line = '{"type":"system","subtype":"permission_denied","message":"denied"}'
    assert ClaudeEngine("claude").parse(line) == []


def test_claude_parse_reads_the_background_task_set() -> None:
    # copied from a real run's stdout.log (descriptions cut)
    started = (
        '{"type":"system","subtype":"background_tasks_changed","tasks":[{"task_id":"bua1kq6g0",'
        '"run_id":"0mvsqz9c2-eb0c8c5b","task_type":"local_bash","description":"uv run fx panel"}'
        '],"uuid":"85a71651-3cd2-45be-a963-97be2bace89a",'
        '"session_id":"71ef0646-b16a-4470-b6bc-199044dc488c"}'
    )
    ended = (
        '{"type":"system","subtype":"background_tasks_changed","tasks":[],'
        '"uuid":"84f17d20-fe8b-4d18-902c-9712dd7f04c0",'
        '"session_id":"71ef0646-b16a-4470-b6bc-199044dc488c"}'
    )
    engine = ClaudeEngine("claude")
    assert engine.parse(started) == [BackgroundTasks(("bua1kq6g0",))]
    assert engine.parse(ended) == [BackgroundTasks(())]
    odd = '{"type":"system","subtype":"background_tasks_changed","tasks":[{"x":1},"y"]}'
    assert engine.parse(odd) == [BackgroundTasks(())]


def test_claude_passes_a_model_only_when_the_bot_has_one(tmp_path: Path) -> None:
    default, _ = ClaudeEngine("/bin/claude").command("hi", tmp_path, MCP)
    assert "--model" not in default
    argv, _ = ClaudeEngine("/bin/claude", "sonnet").command("hi", tmp_path, MCP)
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert argv[argv.index("-p") + 1] == "hi"


def test_codex_passes_a_model_only_when_the_bot_has_one(tmp_path: Path) -> None:
    default, _ = CodexEngine("/bin/codex").command("hi", tmp_path, MCP)
    assert "-m" not in default
    argv, _ = CodexEngine("/bin/codex", "gpt-6.1-sol").command("hi", tmp_path, MCP)
    assert argv[argv.index("-m") + 1] == "gpt-6.1-sol"
    assert argv[-1] == "hi"  # the prompt stays the last positional
