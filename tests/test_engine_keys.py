"""Engine keys from <data>/secrets/engines.env."""

import logging
import os
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from cli import service
from cli.main import main
from host import engine_keys, plugins, serve
from host.config import Config
from host.engines.base import REDACTED, McpServer, redactor
from host.engines.claude import ClaudeEngine
from host.engines.codex import CodexEngine
from host.store import open_store

KEY = "sk-ant-from-the-file-0123456789"


def key_file(cfg: Config, text: str, mode: int = 0o600) -> Path:
    path = engine_keys.path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)
    return path


def test_no_file_changes_nothing(cfg: Config) -> None:
    env = {"PATH": "/usr/bin"}
    assert engine_keys.load(cfg, env) == []
    assert env == {"PATH": "/usr/bin"}


def test_the_files_keys_fill_the_env_and_the_hosts_own_win(
    cfg: Config, caplog: pytest.LogCaptureFixture
) -> None:
    key_file(
        cfg,
        "# keys for the service\n\n"
        f"ANTHROPIC_API_KEY={KEY}\n"
        "export CODEX_API_KEY='codex-from-the-file-9876'\n"
        'OPENAI_API_KEY = "openai-from-the-file-5555"\n',
    )
    env = {"CODEX_API_KEY": "codex-from-the-host-env", "OPENAI_API_KEY": ""}
    with caplog.at_level(logging.INFO):
        added = engine_keys.load(cfg, env)
    assert added == ["ANTHROPIC_API_KEY", "OPENAI_API_KEY"]
    assert env == {
        "ANTHROPIC_API_KEY": KEY,
        "CODEX_API_KEY": "codex-from-the-host-env",  # already set: the host's env wins
        "OPENAI_API_KEY": "openai-from-the-file-5555",  # set but empty: the file fills it
    }
    assert "ANTHROPIC_API_KEY" in caplog.text and "CODEX_API_KEY kept" in caplog.text
    assert "from-the-file" not in caplog.text and "from-the-host" not in caplog.text


def test_values_are_taken_literally_with_no_shell(cfg: Config, tmp_path: Path) -> None:
    ran = tmp_path / "ran"
    value = f"a$HOME`touch {ran}`;$(touch {ran})=b"
    key_file(cfg, f"ANTHROPIC_API_KEY={value}\n")
    env: dict[str, str] = {}
    engine_keys.load(cfg, env)
    assert env == {"ANTHROPIC_API_KEY": value}
    assert not ran.exists()


def test_only_engine_keys_are_read(cfg: Config, caplog: pytest.LogCaptureFixture) -> None:
    key_file(cfg, f"PATH=/evil\nDYLD_INSERT_LIBRARIES=/evil.dylib\nANTHROPIC_API_KEY={KEY}\n")
    env = {"PATH": ""}
    with caplog.at_level(logging.WARNING):
        engine_keys.load(cfg, env)
    assert env == {"PATH": "", "ANTHROPIC_API_KEY": KEY}
    assert "skipped PATH, DYLD_INSERT_LIBRARIES" in caplog.text


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o660, 0o644])
def test_a_file_open_to_others_is_refused_with_the_chmod(cfg: Config, mode: int) -> None:
    path = key_file(cfg, f"ANTHROPIC_API_KEY={KEY}\n", mode)
    env: dict[str, str] = {}
    with pytest.raises(engine_keys.KeyFileError, match=f"chmod 600 {path}") as e:
        engine_keys.load(cfg, env)
    assert env == {} and KEY not in str(e.value)


def test_a_file_owned_by_someone_else_is_refused(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_file(cfg, f"ANTHROPIC_API_KEY={KEY}\n")
    uid = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: uid + 1)
    with pytest.raises(engine_keys.KeyFileError, match="not owned by you"):
        engine_keys.load(cfg, {})


def test_a_line_that_isnt_name_equals_value_is_refused_without_its_text(cfg: Config) -> None:
    key_file(cfg, f"ANTHROPIC_API_KEY={KEY}\n{KEY}\n")
    with pytest.raises(engine_keys.KeyFileError, match="line 2: not NAME=value") as e:
        engine_keys.load(cfg, {})
    assert KEY not in str(e.value)


