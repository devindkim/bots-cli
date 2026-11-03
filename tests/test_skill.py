"""The skill each bot gets: skills/being-a-bot.md plus the user's own sections."""

from __future__ import annotations

import os
from dataclasses import replace

from host import botdir
from host.config import Config

KERNEL = botdir.SKILL.read_text()


def test_no_added_sections_renders_the_skill_alone(cfg: Config) -> None:
    rendered = botdir.render_skill(cfg, "ada")
    assert rendered == KERNEL.replace(botdir.PLUGIN_SECTIONS + "\n\n", "").replace("<id>", "ada")
    assert "<!--" not in rendered


def test_isolated_mode_says_what_it_closes_and_nothing_else_changes(cfg: Config) -> None:
    """The skill must not promise reads the sandbox refuses; default mode is untouched."""
    for old in botdir.ISOLATED_SWAPS:
        assert KERNEL.count(old) == 1, old  # a reworded skill must update the swap too
    isolated = botdir.render_skill(replace(cfg, isolation="seatbelt", isolation_scope="bot"), "ada")
    assert "yours or not" not in isolated and "Others are closed on this host" in isolated
    assert "(`user:<their id>`)" not in isolated
    assert isolated.count("## Isolation\n") == 1
    assert isolated.index("## Isolation") < isolated.index("## Ask first")
    undo = isolated.replace(botdir.ISOLATED_SECTION + "\n\n", "")
    for old, new in botdir.ISOLATED_SWAPS.items():
        undo = undo.replace(new, old)
    assert undo == botdir.render_skill(cfg, "ada")


def test_the_shared_scope_says_what_it_opens_and_what_stays_each_bots_own(cfg: Config) -> None:
    for old in botdir.SHARED_SWAPS:
        assert KERNEL.count(old) == 1, old
    shared = botdir.render_skill(replace(cfg, isolation="seatbelt"), "ada")
    assert "Never read other\nbots' folders" not in shared
    assert "Others are closed on this host" in shared  # conversations stay the bot's own
    assert shared.count("## Isolation\n") == 1 and botdir.SHARED_SECTION in shared
    assert "MEMORY.md" in botdir.SHARED_SECTION and "the other bots' folders" not in shared
    undo = shared.replace(botdir.SHARED_SECTION + "\n\n", "")
    for old, new in {**botdir.ISOLATED_SWAPS, **botdir.SHARED_SWAPS}.items():
        undo = undo.replace(new, old)
    assert undo == botdir.render_skill(cfg, "ada")


def test_added_sections_go_in_by_name_before_ask_first(cfg: Config) -> None:
    (cfg.root / "skills").mkdir()
    (cfg.root / "skills" / "b-phone.md").write_text("## Phone\n\nCheck at 390px.\n")
    (cfg.root / "skills" / "a-desk.md").write_text("\n## Desk\n\nYour folder is bots/<id>/.\n\n")
    (cfg.root / "skills" / "notes.txt").write_text("not a section")
    rendered = botdir.render_skill(cfg, "ada")
    assert rendered.endswith(
        "## Desk\n\nYour folder is bots/ada/.\n\n## Phone\n\nCheck at 390px.\n\n## Ask first\n"
        + KERNEL.split("## Ask first\n", 1)[1]
    )
    assert "not a section" not in rendered


def test_agents_md_carries_the_rendered_skill(cfg: Config) -> None:
    (cfg.root / "skills").mkdir()
    (cfg.root / "skills" / "phone.md").write_text("## Phone\n\nCheck at 390px.\n")
    botdir.scaffold(cfg, "ada")
    agents = botdir.write_agents_md(cfg, "ada", "Ada").read_text()
    assert botdir.render_skill(cfg, "ada").strip() in agents
    assert "## Phone\n\nCheck at 390px.\n\n## Ask first" in agents


def test_claude_md_is_always_the_link_to_agents_md(cfg: Config) -> None:
    """Any other CLAUDE.md is what the engine loads: a link into notes/, which other bots
    write in the shared sandbox, would let them steer this bot."""
    folder = botdir.scaffold(cfg, "ada")
    (folder / "notes" / "x.md").write_text("planted\n")
    (folder / "CLAUDE.md").symlink_to("notes/x.md")
    botdir.write_agents_md(cfg, "ada", "Ada")
    assert os.readlink(folder / "CLAUDE.md") == "AGENTS.md"
    (folder / "CLAUDE.md").unlink()
    (folder / "CLAUDE.md").write_text("planted\n")
    botdir.write_agents_md(cfg, "ada", "Ada")
    assert os.readlink(folder / "CLAUDE.md") == "AGENTS.md"


def test_a_second_name_for_memory_md_is_cut_off_before_the_turn(cfg: Config) -> None:
    folder = botdir.scaffold(cfg, "ada")
    (folder / "MEMORY.md").write_text("mine\n")
    os.link(folder / "MEMORY.md", folder / "notes" / "m.md")
    botdir.write_agents_md(cfg, "ada", "Ada")
    (folder / "notes" / "m.md").write_text("planted\n")
    assert (folder / "MEMORY.md").read_text() == "mine\n"
    assert (folder / "MEMORY.md").stat().st_nlink == 1
