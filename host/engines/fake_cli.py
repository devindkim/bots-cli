"""A scripted stand-in for a coding-agent CLI, so the host is tested without spending a turn.

Run as `python -m host.engines.fake_cli --mcp <json> <prompt>` in the bot folder. It writes
the prompt to `last_prompt.txt` and plays `fake_script.json` there: a list of steps,

    {"tool": "send_to_user", "args": {"body": "hi"}}   a real MCP call to botsmcp
    {"open": "Bash", "summary": "npm test", "sleep": 3} a tool call left open (never a hang)
    {"open": "Bash", "sleep": 3, "detached": true}      ...running a child in its own session,
                                                        as Claude Code's Bash does (pid in child.pid)
    {"open": "Bash", "until": "done"}                   ...one that ends when the file `done` exists
    {"shell": "cat ../../bots.sqlite3"}                  a shell command; output in its tool_end
    {"sleep": 5}                                        silence (a hang, if long enough)
    {"until": "done"}                                   silence until the file `done` exists
    {"text": "thinking"}
    {"rate_limit": 1767225600}                          reset time, or null
    {"background": ["b1"]}                              the engine's set of background jobs
    {"exit": 1}                                         stop here with this code
    {"cost": 0.25}                                      what the result reports (else 0.0)

printing one JSON event per line (see fake.py). Without an exit step it emits a result
and exits 0.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


def emit(**event: Any) -> None:
    print(json.dumps(event), flush=True)


async def play(steps: list[dict[str, Any]], mcp: dict[str, Any]) -> int:
    async with AsyncExitStack() as stack:
        session: ClientSession | None = None
        if any("tool" in s for s in steps):
            params = StdioServerParameters(command=mcp["command"], args=mcp["args"], env=mcp["env"])
            read, write = await stack.enter_async_context(stdio_client(params))
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
        cost = 0.0
        for n, step in enumerate(steps):
            tool_id = f"t{n}"
            if "tool" in step:
                assert session is not None
                emit(
                    e="tool_start", id=tool_id, name=step["tool"], summary=json.dumps(step["args"])
                )
                result = await session.call_tool(step["tool"], step.get("args", {}))
                text = "".join(getattr(c, "text", "") for c in result.content)
                emit(e="tool_end", id=tool_id, output=text)
            elif "open" in step:
                emit(e="tool_start", id=tool_id, name=step["open"], summary=step.get("summary", ""))
                if step.get("detached"):
                    child = await asyncio.create_subprocess_exec(
                        "sleep", str(step["sleep"]), start_new_session=True
                    )
                    Path("child.pid").write_text(str(child.pid))
                await asyncio.sleep(step.get("sleep", 0))
                while "until" in step and not Path(step["until"]).exists():
                    await asyncio.sleep(0.02)
                emit(e="tool_end", id=tool_id)
            elif "shell" in step:
                emit(e="tool_start", id=tool_id, name="Bash", summary=step["shell"])
                proc = await asyncio.create_subprocess_shell(
                    step["shell"], stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
                )
                out, _ = await proc.communicate()
                emit(
                    e="tool_end",
                    id=tool_id,
                    output=out.decode(errors="replace"),
                    code=proc.returncode,
                )
            elif "sleep" in step:
                await asyncio.sleep(step["sleep"])
            elif "until" in step:
                while not Path(step["until"]).exists():
                    await asyncio.sleep(0.02)
            elif "text" in step:
                emit(e="text", chunk=step["text"])
            elif "background" in step:
                emit(e="background", ids=step["background"])
            elif "rate_limit" in step:
                emit(e="rate_limited", reset_at=step["rate_limit"])
            elif "exit" in step:
                return int(step["exit"])
            elif "cost" in step:
                cost = float(step["cost"])
    emit(e="result", session_id=f"fake-{int(time.time() * 1000)}", cost=cost)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mcp", required=True)
    parser.add_argument("prompt")
    args = parser.parse_args()
    prompt = sys.stdin.read() if args.prompt == "-" else args.prompt
    Path("last_prompt.txt").write_text(prompt)
    script = Path("fake_script.json")
    steps = json.loads(script.read_text()) if script.exists() else []
    sys.exit(asyncio.run(play(steps, json.loads(args.mcp))))


if __name__ == "__main__":
    main()
