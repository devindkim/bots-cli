"""`bots init`: a fresh folder that a host can serve, and never over an existing install."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from cli import init as first_run
from cli.main import main
from host.config import load
from host.store import open_store


def bots(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def test_init_makes_a_folder_a_host_can_serve(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "new"
    code, out, err = bots(capsys, "init", str(root), "--engine", "claude")
    assert (code, err) == (0, "")
    cfg = load(root)
    assert cfg.isolation == "none"
    bots_made = {b["id"]: b for b in open_store(cfg).list_bots()}
    assert set(bots_made) == {"builder", "chief", "researcher", "engineer", "reviewer"}
    assert {b["engine"] for b in bots_made.values()} == {"claude"}
    for bot_id in bots_made:
        soul = first_run.SOULS / f"{bot_id}.md"
        assert (cfg.bot_dir(bot_id) / "SOUL.md").read_text() == soul.read_text()
    assert out.index("Already logged in to claude") < out.index("export ANTHROPIC_API_KEY=")
    assert "token" not in out.lower()
    assert out.index(f"BOTS_HOME={root} bots serve") < out.index(
        f"BOTS_HOME={root} bots chat chief"
    )


def test_init_codex_names_its_key(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = bots(capsys, "init", str(tmp_path / "x"), "--engine", "codex")
    assert code == 0
    assert "export CODEX_API_KEY=" in out
    assert (
        f"put CODEX_API_KEY=... in {(tmp_path / 'x').resolve() / 'secrets' / 'engines.env'}" in out
    )
    assert {b["engine"] for b in open_store(load(tmp_path / "x")).list_bots()} == {"codex"}


def test_init_defaults_to_bots_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "home"
    monkeypatch.setenv("BOTS_HOME", str(root))
    assert bots(capsys, "init", "--engine", "fake")[0] == 0
    assert (root / "config.toml").is_file()


@pytest.mark.parametrize("mark", ["config.toml", "bots.sqlite3", "bots"])
def test_init_never_touches_an_existing_install(
    tmp_path: Path, mark: str, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "live"
    root.mkdir()
    if mark == "bots":
        (root / mark).mkdir()
    else:
        (root / mark).write_text("keep")
    code, _, err = bots(capsys, "init", str(root), "--engine", "fake")
    assert code == 1
    assert "already holds a host's data" in err and mark in err
    assert sorted(p.name for p in root.iterdir()) == [mark]


def test_init_into_an_empty_existing_folder(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert bots(capsys, "init", str(tmp_path), "--engine", "fake")[0] == 0
    assert (tmp_path / "bots" / "chief" / "SOUL.md").is_file()


def test_init_writes_nothing_when_a_soul_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(first_run, "SOULS", tmp_path / "gone")
    root = tmp_path / "new"
    code, _, err = bots(capsys, "init", str(root), "--engine", "fake")
    assert code == 1 and "missing" in err
    assert not root.exists()


def test_init_says_when_the_engine_cli_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    code, out, _ = bots(capsys, "init", str(tmp_path / "a"), "--engine", "claude")
    assert code == 0 and "claude is not on your PATH" in out
    code, out, _ = bots(capsys, "init", str(tmp_path / "b"), "--engine", "fake")
    assert code == 0 and "PATH" not in out


def test_init_printed_commands_survive_spaces_and_shell_syntax(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Each printed command, run by a shell with a stand-in `bots`, gets the whole folder."""
    root = tmp_path / "my bots $HOME `x`;'q'"
    code, out, err = bots(capsys, "init", str(root), "--engine", "fake")
    assert (code, err) == (0, "")
    serve = next(line.strip() for line in out.splitlines() if line.endswith("bots serve"))
    chat = next(line.strip() for line in out.splitlines() if line.endswith("bots chat chief"))
    probe = tmp_path / "bin"
    probe.mkdir()
    executable = probe / "bots"
    executable.write_text('#!/bin/sh\nprintf "%s" "$BOTS_HOME"\n')
    executable.chmod(0o700)
    for command in (serve, chat):
        result = subprocess.run(
            command,
            shell=True,
            check=False,
            capture_output=True,
            text=True,
            env={**os.environ, "PATH": str(probe)},
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == str(root.resolve())


def test_init_says_the_bots_are_an_example(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = bots(capsys, "init", str(tmp_path / "new"), "--engine", "fake")
    assert code == 0 and "example" in out
    assert "bots chat builder" in out
    assert "bots chat chief" in out and "web UI" in out


def test_the_example_souls_name_only_the_chief() -> None:
    for soul in first_run.SOULS.glob("*.md"):
        assert set(re.findall(r"`user:([^`]+)`", soul.read_text())) <= {"chief"}
