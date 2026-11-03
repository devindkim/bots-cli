"""`bots install` as a systemd user unit."""

import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from cli import service
from cli.main import main
from host.config import OPEN_FILES, Config


@pytest.fixture
def unit_path(cfg: Config, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Linux, $BOTS_HOME at the test's data folder, $XDG_CONFIG_HOME in a temp folder;
    returns where the unit goes."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("BOTS_HOME", str(cfg.root))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    return tmp_path / "xdg" / "systemd" / "user" / "bots-cli.host.service"


def settings(unit: str) -> dict[str, str]:
    """Key=value lines of the [Service] section."""
    section = unit.split("[Service]\n")[1].split("\n[")[0]
    return dict(line.split("=", 1) for line in section.splitlines() if "=" in line)


@pytest.fixture
def systemctl(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        out = "no\n" if cmd[0] == "loginctl" else ""
        return subprocess.CompletedProcess(cmd, 0, out, "")

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    return calls


def test_install_print_on_linux_is_a_user_unit(
    cfg: Config,
    unit_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bin_dir = cfg.root / "engine-bin"  # where the engine CLIs were installed
    bin_dir.mkdir()
    for name in ("claude", "codex"):
        (bin_dir / name).write_text("#!/bin/sh\n")
        (bin_dir / name).chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")

    assert main(["install", "--print"]) == 0

    unit = capsys.readouterr().out
    assert not unit_path.exists()  # --print writes nothing
    assert unit.startswith("[Unit]\n") and "\n[Install]\nWantedBy=default.target\n" in unit
    s = settings(unit)
    assert s["ExecStart"] == f'"{Path(sys.executable).parent}/bots" "serve"'
    assert s["WorkingDirectory"] == str(cfg.root)
    assert s["Restart"] == "always"
    assert s["KillMode"] == "mixed"  # SIGTERM to the host only; it stops its own runs
    assert s["LimitNOFILE"] == str(OPEN_FILES)
    log = cfg.root / "logs" / "host.log"
    assert s["StandardOutput"] == s["StandardError"] == f"append:{log}"
    env = dict(w.strip('"').split("=", 1) for w in s["Environment"].split('" "'))
    assert env["BOTS_HOME"] == str(cfg.root) and env["HOME"] == str(Path.home())
    path = env["PATH"].split(":")
    assert str(bin_dir) in path
    assert not any("homebrew" in p for p in path)
    assert path[-3:] == ["/usr/local/sbin", "/usr/sbin", "/sbin"]


def test_unit_escapes_what_systemd_would_expand(cfg: Config, unit_path: Path) -> None:
    odd = replace(cfg, root=cfg.root / 'a b%h$USER"q')
    s = settings(service.unit(odd))
    assert s["WorkingDirectory"] == f'{cfg.root}/a b%%h$USER"q'  # no specifier, no quotes
    bots_home = next(w for w in s["Environment"].split('" "') if "BOTS_HOME=" in w)
    assert bots_home == f'BOTS_HOME={cfg.root}/a b%%h$USER\\"q"'  # no $ expansion
    with pytest.raises(ValueError, match="systemd unit"):
        service.unit(replace(cfg, root=cfg.root / "line\nbreak"))


def test_exec_start_doubles_dollar_signs(monkeypatch: pytest.MonkeyPatch, cfg: Config) -> None:
    monkeypatch.setattr(service, "program", lambda cfg: ["/opt/$x/bots", "serve"])
    assert settings(service.unit(cfg))["ExecStart"] == '"/opt/$$x/bots" "serve"'


def test_install_on_linux_writes_enables_and_restarts(
    cfg: Config,
    unit_path: Path,
    systemctl: list[list[str]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["install"]) == 0

    assert unit_path.read_text() == service.unit(cfg)
    unit = "bots-cli.host.service"
    assert systemctl[:3] == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", unit],
        ["systemctl", "--user", "restart", unit],  # a second install picks up the new unit
    ]
    out = capsys.readouterr().out
    assert f"started {unit_path}" in out
    assert "loginctl enable-linger" in out  # lingering is off: say how to outlive a logout
    assert (cfg.root / "logs").is_dir()


def test_uninstall_on_linux_stops_and_removes_the_unit(
    cfg: Config, unit_path: Path, systemctl: list[list[str]]
) -> None:
    assert main(["install"]) == 0
    systemctl.clear()

    assert main(["uninstall"]) == 0

    assert not unit_path.exists()
    assert systemctl == [
        ["systemctl", "--user", "disable", "--now", "bots-cli.host.service"],
        ["systemctl", "--user", "daemon-reload"],
    ]


def test_install_elsewhere_says_to_use_serve(
    cfg: Config, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "platform", "freebsd14")
    monkeypatch.setenv("BOTS_HOME", str(cfg.root))
    assert main(["install", "--print"]) == 1
    assert "run `bots serve` under your own supervisor" in capsys.readouterr().err
