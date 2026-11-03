from pathlib import Path

import pytest

import botsmcp
from host.config import Config
from host.store import Store, open_store


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    """A data directory of its own, with fast timings for tests. config.toml is for the
    botsmcp subprocesses: a fake bot's send_to_user must not pop a real notification."""
    (tmp_path / "config.toml").write_text("notify = false\n")
    return Config(root=tmp_path, idle_timeout=2, tick=0.05, failure_backoff=0.2, notify=False)


@pytest.fixture
def store(cfg: Config) -> Store:
    return open_store(cfg)


@pytest.fixture(autouse=True)
def _no_real_bots_home(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In-process botsmcp reads config from $BOTS_HOME too: never the real ~/.bots, whose
    notify = true pops a real macOS notification for every send_to_user a test makes."""
    home = tmp_path_factory.mktemp("bots-home")
    (home / "config.toml").write_text("notify = false\n")
    monkeypatch.setenv("BOTS_HOME", str(home))
    botsmcp._load_cfg.cache_clear()
