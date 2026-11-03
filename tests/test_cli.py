import plistlib
import subprocess
import sys
from pathlib import Path

import pytest

from cli import service
from cli.main import main
from host.config import Config
from host.store import open_store


@pytest.fixture(autouse=True)
def bots_home(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOTS_HOME", str(cfg.root))


def test_new_group_routine(cfg: Config, tmp_path: Path) -> None:
    soul = tmp_path / "soul.md"
    soul.write_text("# Chief\n")
    assert main(["new", "chief", "--name", "Chief", "--title", "Chief", "--soul", str(soul)]) == 0
    assert main(["new", "researcher", "--name", "Rae", "--engine", "codex"]) == 0
    assert main(["group", "new", "team", "--members", "chief,researcher"]) == 0
    assert main(["routine", "add", "chief", "0 8 * * *", "brief me", "--name", "Morning"]) == 0
    assert main(["new", "chief", "--name", "Dup"]) == 1

    store = open_store(cfg)
    assert store.get_bot("researcher")["engine"] == "codex"  # type: ignore[index]
    assert (cfg.bot_dir("chief") / "SOUL.md").read_text() == "# Chief\n"
    assert (cfg.bot_dir("researcher") / "MEMORY.md").exists()
    assert store.get_conversation("group:team")["members"] == ["chief", "researcher"]  # type: ignore[index]
    assert [(r["name"], r["prompt"]) for r in store.list_routines("chief")] == [
        ("Morning", "brief me")
    ]


def test_pause_keeps_a_bot_from_waking_until_resume(
    cfg: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    store = open_store(cfg)
    store.create_bot("ada", "Ada")
    store.user_send("user:ada", "hello")
    assert store.due_bots() == ["ada"]
    assert main(["pause", "ada"]) == 0
    assert store.due_bots() == []
    assert main(["ls"]) == 0 and "paused" in capsys.readouterr().out
    assert main(["resume", "ada"]) == 0
    assert store.due_bots() == ["ada"]
    assert main(["pause", "nobody"]) == 1
    assert "no bot 'nobody'" in capsys.readouterr().err


def test_serve_warns_when_an_engine_is_not_logged_in(
    cfg: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    open_store(cfg).create_bot("ada", "Ada", engine="claude")
    monkeypatch.setattr("host.serve.serve", lambda _cfg: None)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    assert main(["serve"]) == 0
    assert "warning: claude:" in capsys.readouterr().err


def test_new_without_an_id_gets_a_neutral_one(
    cfg: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["new", "--name", "Chief"]) == 0
    assert main(["new", "--name", "Rae"]) == 0
    assert "created bot-2:" in capsys.readouterr().out
    assert open_store(cfg).get_bot("bot-2")["name"] == "Rae"  # type: ignore[index]
    assert (cfg.bot_dir("bot-1") / "SOUL.md").exists()


def test_first_command_on_a_fresh_machine_makes_the_data_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "not-yet" / ".bots"  # ~/.bots before anything has run
    monkeypatch.setenv("BOTS_HOME", str(home))
    assert main(["ls"]) == 0
    assert main(["new", "ada", "--name", "Ada"]) == 0
    assert (home / "bots.sqlite3").exists() and (home / "bots" / "ada" / "MEMORY.md").exists()


@pytest.fixture
def on_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")


@pytest.mark.usefixtures("on_macos")
def test_install_print(
    cfg: Config, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = cfg.root / "engine-bin"  # where the engine CLIs were installed, not on launchd's PATH
    bin_dir.mkdir()
    for name in ("claude", "codex"):
        (bin_dir / name).write_text("#!/bin/sh\n")
        (bin_dir / name).chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    assert main(["install", "--print"]) == 0
    plist = plistlib.loads(capsys.readouterr().out.encode())
    assert plist["Label"] == "bots-cli.host"
    assert plist["ProgramArguments"][1:] == ["serve"]
    assert plist["ProgramArguments"][0] == str(Path(sys.executable).parent / "bots")
    assert plist["RunAtLoad"] and plist["KeepAlive"]
    assert plist["WorkingDirectory"] == str(cfg.root)
    assert plist["StandardOutPath"] == plist["StandardErrorPath"] == str(cfg.root / "logs/host.log")
    assert {"PATH", "HOME"} <= plist["EnvironmentVariables"].keys()
    assert str(bin_dir) in plist["EnvironmentVariables"]["PATH"].split(":")
    assert plist["SoftResourceLimits"] == {"NumberOfFiles": 4096}  # launchd gives 256


@pytest.mark.usefixtures("on_macos")
def test_an_existing_install_keeps_its_launchd_label_from_config(
    cfg: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    (cfg.root / "config.toml").write_text('launchd_label = "com.example.bots"\n')
    assert main(["install", "--print"]) == 0
    assert plistlib.loads(capsys.readouterr().out.encode())["Label"] == "com.example.bots"


def bots(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def test_bad_config_is_an_error_not_a_traceback(
    cfg: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    (cfg.root / "config.toml").write_text("port = \n")
    code, _, err = bots(capsys, "ls")
    assert code == 1
    assert err.startswith("bots: config.toml: ")


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_install_failure_is_an_error(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    platform: str,
) -> None:
    def run(cmd: list[str], check: bool = False, **kw: object) -> None:
        if check:
            raise subprocess.CalledProcessError(5, cmd)

    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(service, "service_path", lambda cfg: tmp_path / "agent")
    monkeypatch.setattr(subprocess, "run", run)
    code, _, err = bots(capsys, "install")
    assert code == 1
    assert "failed with exit code 5" in err


def test_archived_bots_list_restore_and_no_reuse(
    cfg: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["new", "bo", "--name", "Bo"]) == 0
    assert main(["rm", "bo"]) == 0
    capsys.readouterr()

    code, _, err = bots(capsys, "rm", "bo")
    assert (code, err) == (1, "bots: bo is already archived\n")
    code, _, err = bots(capsys, "new", "bo", "--name", "Bo")
    assert (code, err) == (1, "bots: bo is archived; `bots restore bo` brings it back\n")
    assert bots(capsys, "ls")[1] == ""
    assert bots(capsys, "ls", "--all")[1].split() == ["bo", "Bo", "claude", "archived"]

    assert main(["restore", "bo"]) == 0
    assert open_store(cfg).list_bots()[0]["id"] == "bo"
    assert bots(capsys, "restore", "bo")[0] == 1


def test_store_validation_surfaces_as_errors(
    cfg: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    for bot in ("ada", "bo"):
        assert main(["new", bot, "--name", bot.title()]) == 0
    assert main(["rm", "bo"]) == 0
    capsys.readouterr()

    assert bots(capsys, "routine", "add", "bo", "* * * * *", "hi") == (1, "", "bots: no bot 'bo'\n")
    code, _, err = bots(capsys, "group", "new", "g1", "--members", ",")
    assert (code, err) == (1, "bots: a group needs at least two bots\n")
    assert bots(capsys, "group", "new", "g1", "--members", "ada")[0] == 1
    assert open_store(cfg).list_groups() == []


def test_blank_names_are_rejected(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["new", "zed", "--name", "  "])
    assert "must not be blank" in capsys.readouterr().err


def test_ls_columns_line_up(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["new", "ada", "--name", "Ada Lovelace the Great", "--title", "Math"]) == 0
    assert main(["new", "zed", "--name", "Z"]) == 0
    capsys.readouterr()
    lines = bots(capsys, "ls")[1].splitlines()
    assert [line.index("claude") for line in lines] == [len("ada  Ada Lovelace the Great  ")] * 2


def test_routine_default_name_is_first_line(cfg: Config) -> None:
    assert main(["new", "ada", "--name", "Ada"]) == 0
    assert main(["routine", "add", "ada", "0 8 * * *", "line one\nline two"]) == 0
    assert main(["routine", "add", "ada", "0 9 * * *", "x" * 60]) == 0
    names = [r["name"] for r in open_store(cfg).list_routines("ada")]
    assert names == ["line one", "x" * 39 + "…"]


def test_routine_ls_pause_resume_rm(cfg: Config, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["new", "ada", "--name", "Ada"]) == 0
    assert main(["routine", "add", "ada", "0 8 * * *", "brief me", "--name", "Morning"]) == 0
    capsys.readouterr()
    assert bots(capsys, "routine", "ls")[1].split() == [
        "1",
        "ada",
        "0",
        "8",
        "*",
        "*",
        "*",
        "on",
        "Morning",
    ]

    assert main(["routine", "pause", "1"]) == 0
    assert open_store(cfg).list_routines()[0]["paused"] == 1
    assert main(["routine", "resume", "1"]) == 0
    assert open_store(cfg).list_routines()[0]["paused"] == 0

    assert main(["rm", "ada"]) == 0  # archiving pauses its routines; resuming would be a dud
    capsys.readouterr()
    code, _, err = bots(capsys, "routine", "resume", "1")
    assert (code, err) == (1, "bots: routine 1 belongs to archived ada; restore it first\n")

    assert main(["routine", "rm", "1"]) == 0
    assert open_store(cfg).list_routines() == []
    assert bots(capsys, "routine", "rm", "1")[2] == "bots: no routine 1\n"


def test_group_ls_rm(cfg: Config, capsys: pytest.CaptureFixture[str]) -> None:
    for bot in ("ada", "bo"):
        assert main(["new", bot, "--name", bot.title()]) == 0
    assert main(["group", "new", "team", "--name", "Team", "--members", "ada,bo"]) == 0
    capsys.readouterr()
    assert bots(capsys, "group", "ls")[1].split() == ["group:team", "Team", "ada,", "bo"]
    assert main(["group", "rm", "team"]) == 0
    assert open_store(cfg).list_groups() == []
    assert bots(capsys, "group", "rm", "team")[2] == "bots: no group 'team'\n"


def test_send(cfg: Config, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["new", "ada", "--name", "Ada"]) == 0
    assert main(["send", "ada", "research standing desks"]) == 0
    assert open_store(cfg).messages("user:ada")[-1].body == "research standing desks"
    assert main(["send", "nobody", "hi"]) == 1


def test_model_on_new_and_changed_later(cfg: Config, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["new", "ada", "--name", "Ada", "--model", "sonnet"]) == 0
    assert main(["new", "rae", "--name", "Rae", "--engine", "codex"]) == 0
    capsys.readouterr()
    store = open_store(cfg)
    assert store.get_bot("ada")["model"] == "sonnet"  # type: ignore[index]
    assert store.get_bot("rae")["model"] is None  # type: ignore[index]
    assert [line.split()[2] for line in bots(capsys, "ls")[1].splitlines()] == [
        "claude:sonnet",
        "codex",
    ]

    assert bots(capsys, "model", "rae", "gpt-6.1-sol") == (
        0,
        "rae: gpt-6.1-sol from its next turn\n",
        "",
    )
    assert bots(capsys, "model", "ada", "--default")[0] == 0
    assert store.get_bot("rae")["model"] == "gpt-6.1-sol"  # type: ignore[index]
    assert store.get_bot("ada")["model"] is None  # type: ignore[index]

    assert bots(capsys, "model", "nobody", "opus") == (1, "", "bots: no bot 'nobody'\n")
    for bad in (["ada"], ["ada", "opus", "--default"], ["ada", " "]):
        with pytest.raises(SystemExit):
            main(["model", *bad])
