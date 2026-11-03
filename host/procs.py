"""Killing a run's whole process tree.

Claude Code runs each Bash command in its own process group, so signalling only the CLI's
group would orphan what the bot was running."""

from __future__ import annotations

import os
import subprocess


def descendants(pid: int) -> list[int]:
    out = subprocess.run(
        ["ps", "-A", "-o", "pid=,ppid="], check=False, capture_output=True, text=True
    ).stdout
    children: dict[int, list[int]] = {}
    for line in out.splitlines():
        child, parent = map(int, line.split())
        children.setdefault(parent, []).append(child)
    found, stack = [], [pid]
    while stack:
        for child in children.get(stack.pop(), []):
            found.append(child)
            stack.append(child)
    return found


def kill_tree(pgid: int, sig: int) -> None:
    """Signal the group `pgid` (led by the CLI) and every group its descendants are in."""
    groups = {pgid}
    for pid in descendants(pgid):
        try:
            groups.add(os.getpgid(pid))
        except ProcessLookupError:
            pass
    for group in groups:
        try:
            os.killpg(group, sig)
        except (ProcessLookupError, PermissionError):
            pass
