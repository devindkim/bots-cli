"""Engine keys from <data>/secrets/engines.env, for a service that can't see shell exports.

No shell runs the file, only credential names are read, and it must be closed to group
and others. Only key names are ever logged."""

from __future__ import annotations

import logging
import os
import re
import stat
import subprocess
from collections.abc import MutableMapping
from pathlib import Path

from host import plugins
from host.config import Config
from host.engines.base import secrets

log = logging.getLogger(__name__)

FILE = "engines.env"

# The keys each kernel engine's CLI logs in with headless, and how to ask it whether it is
# logged in on its own (exit 0: yes). The CLIs: `claude auth status`, `codex login status`.
LOGINS = {
    "claude": (("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN"), "auth"),
    "codex": (("CODEX_API_KEY", "OPENAI_API_KEY"), "login"),
}

_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class KeyFileError(RuntimeError):
    """The file can't be used. The message names the file and the fix, never a value."""


def path(cfg: Config) -> Path:
    return cfg.secrets_dir / FILE


def read(file: Path) -> dict[str, str]:
    """The credentials in `file` by name; {} when there is no file."""
    try:
        st = file.stat()
    except FileNotFoundError:
        return {}
    if st.st_uid != os.getuid():
        raise KeyFileError(f"{file} is not owned by you (chown it to your user)")
    if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise KeyFileError(f"{file} is open to other users (chmod 600 {file})")
    names = secrets()
    keys: dict[str, str] = {}
    skipped = []
    for n, line in enumerate(file.read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, value = line.removeprefix("export ").partition("=")
        name, value = name.strip(), value.strip()
        if not sep or not _NAME.fullmatch(name):
            raise KeyFileError(f"{file} line {n}: not NAME=value")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if name not in names:
            skipped.append(name)
        elif value:
            keys[name] = value
    if skipped:
        log.warning("%s: skipped %s (not engine keys)", file, ", ".join(skipped))
    return keys


def load(cfg: Config, environ: MutableMapping[str, str] = os.environ) -> list[str]:
    """Put the file's keys into `environ` where it has none; the names it set."""
    plugins.load(cfg)  # so the file may hold a plugin engine's key
    file = path(cfg)
    keys = read(file)
    added = [k for k in keys if not environ.get(k)]
    for k in added:
        environ[k] = keys[k]
    if kept := [k for k in keys if k not in added]:
        log.info("engine keys: %s kept from the host's env over %s", ", ".join(kept), file)
    if added:
        log.info("engine keys from %s: %s", file, ", ".join(added))
    return added


def logged_in(binary: str, engine: str) -> bool:
    """Whether the engine's CLI is logged in by itself (its keychain or config), with every
    key var taken out of its env so a shell export doesn't count."""
    _, command = LOGINS[engine]
    env = {k: v for k, v in os.environ.items() if k not in secrets()}
    try:
        done = subprocess.run(
            [binary, command, "status"], env=env, capture_output=True, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0


def missing(cfg: Config, engines: list[str]) -> list[str]:
    """One warning line per engine in `engines` the service would run without a login:
    no key in the file and no login of the CLI's own. A key in this shell only is named, since
    the service won't see it. An isolated engine can't reach the CLI's own login (the
    keychain is closed to it), so there only a key counts. Raises KeyFileError as the host
    would at start."""
    plugins.load(cfg)
    keys = read(path(cfg))
    isolated = cfg.isolation != "none"
    lines = []
    for engine in engines:
        if engine not in LOGINS:
            continue
        names, _ = LOGINS[engine]
        if any(n in keys for n in names):
            continue
        if not isolated and logged_in(cfg.engine_binary(engine), engine):
            continue
        shell = [n for n in names if os.environ.get(n)]
        if shell:
            hint = f"{shell[0]} is set in this shell only, and the service won't see it"
        elif isolated:
            hint = (
                f"no {names[0]} found, and isolation = {cfg.isolation!r} can't use the CLI's login"
            )
        else:
            hint = f"no {names[0]} found and the CLI isn't logged in"
        lines.append(
            f"warning: {engine}: {hint}. Its turns will fail until you put {names[0]}=... in "
            f"{path(cfg)} (chmod 600){'' if isolated else ' or log the CLI in'}."
        )
    return lines
