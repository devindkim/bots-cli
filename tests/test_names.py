"""Bot names: unique, tidy, addressable by @Name, and kept out of SOUL.md."""

from __future__ import annotations

import pytest

from host import botdir
from host.config import Config
from host.store import Store
from host.turn import envelope


@pytest.mark.parametrize("bad", ["", "   ", "x" * 61, None, 3])
def test_bad_names_are_refused(store: Store, bad: object) -> None:
    with pytest.raises((ValueError, TypeError)):
        store.create_bot("ada", bad)  # type: ignore[arg-type]
    assert store.get_bot("ada") is None


def test_names_are_unique_ignoring_case(store: Store) -> None:
    store.create_bot("a", "Ada")
    with pytest.raises(ValueError, match="taken by a"):
        store.create_bot("c", "ada")


def test_an_archived_bot_keeps_its_name_reserved(store: Store) -> None:
    store.create_bot("a", "Ada")
    store.archive_bot("a")
    with pytest.raises(ValueError, match="taken by a"):
        store.create_bot("b", "Ada")


def test_a_group_created_without_a_name_is_named_by_its_id(store: Store) -> None:
    store.create_bot("a", "Ada")
    store.create_bot("b", "Grace")
    conv = store.create_group("team", "", ["a", "b"])
    assert store.get_conversation(conv)["name"] == "team"  # type: ignore[index]


def test_at_name_wakes_only_that_member(store: Store) -> None:
    for bot_id, name in (("bot-1", "Ada"), ("bot-2", "Grace"), ("bot-3", "Linus")):
        store.create_bot(bot_id, name)
    conv = store.create_group("team", "Team", ["bot-1", "bot-2", "bot-3"])
    assert store.user_send(conv, "@grace can you look?").recipients == ["bot-2"]
    assert store.user_send(conv, "@Grace, @bot-3: both").recipients == ["bot-2", "bot-3"]
    assert store.user_send(conv, "mail grace@Grace.dev").recipients == [
        "bot-1",
        "bot-2",
        "bot-3",
    ]  # no mention: everyone
    assert store.user_send(conv, "@Graceful").recipients == ["bot-1", "bot-2", "bot-3"]
    sent = store.post(conv, "bot-1", "@Linus over to you")
    assert sent is not None and sent.recipients == ["user", "bot-3"]


def test_at_name_is_ignored_when_it_could_mean_two_bots(store: Store) -> None:
    store.create_bot("ada", "bob")  # a name that is another member's id
    store.create_bot("bob", "Bobby")
    store.create_bot("cy", "Ada Lovelace")  # two words: not a mention
    conv = store.create_group("team", "Team", ["ada", "bob", "cy"])
    assert store.user_send(conv, "@bob hi").recipients == ["bob"]
    assert store.user_send(conv, "@Ada hi").recipients == ["ada"]  # the id still works
    assert store.user_send(conv, "@Lovelace hi").recipients == ["ada", "bob", "cy"]


def test_at_dotted_name_does_not_also_wake_its_id_prefix(store: Store) -> None:
    store.create_bot("ada", "Grace")
    store.create_bot("worker", "Ada.Lovelace")
    store.create_bot("third", "Linus")
    conv = store.create_group("team", "Team", ["ada", "worker", "third"])
    assert store.user_send(conv, "@Ada.Lovelace please review").recipients == ["worker"]


def test_at_dotted_name_does_not_also_wake_a_shorter_name(store: Store) -> None:
    store.create_bot("bot-1", "Ada")
    store.create_bot("bot-2", "Ada.Lovelace")
    store.create_bot("bot-3", "Linus")
    conv = store.create_group("team", "Team", ["bot-1", "bot-2", "bot-3"])
    assert store.user_send(conv, "@ada.lovelace look").recipients == ["bot-2"]
    assert store.user_send(conv, "@Ada. Then @bot-3: you").recipients == ["bot-1", "bot-3"]


def test_soul_starts_without_a_name_and_agents_md_carries_it(cfg: Config, store: Store) -> None:
    store.create_bot("bot-2", "bot-2", title="friend")
    folder = botdir.scaffold(cfg, "bot-2")
    assert (folder / "SOUL.md").read_text().startswith("## Who I report to")

    agents = botdir.write_agents_md(cfg, "bot-2", "Ada", "friend").read_text()
    assert "# Ada (id bot-2)\n\nfriend\n\n---\n\n## Who I report to" in agents
    agents = botdir.write_agents_md(cfg, "bot-2", "Grace").read_text()
    assert "# Grace (id bot-2)\n\n---\n\n## Who I report to" in agents


def test_the_envelope_names_the_bot_once_in_the_roster(cfg: Config, store: Store) -> None:
    store.create_bot("ada", "Ada", title="researcher")
    prompt, _ = envelope(cfg, store, "ada")
    assert "You are" not in prompt
    assert "- ada: Ada, researcher (you)" in prompt


OLD_SOUL = "# bot-1\n\nfriend\n\n## Who I report to\n\nThe user.\n"


def test_at_unicode_name_matches_its_original_spelling(store: Store) -> None:
    store.create_bot("worker", "Straße")
    store.create_bot("other", "Grace")
    conv = store.create_group("unicode", "Unicode", ["worker", "other"])
    assert store.user_send(conv, "@Straße please review").recipients == ["worker"]
    assert store.user_send(conv, "@strasse and @STRASSE").recipients == ["worker"]
