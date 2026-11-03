"""`bots install` and `bots uninstall`: run the host as a launchd or systemd user service."""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from host.config import OPEN_FILES, Config
from host.engines import engines

# Searched after the folders the engines were found in.
SYSTEM_PATH = {
    "darwin": ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"],
    "linux": ["/usr/local/bin", "/usr/bin", "/bin", "/usr/local/sbin", "/usr/sbin", "/sbin"],
}


def kind(platform: str | None = None) -> str:
    """ "launchd" or "systemd"; an error anywhere else."""
    platform = platform or sys.platform
    if platform == "darwin":
        return "launchd"
    if platform.startswith("linux"):
        return "systemd"
    raise RuntimeError(
        f"`bots install` knows launchd (macOS) and systemd (Linux), not {platform!r}; "
        "run `bots serve` under your own supervisor"
    )


def service_path(cfg: Config, platform: str | None = None) -> Path:
    if kind(platform) == "launchd":
        return Path.home() / "Library" / "LaunchAgents" / f"{cfg.launchd_label}.plist"
    config_home = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(config_home) / "systemd" / "user" / unit_name(cfg)


def unit_name(cfg: Config) -> str:
    return f"{cfg.launchd_label}.service"


def search_path(cfg: Config, platform: str | None = None) -> str:
    # The engines' folders as found now (cfg.engine_binary): a service manager's PATH has
    # neither ~/.local/bin nor most install locations, so the host would not find them
    # itself, or would find a different copy.
    binaries = [cfg.engine_binary(n, s.command) for n, s in engines(cfg).items() if s.command]
    found = [str(Path(b).parent) for b in binaries if b and Path(b).is_absolute()]
    system = SYSTEM_PATH["darwin" if kind(platform) == "launchd" else "linux"]
    return ":".join(dict.fromkeys([str(Path(sys.executable).parent), *found, *system]))


def environment(cfg: Config, platform: str | None = None) -> dict[str, str]:
    return {
        "PATH": search_path(cfg, platform),
        "HOME": str(Path.home()),
        "BOTS_HOME": str(cfg.root),
    }


def program(cfg: Config) -> list[str]:
    return [str(Path(sys.executable).parent / "bots"), "serve"]


# -- launchd -----------------------------------------------------------------


def plist(cfg: Config) -> dict[str, Any]:
    log = str(cfg.logs_dir / "host.log")
    return {
        "Label": cfg.launchd_label,
        "ProgramArguments": program(cfg),
        "WorkingDirectory": str(cfg.root),
        "EnvironmentVariables": environment(cfg, "darwin"),
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": log,
        "StandardErrorPath": log,
        # launchd's default is 256 (host/config.py OPEN_FILES).
        "SoftResourceLimits": {"NumberOfFiles": OPEN_FILES},
    }


# -- systemd -----------------------------------------------------------------


def _value(s: str) -> str:
    """A path or word as one systemd value: `%` is a specifier, so it is doubled. Newlines
    can't be written in a unit at all."""
    if "\n" in s or "\r" in s:
        raise ValueError(f"not allowed in a systemd unit: {s!r}")
    return s.replace("%", "%%")


def _quoted(s: str, dollar: bool = False) -> str:
    """One quoted word for ExecStart= (`dollar`: `$` expands there) or Environment=."""
    escaped = _value(s).replace("\\", "\\\\").replace('"', '\\"')
    if dollar:
        escaped = escaped.replace("$", "$$")
    return f'"{escaped}"'


def unit(cfg: Config) -> str:
    log = _value(str(cfg.logs_dir / "host.log"))
    env = " ".join(_quoted(f"{k}={v}") for k, v in environment(cfg, "linux").items())
    return "\n".join(
        [
            "[Unit]",
            f"Description=bots host for {_value(str(cfg.root))}",
            "Wants=network-online.target",
            "After=network-online.target",
            "",
            "[Service]",
            "Type=simple",
            "ExecStart=" + " ".join(_quoted(a, dollar=True) for a in program(cfg)),
            f"WorkingDirectory={_value(str(cfg.root))}",
            f"Environment={env}",
            "Restart=always",
            "RestartSec=5",
            # SIGTERM to the host alone, as launchd does: it stops its runs itself
            # (host/serve.py), well inside the timeout; what is left after it gets SIGKILL.
            "KillMode=mixed",
            "TimeoutStopSec=30",
            f"LimitNOFILE={OPEN_FILES}",
            f"StandardOutput=append:{log}",
            f"StandardError=append:{log}",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
    )


def lingering() -> bool | None:
    """Whether this user's services run without a login session; None when unknown."""
    if not shutil.which("loginctl"):
        return None
    out = subprocess.run(
        ["loginctl", "show-user", str(os.getuid()), "--property=Linger", "--value"],
        check=False,
        capture_output=True,
        text=True,
    )
    value = out.stdout.strip()
    return {"yes": True, "no": False}.get(value)


# -- the commands ------------------------------------------------------------


def text(cfg: Config) -> str:
    """What `bots install --print` shows: the file `bots install` would write."""
    if kind() == "launchd":
        return plistlib.dumps(plist(cfg)).decode()
    return unit(cfg)


def _bootout(cfg: Config) -> None:
    """Unload the launchd job if it is loaded; not loaded is fine."""
    subprocess.run(
        ["launchctl", "bootout", f"gui/{os.getuid()}/{cfg.launchd_label}"],
        check=False,
        capture_output=True,
    )


def install(cfg: Config) -> str:
    data = text(cfg)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    path = service_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data)
    if kind() == "launchd":
        _bootout(cfg)
        subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)], check=True)
        return f"loaded {path}; logs in {cfg.logs_dir / 'host.log'}"
    systemctl = ["systemctl", "--user"]
    subprocess.run([*systemctl, "daemon-reload"], check=True)
    subprocess.run([*systemctl, "enable", unit_name(cfg)], check=True)
    subprocess.run([*systemctl, "restart", unit_name(cfg)], check=True)
    done = f"started {path}; logs in {cfg.logs_dir / 'host.log'}"
    if lingering() is False:
        done += (
            "\nit stops when you log out; to keep it running: "
            f"loginctl enable-linger {os.environ.get('USER') or os.getuid()}"
        )
    return done


def uninstall(cfg: Config) -> str:
    path = service_path(cfg)
    if kind() == "launchd":
        _bootout(cfg)
        path.unlink(missing_ok=True)
    else:
        systemctl = ["systemctl", "--user"]
        subprocess.run(
            [*systemctl, "disable", "--now", unit_name(cfg)], check=False, capture_output=True
        )
        path.unlink(missing_ok=True)
        subprocess.run([*systemctl, "daemon-reload"], check=False, capture_output=True)
    return f"unloaded and removed {path}"
