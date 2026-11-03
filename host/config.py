"""config.toml in the data directory ($BOTS_HOME, else ~/.bots). The repo holds only code."""

from __future__ import annotations

import os
import shutil
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HOME = Path.home() / ".bots"
# launchd's default soft limit is 256 open files, shared by every turn's pipes and logs.
OPEN_FILES = 4096

_UNITS = {"s": 1, "m": 60, "h": 3600}


def seconds(value: str | float) -> float:
    """10, "10s", "10m", "2h" -> seconds."""
    if isinstance(value, int | float):
        return float(value)
    value = value.strip()
    if value[-1] in _UNITS:
        return float(value[:-1]) * _UNITS[value[-1]]
    return float(value)


@dataclass(frozen=True)
class Config:
    root: Path = HOME
    max_concurrent: int = 2
    idle_timeout: float = 600
    # Silence allowed while the engine waits on a background job: Bash's own 2h cap, plus margin.
    background_timeout: float = 7800
    rate_limit_backoff: float = 1800
    failure_backoff: float = 60  # first retry after a failed run; doubles, at most an hour
    daily_budget: float = 0.0  # USD for all bots per local day, engine-reported; 0: none
    envelope_context: int = 10
    group_sends_per_turn: int = 3
    notify: bool = True  # desktop notification on each send_to_user; tests turn it off
    tick: float = 1.0  # scheduler poll interval
    # The service name for `bots install`: launchd's label and the systemd unit's stem.
    launchd_label: str = "bots-cli.host"
    # Engine binaries by engine name; unset ones are found on the PATH (engine_binary).
    engines: dict[str, str] = field(default_factory=dict)
    # Plugin modules to load, in order (host/plugins.py), e.g. ["my_plugins.weather"].
    plugins: list[str] = field(default_factory=list)
    # "none": engines run as the user, with the user's access (the default). "seatbelt": each
    # run inside a macOS sandbox that fences off the data folder and $HOME (host/sandbox.py).
    # "bwrap": the same on Linux, in bubblewrap.
    isolation: str = "none"
    # What an isolated run shares with the other bots. "shared": every bot folder and shared/,
    # but the files a bot's prompt is built from stay its own (host/sandbox.py). "bot": its
    # own folder and shared/ only.
    isolation_scope: str = "shared"
    # Extra folders an isolated run may read, e.g. a toolchain under $HOME ("~/.nvm").
    isolation_read: list[str] = field(default_factory=list)

    @property
    def db_path(self) -> Path:
        return self.root / "bots.sqlite3"

    @property
    def bots_dir(self) -> Path:
        return self.root / "bots"

    @property
    def shared_dir(self) -> Path:
        return self.root / "shared"

    @property
    def secrets_dir(self) -> Path:
        return self.root / "secrets"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    def bot_dir(self, bot_id: str) -> Path:
        return self.bots_dir / bot_id

    def engine_binary(self, engine: str, command: str = "") -> str:
        """config.toml's [engines] path, else the engine's command (default: its name) on the
        PATH, else the bare command, so a missing CLI fails its run with "not found" like any
        failed spawn."""
        if path := self.engines.get(engine):
            return os.path.expanduser(path)
        command = command or engine
        return shutil.which(command) or command


_DURATIONS = {"idle_timeout", "background_timeout", "rate_limit_backoff", "failure_backoff", "tick"}


def load(root: Path | None = None) -> Config:
    """Read <root>/config.toml over the defaults. Root is $BOTS_HOME, else ~/.bots."""
    root = Path(root or os.environ.get("BOTS_HOME") or HOME).resolve()
    path = root / "config.toml"
    try:
        raw = tomllib.loads(path.read_text()) if path.exists() else {}
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"config.toml: {e}") from e
    defaults = Config()
    known = {f.name for f in fields(Config)} - {"root"}
    values: dict[str, object] = {}
    for key, value in raw.items():
        if key not in known:
            raise ValueError(f"config.toml: unknown key {key!r}")
        values[key] = _value(key, value, getattr(defaults, key))
    return Config(root=root, **values)  # type: ignore[arg-type]


def _value(key: str, value: object, default: object) -> object:
    """Check a config.toml value against the default's type, so a typo fails at load with
    the key's name instead of somewhere deep in the host."""
    if key in _DURATIONS:
        try:
            return seconds(value)  # type: ignore[arg-type]
        except (ValueError, IndexError, AttributeError, TypeError):
            raise ValueError(
                f'config.toml: {key} must be a duration like 10, "10s", "10m" or "2h"'
            ) from None
    if key == "engines":
        if not isinstance(value, dict) or not all(isinstance(v, str) for v in value.values()):
            raise ValueError('config.toml: engines must be a table of name = "/path/to/binary"')
        return value
    if key == "isolation":
        if value not in ("none", "seatbelt", "bwrap"):
            raise ValueError('config.toml: isolation must be "none", "seatbelt" or "bwrap"')
        return value
    if key == "isolation_scope":
        if value not in ("shared", "bot"):
            raise ValueError('config.toml: isolation_scope must be "shared" or "bot"')
        return value
    if key == "isolation_read":
        if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
            raise ValueError('config.toml: isolation_read must be a list of folders like "~/.nvm"')
        return value
    if key == "plugins":
        if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
            raise ValueError(
                'config.toml: plugins must be a list of module names like "plugins.mail"'
            )
        return value
    if isinstance(default, float) and isinstance(value, int) and not isinstance(value, bool):
        return float(value)  # daily_budget = 20
    if isinstance(value, bool) != isinstance(default, bool) or not isinstance(value, type(default)):
        raise ValueError(f"config.toml: {key} must be {type(default).__name__}, got {value!r}")
    return value
