from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

CLI = "import sys; from cli.main import main; sys.exit(main(sys.argv[1:]))"


def bots(*args: str) -> list[str]:
    return [sys.executable, "-c", CLI, *args]


def test_a_second_host_refuses_and_leaves_the_first_running(tmp_path: Path) -> None:
    env = {**os.environ, "BOTS_HOME": str(tmp_path / "data")}
    subprocess.run(bots("init", "--engine", "fake"), env=env, check=True, capture_output=True)
    first = subprocess.Popen(bots("serve"), env=env, stderr=subprocess.PIPE, text=True)
    try:
        assert first.stderr is not None
        while "serving" not in first.stderr.readline():
            assert first.poll() is None
        second = subprocess.run(
            bots("serve"), env=env, capture_output=True, text=True, timeout=10, check=False
        )
        assert second.returncode == 1
        assert "already serving" in second.stderr
        assert first.poll() is None
    finally:
        first.terminate()
        first.wait(timeout=10)
