"""Plugins: parts only some setups want. config.toml's `plugins` names them by module path.

Each module exposes `PLUGIN = Plugin(...)`. Every field but the name is optional:

- `add_commands(sub, cmd)` adds `bots` subcommands;
- `skill_fragment` is a markdown file added to the skill;
- `engines` adds engine adapters by name;
- `host_secrets` names env keys the host holds for those engines, which no engine CLI inherits.

A missing or broken plugin stops `bots` and the host with a config.toml error naming it."""

from __future__ import annotations

import importlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from host.config import Config

if TYPE_CHECKING:  # host.engines imports this module
    from host.engines.base import Engine


@dataclass(frozen=True)
class EngineSpec:
    make: Callable[[Config, str | None], Engine]  # (cfg, the bot's model) -> a fresh engine
    # The CLI it runs: what cfg.engine_binary looks for on the PATH, and whose session files
    # `bots inspect` points at. "": runs nothing of its own (the fake engine).
    command: str = ""


@dataclass(frozen=True)
class Plugin:
    name: str
    add_commands: Callable[[Any, Any], None] | None = None
    skill_fragment: Path | None = None
    engines: Mapping[str, EngineSpec] = field(default_factory=dict)
    host_secrets: frozenset[str] = frozenset()


# Every loaded plugin's host_secrets, for this process. Only ever grows: a key named once is
# kept from the CLIs even if a later config drops the plugin.
_host_secrets: set[str] = set()


def load(cfg: Config) -> tuple[Plugin, ...]:
    """The configured plugins, in config.toml's order. Raises ValueError naming a bad one."""
    return _load(tuple(cfg.plugins))


@cache
def _load(modules: tuple[str, ...]) -> tuple[Plugin, ...]:
    loaded = []
    for module in modules:
        try:
            mod = importlib.import_module(module)
        except ModuleNotFoundError as e:
            if e.name and (module == e.name or module.startswith(e.name + ".")):
                raise ValueError(f"config.toml: plugin {module!r} not found") from None
            raise ValueError(f"config.toml: plugin {module!r} failed to load: {e}") from None
        except Exception as e:  # noqa: BLE001 (any import-time error: name the plugin)
            raise ValueError(f"config.toml: plugin {module!r} failed to load: {e}") from None
        plugin = getattr(mod, "PLUGIN", None)
        if not isinstance(plugin, Plugin):
            # ValueError like every config.toml error, which `bots` and the host report
            raise ValueError(f"config.toml: plugin {module!r} has no PLUGIN = Plugin(...)")  # noqa: TRY004
        loaded.append(plugin)
        _host_secrets.update(plugin.host_secrets)
    return tuple(loaded)


def host_secrets() -> frozenset[str]:
    return frozenset(_host_secrets)


def skill_fragments(cfg: Config) -> list[Path]:
    return [p.skill_fragment for p in load(cfg) if p.skill_fragment]
