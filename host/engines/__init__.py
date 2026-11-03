"""One adapter per CLI, built fresh for each turn. Plugins add more (host/plugins.py)."""

from __future__ import annotations

from pathlib import Path
from typing import NoReturn

from host import plugins
from host.config import Config
from host.engines.base import Engine, Event, Isolation, McpServer
from host.engines.claude import ClaudeEngine
from host.engines.codex import CodexEngine
from host.engines.fake import FakeEngine
from host.plugins import EngineSpec

KERNEL = {
    "claude": EngineSpec(
        lambda cfg, model: ClaudeEngine(cfg.engine_binary("claude"), model),
        command="claude",
    ),
    "codex": EngineSpec(
        lambda cfg, model: CodexEngine(cfg.engine_binary("codex"), model),
        command="codex",
    ),
}
FAKE = EngineSpec(lambda cfg, model: FakeEngine())  # plays a script


def engines(cfg: Config) -> dict[str, EngineSpec]:
    """Every loaded engine by name: the kernel's, the plugins' in config order, fake last."""
    found = dict(KERNEL)
    for plugin in plugins.load(cfg):
        for name, spec in plugin.engines.items():
            if name in found or name == "fake":
                raise ValueError(f"config.toml: plugin {plugin.name!r} redefines engine {name!r}")
            found[name] = spec
    return found | {"fake": FAKE}


def engine_names(cfg: Config) -> tuple[str, ...]:
    return tuple(engines(cfg))


def get_engine(name: str, cfg: Config, model: str | None = None) -> Engine:
    """`model` is the bot's own; None leaves the CLI on its default. A bot whose engine is
    not loaded (its plugin left config.toml) gets one whose every turn fails, saying so."""
    spec = engines(cfg).get(name)
    return spec.make(cfg, model) if spec else NotLoaded(name)


class NotLoaded:
    """Fails before anything runs, so the turn ends failed through the usual path: the
    messages stay unread and the retry backs off."""

    def __init__(self, name: str) -> None:
        self.name = name

    def _fail(self) -> NoReturn:
        raise RuntimeError(f"engine {self.name!r} is not loaded (see plugins in config.toml)")

    def isolation(self, home: Path, tmp: Path) -> Isolation:
        self._fail()

    def command(self, prompt: str, cwd: Path, mcp: McpServer) -> tuple[list[str], dict[str, str]]:
        self._fail()

    def parse(self, line: str) -> list[Event]:
        return []
