"""host/plugins.py: config.toml's plugins, what each adds, and the kernel without them."""

import argparse
import sys
import textwrap
import time
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from host import botdir, plugins
from host.config import Config, load
from host.engines import engine_names, get_engine
from host.scheduler import Scheduler
from host.store import Store, user_conversation


@pytest.fixture
def plugin_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A folder on sys.path for plugin modules this test writes; forgotten afterwards."""
    folder = tmp_path / "plugdir"
    folder.mkdir()
    monkeypatch.syspath_prepend(str(folder))
    before = set(sys.modules)
    plugins._load.cache_clear()
    registered = set(plugins._host_secrets)
    yield folder
    plugins._load.cache_clear()
    plugins._host_secrets.intersection_update(registered)
    for name in set(sys.modules) - before:
        if name.startswith("plug_"):
            del sys.modules[name]


def write_plugin(folder: Path, name: str, body: str) -> None:
    (folder / f"{name}.py").write_text(textwrap.dedent(body))


GOOD = """
    from pathlib import Path
    from host.engines.fake import FakeEngine
    from host.plugins import EngineSpec, Plugin

    def add_commands(sub, cmd):
        cmd("hello", lambda cfg, args: print("hello from a plugin"), "say hello")

    (Path(__file__).parent / "{name}.md").write_text("## {title}\\n\\nFrom a plugin, bots/<id>/.\\n")
    PLUGIN = Plugin(
        name="{name}",
        add_commands=add_commands,
        skill_fragment=Path(__file__).parent / "{name}.md",
        engines={{"{name}-engine": EngineSpec(lambda cfg, model: FakeEngine(), lambda: [])}},
    )
"""


def good(folder: Path, name: str, title: str) -> str:
    write_plugin(folder, name, GOOD.format(name=name, title=title))
    return name


def test_no_plugins_by_default(tmp_path: Path) -> None:
    cfg = load(tmp_path)
    assert cfg.plugins == []
    assert plugins.load(cfg) == ()
    assert engine_names(cfg) == ("claude", "codex", "fake")


@pytest.mark.parametrize(
    ("value", "error"),
    [("'plug_a'", "must be a list"), ("[1]", "must be a list"), ('[""]', "must be a list")],
)
def test_the_plugins_key_is_a_list_of_names(tmp_path: Path, value: str, error: str) -> None:
    (tmp_path / "config.toml").write_text(f"plugins = {value}\n")
    with pytest.raises(ValueError, match=error):
        load(tmp_path)


def test_a_bad_plugin_fails_with_its_name(cfg: Config, plugin_dir: Path) -> None:
    write_plugin(plugin_dir, "plug_broken", "raise RuntimeError('half written')\n")
    write_plugin(plugin_dir, "plug_needs", "import plug_not_installed\n")
    cases = {
        "plug_missing": "plugin 'plug_missing' not found",
        "plugins.nope": "plugin 'plugins.nope' not found",
        "plug_broken": "plugin 'plug_broken' failed to load: half written",
        "plug_needs": "plugin 'plug_needs' failed to load: No module named 'plug_not_installed'",
        "host.kinds": r"plugin 'host.kinds' has no PLUGIN = Plugin\(...\)",
    }
    for module, error in cases.items():
        with pytest.raises(ValueError, match=f"^config.toml: {error}"):
            plugins.load(replace(cfg, plugins=[module]))


def test_a_plugin_may_not_take_an_engine_name(cfg: Config, plugin_dir: Path) -> None:
    write_plugin(
        plugin_dir,
        "plug_claude",
        """
        from host.engines.fake import FakeEngine
        from host.plugins import EngineSpec, Plugin
        PLUGIN = Plugin(name="x", engines={"claude": EngineSpec(lambda c, m: FakeEngine(), list)})
        """,
    )
    with pytest.raises(ValueError, match="plugin 'x' redefines engine 'claude'"):
        engine_names(replace(cfg, plugins=["plug_claude"]))


def test_plugins_add_engines_commands_and_skill_sections_in_order(
    cfg: Config,
    plugin_dir: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = replace(cfg, plugins=[good(plugin_dir, "plug_b", "Bee"), good(plugin_dir, "plug_a", "A")])
    assert engine_names(cfg) == ("claude", "codex", "plug_b-engine", "plug_a-engine", "fake")
    (cfg.root / "skills").mkdir()
    (cfg.root / "skills" / "mine.md").write_text("## Mine\n\nThe user's own.\n")
    skill = botdir.render_skill(cfg, "ada")
    assert (
        "## Bee\n\nFrom a plugin, bots/ada/.\n\n## A\n\nFrom a plugin, bots/ada/.\n\n"
        "## Mine\n\nThe user's own.\n\n## Ask first\n"
    ) in skill

    from cli.main import main, parser

    (cfg.root / "config.toml").write_text('notify = false\nplugins = ["plug_b"]\n')
    sub = next(
        a for a in parser(load(cfg.root))._actions if isinstance(a, argparse._SubParsersAction)
    )
    assert "hello" in sub.choices
    monkeypatch.setenv("BOTS_HOME", str(cfg.root))
    assert main(["hello"]) == 0
    assert capsys.readouterr().out == "hello from a plugin\n"


def test_the_cli_reports_a_bad_plugin_and_refuses_unloaded_engines(
    cfg: Config, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from cli.main import main

    monkeypatch.setenv("BOTS_HOME", str(cfg.root))
    with pytest.raises(SystemExit):  # argparse: not one of the choices
        main(["new", "ada", "--name", "Ada", "--engine", "someplugin"])
    assert "invalid choice: 'someplugin'" in capsys.readouterr().err
    (cfg.root / "config.toml").write_text('plugins = ["plugins.nope"]\n')
    assert main(["ls"]) == 1
    assert capsys.readouterr().err == "bots: config.toml: plugin 'plugins.nope' not found\n"


def test_a_bot_whose_engine_is_not_loaded_fails_its_turn_and_others_still_run(
    cfg: Config, store: Store
) -> None:
    assert get_engine("gone", cfg).name == "gone"
    store.create_bot("old", "Old", engine="gone")  # its plugin left config.toml
    store.create_bot("ada", "Ada", engine="fake")
    botdir.scaffold(cfg, "ada")
    (cfg.bot_dir("ada") / "fake_script.json").write_text("[]")
    store.user_send(user_conversation("old"), "hello")
    store.user_send(user_conversation("ada"), "hello")
    sched = Scheduler(replace(cfg, max_concurrent=1), store)  # old is first in line
    deadline = time.time() + 20
    while not all(
        (runs := store.list_runs(b)) and runs[0]["status"] != "running" for b in ("old", "ada")
    ):
        assert time.time() < deadline, "timed out"
        sched.tick()
        time.sleep(cfg.tick)
    sched.shutdown()
    run = store.list_runs("old")[0]
    assert run["status"] == "failed" and run["retry_after"] > run["ended_at"]
    assert "engine 'gone' is not loaded" in run["error"]
    assert "engine 'gone' is not loaded" in store.messages(user_conversation("old"))[-1].body
    unread = store.unread("old")[user_conversation("old")]
    assert [m.body for m in unread if m.sender == "user"] == ["hello"]
    assert store.list_runs("ada")[0]["status"] == "ok"
