from pathlib import Path

import pytest

from host import sandbox
from host.config import HOME, REPO, Config, load


def test_load_reads_values_and_durations(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text('max_concurrent = 9\nidle_timeout = "10m"\n')
    cfg = load(tmp_path)
    assert (cfg.max_concurrent, cfg.idle_timeout, cfg.root) == (9, 600.0, tmp_path.resolve())


def test_background_timeout_is_a_duration(tmp_path: Path) -> None:
    assert load(tmp_path).background_timeout == 7800
    (tmp_path / "config.toml").write_text('background_timeout = "3h"\n')
    assert load(tmp_path).background_timeout == 10800


def test_load_reads_notify(tmp_path: Path) -> None:
    assert load(tmp_path).notify is True
    (tmp_path / "config.toml").write_text("notify = false\n")
    assert load(tmp_path).notify is False


def test_isolated_bots_share_their_sandbox_unless_config_says_bot(tmp_path: Path) -> None:
    assert load(tmp_path).isolation_scope == "shared"
    (tmp_path / "config.toml").write_text('isolation = "seatbelt"\nisolation_scope = "bot"\n')
    assert load(tmp_path).isolation_scope == "bot"


@pytest.mark.parametrize(
    ("toml", "error"),
    [
        ("tick = ", "config.toml: Invalid value"),
        ("tcik = 1", "config.toml: unknown key 'tcik'"),
        ('max_concurrent = "2"', "config.toml: max_concurrent must be int"),
        ("max_concurrent = true", "config.toml: max_concurrent must be int"),
        ("notify = 0", "config.toml: notify must be bool"),
        ('tick = "soon"', "config.toml: tick must be a duration"),
        ('tick = ""', "config.toml: tick must be a duration"),
        ('engines = "claude"', "config.toml: engines must be a table"),
        ('root = "/tmp"', "config.toml: unknown key 'root'"),
        ('isolation_scope = "bots"', 'config.toml: isolation_scope must be "shared" or "bot"'),
    ],
)
def test_load_rejects_bad_config_with_the_key(tmp_path: Path, toml: str, error: str) -> None:
    (tmp_path / "config.toml").write_text(toml + "\n")
    with pytest.raises(ValueError, match="^" + error.replace("(", r"\(")):
        load(tmp_path)


def test_data_lives_outside_the_repo_unless_bots_home_says_otherwise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert not HOME.resolve().is_relative_to(REPO)
    monkeypatch.delenv("BOTS_HOME", raising=False)
    monkeypatch.setattr("host.config.HOME", tmp_path / "home")
    assert load().root == (tmp_path / "home").resolve()
    monkeypatch.setenv("BOTS_HOME", str(tmp_path))
    assert load().root == tmp_path.resolve()


@pytest.fixture
def path_bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A PATH holding only a claude CLI."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "claude").write_text("#!/bin/sh\n")
    (bin_dir / "claude").chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    return bin_dir


def test_engines_are_found_on_the_path_unless_config_names_them(
    tmp_path: Path, path_bin: Path
) -> None:
    cfg = load(tmp_path)
    assert cfg.engine_binary("claude") == str(path_bin / "claude")
    assert cfg.engine_binary("other", "claude") == str(path_bin / "claude")  # runs claude too
    assert cfg.engine_binary("codex") == "codex"  # not installed: its run fails "not found"
    (tmp_path / "config.toml").write_text('[engines]\ncodex = "~/tools/codex"\n')
    cfg = load(tmp_path)
    assert cfg.engine_binary("codex") == str(Path.home() / "tools" / "codex")
    assert cfg.engine_binary("claude") == str(path_bin / "claude")


@pytest.mark.parametrize("isolation", ["seatbelt", "bwrap"])
@pytest.mark.parametrize("root", [REPO, REPO / "data"])
def test_isolation_refuses_a_data_folder_in_the_checkout(isolation: str, root: Path) -> None:
    with pytest.raises(sandbox.Unavailable, match="inside the source checkout"):
        sandbox.check(Config(root=root, isolation=isolation))
