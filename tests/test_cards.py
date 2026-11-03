"""Option cards: a bot's question with options, shown to the user and read back by bots."""

from typing import Any

import pytest

import botsmcp
from host import turn
from host.store import Store, check_ask, user_conversation

RESTART: list[dict[str, Any]] = [
    {
        "q": "Restart the host?",
        "options": [
            {"label": "When all bots are idle", "detail": "Waits", "recommended": True},
            {"label": "Now"},
            {"label": "Not yet"},
        ],
    }
]
TWO: list[dict[str, Any]] = [
    {"q": "Which branch?", "options": [{"label": "main"}, {"label": "dev"}]},
    {"q": "Which checks?", "options": [{"label": "tests"}, {"label": "lint"}], "multi": True},
]


@pytest.fixture
def ada(store: Store, monkeypatch: pytest.MonkeyPatch) -> Store:
    """The MCP tools running as bot ada, in group team with bob."""
    store.create_bot("ada", "Ada", engine="fake")
    store.create_bot("bob", "Bob", engine="fake")
    store.create_group("team", "Team", ["ada", "bob"])
    monkeypatch.setattr(botsmcp, "BOT_ID", "ada")
    monkeypatch.setattr(botsmcp, "_store", store)
    return store


def asked(store: Store, ask: list[dict[str, Any]] = RESTART) -> int:
    msg = store.send_to_user("ada", "A question", ask=ask)
    assert msg is not None
    return msg.id


def test_check_ask_tidies_and_fills_defaults() -> None:
    ask = check_ask([{"q": "  Go\n now? ", "options": [{"label": " Yes "}, {"label": "No"}]}])

    assert ask == [{"q": "Go now?", "options": [{"label": "Yes"}, {"label": "No"}], "multi": False}]


@pytest.mark.parametrize(
    ("ask", "says"),
    [
        ("pick one", "ask must be a list"),
        ([], "1 to 4 questions, not 0"),
        ([TWO[0]] * 5, "1 to 4 questions, not 5"),
        ([{"q": "x", "options": [{"label": "a"}]}], "question 1 needs 2 to 6 options, not 1"),
        ([{"q": "x", "options": [{"label": str(i)} for i in range(7)]}], "not 7"),
        ([{"q": "x" * 201, "options": TWO[0]["options"]}], "question 1 q is over 200"),
        ([{"q": " ", "options": TWO[0]["options"]}], "question 1 q is empty"),
        ([{"q": "x", "options": [{"label": "a" * 61}, {"label": "b"}]}], "label is over 60"),
        ([{"q": "x", "options": [{"label": "a", "detail": "d" * 121}, {"label": "b"}]}], "120"),
        (
            [
                {
                    "q": "x",
                    "options": [{"label": l, "recommended": True} for l in "ab"],
                }
            ],
            "recommends more than one",
        ),
        ([{"q": "x", "options": [{"label": "a"}, {"label": "A"}]}], "offers 'A' twice"),
        ([{"q": "x", "options": [{"lable": "a"}, {"label": "b"}]}], "unknown field 'lable'"),
        ([{"q": "x", "options": TWO[0]["options"], "multi": "yes"}], "multi must be true"),
        ([{"q": "x", "options": ["a", "b"]}], "option 1 must be an object"),
    ],
)
def test_check_ask_says_what_is_wrong(ask: Any, says: str) -> None:
    with pytest.raises((ValueError, TypeError)) as e:
        check_ask(ask)
    assert says in str(e.value)


def test_send_to_user_with_ask_stores_the_card(ada: Store) -> None:
    assert botsmcp.send_to_user("Restart?", ask=RESTART).startswith("sent")

    msg = ada.messages(user_conversation("ada"))[-1]
    assert msg.body == "Restart?"
    assert msg.card == {"ask": check_ask(RESTART)}


def test_a_bad_ask_sends_nothing_and_says_why(ada: Store) -> None:
    reply = botsmcp.send_to_user("Restart?", ask=[{"q": "x", "options": [{"label": "Other"}]}])

    assert reply.startswith("rejected: question 1 needs 2 to 6 options")
    assert [m for m in ada.messages(user_conversation("ada")) if m.kind == "text"] == []


def test_send_to_group_with_ask_stores_the_card(ada: Store) -> None:
    assert botsmcp.send_to_group("team", "Which?", ask=TWO).startswith("sent")
    assert botsmcp.send_to_group("team", "Bad", ask=[]).startswith("rejected: ask needs 1 to 4")

    [msg] = [m for m in ada.messages("group:team") if m.kind == "text"]
    assert msg.card == {"ask": check_ask(TWO)}


def test_plain_sends_have_no_card(ada: Store) -> None:
    botsmcp.send_to_user("hi")
    assert ada.messages(user_conversation("ada"))[-1].card is None


def test_bots_read_back_the_options_they_offered(ada: Store) -> None:
    botsmcp.send_to_group("team", "Which?", ask=TWO)
    shown = botsmcp.read_conversation("group:team")

    assert "] Which?\n[ask: Which branch?: A) main | B) dev]\n" in shown
    assert "[ask: Which checks? (pick any): A) tests | B) lint]" in shown
    [msg] = [m for m in ada.messages("group:team") if m.kind == "text"]
    assert turn._render(msg, "bob").endswith(
        "Which?\n[ask: Which branch?: A) main | B) dev]"
        "\n[ask: Which checks? (pick any): A) tests | B) lint]"
    )
    restart = ada.get_message(asked(ada))
    assert restart is not None
    assert "A) When all bots are idle (recommended) | B) Now | C) Not yet]" in turn._render(
        restart, "ada"
    )


def test_attention_count_keeps_option_cards_and_respects_read_archive(store: Store) -> None:
    store.create_bot("ada", "Ada")
    store.create_bot("bob", "Bob")
    group = store.create_group("team", "Team", ["ada", "bob"])
    chat = user_conversation("ada")
    store.mark_read("user", chat)
    store.mark_read("user", group)
    store.send_to_user("ada", "Choose", ask=RESTART)
    store.send_to_group("ada", "team", "Choose", None, ask=RESTART)
    assert store.user_unread_counts() == {chat: 1, group: 1}
    store.send_to_group("ada", "team", "@bob choose", None, ask=RESTART)
    assert store.user_unread_counts() == {chat: 1, group: 1}
    assert "bob" in store.due_bots()
    store.mark_read("user", chat)
    assert store.user_unread_counts() == {group: 1}
    store.archive_conversation(group)
    assert store.user_unread_counts() == {}