def test_the_host_refuses_to_start_on_a_bad_file(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_file(cfg, f"ANTHROPIC_API_KEY={KEY}\n", 0o644)
    monkeypatch.setattr(serve, "open_store", lambda cfg: pytest.fail("the host started"))
    with pytest.raises(engine_keys.KeyFileError, match="chmod 600"):
        serve.serve(cfg)


@pytest.fixture
def keyed(cfg: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Config]:
    """cfg with a stand-in plugin loaded that holds STANDIN_API_KEY for its engine."""
    folder = tmp_path / "plugdir"
    folder.mkdir()
    (folder / "plug_keyed.py").write_text(
        "from host.plugins import Plugin\n"
        'PLUGIN = Plugin(name="keyed", host_secrets=frozenset({"STANDIN_API_KEY"}))\n'
    )
    monkeypatch.syspath_prepend(str(folder))
    registered = set(plugins._host_secrets)
    plugins._load.cache_clear()
    yield replace(cfg, plugins=["plug_keyed"])
    plugins._load.cache_clear()
    plugins._host_secrets.intersection_update(registered)
    sys.modules.pop("plug_keyed", None)


def test_a_loaded_key_reaches_the_engines_and_a_plugins_host_secret_doesnt(
    keyed: Config, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    key_file(keyed, f"ANTHROPIC_API_KEY={KEY}\nSTANDIN_API_KEY=standin-key-1234\n")
    for name in ("ANTHROPIC_API_KEY", "STANDIN_API_KEY"):
        monkeypatch.setenv(name, "")  # so monkeypatch restores the env after load() sets it
    assert sorted(engine_keys.load(keyed)) == ["ANTHROPIC_API_KEY", "STANDIN_API_KEY"]
    assert os.environ["STANDIN_API_KEY"] == "standin-key-1234"  # the plugin's engine reads it
    mcp = McpServer("python", [], {})
    _, env = ClaudeEngine("claude", None).command("hi", tmp_path, mcp)
    assert env["ANTHROPIC_API_KEY"] == KEY
    assert "STANDIN_API_KEY" not in env
    _, env = CodexEngine("codex", None).command("hi", tmp_path, mcp)
    assert env["ANTHROPIC_API_KEY"] == KEY and "STANDIN_API_KEY" not in env
    # and a CLI that prints it has it cut from its output
    assert (
        redactor({"STANDIN_API_KEY": "standin-key-1234"})("k=standin-key-1234") == f"k={REDACTED}"
    )


def test_without_the_plugin_its_key_name_is_skipped(
    cfg: Config, caplog: pytest.LogCaptureFixture
) -> None:
    key_file(cfg, "STANDIN_API_KEY=standin-key-1234\n")
    env: dict[str, str] = {}
    with caplog.at_level(logging.WARNING):
        assert engine_keys.load(cfg, env) == []
    assert env == {}
    assert "skipped STANDIN_API_KEY" in caplog.text


# -- bots install ----------------------------------------------------------------


def status_cli(cfg: Config, name: str, logged_in: bool) -> Path:
    """A stand-in engine CLI whose `<x> status` says logged in only if asked with no key
    in its env, and records the env names it got."""
    path = cfg.root / f"{name}-cli"
    code = 0 if logged_in else 1
    path.write_text(f'#!/bin/sh\nenv | cut -d= -f1 > "{path}.env"\nexit {code}\n')
    path.chmod(0o755)
    return path


@pytest.fixture
def installing(cfg: Config, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[list[str]]:
    """`bots install` against this data folder, its launchctl calls recorded, nothing loaded."""
    calls: list[list[str]] = []

    def run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        if cmd[0] == "launchctl":
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return real_run(cmd, **kw)  # type: ignore[call-overload,no-any-return]

    real_run = subprocess.run
    monkeypatch.setenv("BOTS_HOME", str(cfg.root))
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(service, "service_path", lambda cfg: tmp_path / "agent.plist")
    monkeypatch.setattr(subprocess, "run", run)
    for name in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    for name in ("CODEX_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    return calls


def engines_config(cfg: Config, claude: Path, codex: Path, extra: str = "") -> None:
    (cfg.root / "config.toml").write_text(
        f'notify = false\n{extra}[engines]\nclaude = "{claude}"\ncodex = "{codex}"\n'
    )


def test_install_warns_for_an_engine_with_no_key_and_no_login(
    cfg: Config,
    installing: list[list[str]],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claude = status_cli(cfg, "claude", logged_in=False)
    engines_config(cfg, claude, status_cli(cfg, "codex", logged_in=True))
    store = open_store(cfg)
    store.create_bot("ada", "Ada", engine="claude")
    store.create_bot("rae", "Rae", engine="codex")
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)  # in this shell only
    assert main(["install"]) == 0
    _, err = capsys.readouterr()
    assert installing  # it still installs
    lines = err.splitlines()
    assert len(lines) == 1 and lines[0].startswith("warning: claude:")
    assert "ANTHROPIC_API_KEY is set in this shell only" in lines[0]
    assert str(engine_keys.path(cfg)) in lines[0] and "log the CLI in" in lines[0]
    assert KEY not in err
    # The login check ran with the shell's key taken out, so the export didn't count.
    assert "ANTHROPIC_API_KEY" not in Path(f"{claude}.env").read_text().split()


def test_install_is_quiet_when_the_file_has_the_key(
    cfg: Config, installing: list[list[str]], capsys: pytest.CaptureFixture[str]
) -> None:
    engines_config(cfg, status_cli(cfg, "claude", False), status_cli(cfg, "codex", False))
    open_store(cfg).create_bot("ada", "Ada", engine="claude")
    key_file(cfg, f"CLAUDE_CODE_OAUTH_TOKEN={KEY}\n")
    assert main(["install"]) == 0
    assert capsys.readouterr().err == ""  # codex has no bot, so it isn't checked


def test_install_in_isolated_mode_wants_a_key_even_when_the_cli_is_logged_in(
    cfg: Config, installing: list[list[str]], capsys: pytest.CaptureFixture[str]
) -> None:
    extra = 'isolation = "seatbelt"\n'
    engines_config(cfg, status_cli(cfg, "claude", True), status_cli(cfg, "codex", True), extra)
    open_store(cfg).create_bot("ada", "Ada", engine="claude")
    assert main(["install"]) == 0
    err = capsys.readouterr().err
    assert "can't use the CLI's login" in err and "log the CLI in" not in err


def test_install_refuses_a_key_file_the_host_would_refuse(
    cfg: Config, installing: list[list[str]], capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    open_store(cfg).create_bot("ada", "Ada", engine="claude")
    key_file(cfg, f"ANTHROPIC_API_KEY={KEY}\n", 0o644)
    assert main(["install"]) == 1
    assert "chmod 600" in capsys.readouterr().err
    assert installing == [] and not (tmp_path / "agent.plist").exists()


def test_install_before_any_bot_checks_nothing(
    cfg: Config, installing: list[list[str]], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["install"]) == 0
    assert capsys.readouterr().err == ""
    assert not cfg.db_path.exists()
