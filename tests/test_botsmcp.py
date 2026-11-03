"""The MCP tools called in-process, for what a scripted turn cannot observe."""

import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

import botsmcp
from host.config import Config
from host.store import Store, user_conversation


@pytest.fixture
def ada(store: Store, monkeypatch: pytest.MonkeyPatch) -> Store:
    """The MCP tools running as bot ada, with bob as another bot."""
    store.create_bot("ada", "Ada", engine="fake")
    store.create_bot("bob", "Bob", engine="fake")
    monkeypatch.setattr(botsmcp, "BOT_ID", "ada")
    monkeypatch.setattr(botsmcp, "_store", store)
    return store


def test_a_bot_pauses_resumes_changes_and_deletes_its_own_routine(ada: Store) -> None:
    rid = ada.create_routine("ada", "Morning", "0 8 * * *", "brief me")

    assert botsmcp.pause_routine(rid).startswith(f"routine {rid} paused")
    assert ada.list_routines("ada")[0]["paused"] == 1
    assert botsmcp.resume_routine(rid).startswith(f"routine {rid} resumed")
    assert ada.list_routines("ada")[0]["paused"] == 0

    changed = botsmcp.update_routine(rid, cron="30 7 * * 1-5", prompt="brief me early")
    assert changed.startswith(f"routine {rid} changed")
    r = ada.list_routines("ada")[0]
    assert (r["name"], r["cron"], r["prompt"]) == ("Morning", "30 7 * * 1-5", "brief me early")
    assert botsmcp.update_routine(rid, name="Early").startswith(f"routine {rid} changed")
    assert ada.list_routines("ada")[0]["name"] == "Early"

    assert botsmcp.delete_routine(rid).startswith(f"routine {rid} deleted")
    assert ada.list_routines() == []
    events = [m.body for m in ada.messages(user_conversation("ada")) if m.kind == "event"]
    assert events[-5:] == [
        "Paused routine ⏱ Morning",
        "Resumed routine ⏱ Morning",
        "Changed routine ⏱ Morning",
        "Changed routine ⏱ Morning",
        "Deleted routine ⏱ Early",
    ]


def test_a_bot_cannot_touch_another_bots_routine(ada: Store) -> None:
    rid = ada.create_routine("bob", "Daily", "0 9 * * *", "check in")
    before = ada.list_routines()
    bobs_chat = ada.messages(user_conversation("bob"))
    refusal = f"rejected: routine {rid} is bob's, not yours"

    assert botsmcp.pause_routine(rid) == refusal
    assert botsmcp.resume_routine(rid) == refusal
    assert botsmcp.update_routine(rid, cron="* * * * *", prompt="spam") == refusal
    assert botsmcp.delete_routine(rid) == refusal
    assert ada.list_routines() == before
    assert ada.messages(user_conversation("bob")) == bobs_chat


def test_routine_changes_reject_bad_input_and_leave_the_routine_alone(ada: Store) -> None:
    rid = ada.create_routine("ada", "Morning", "0 8 * * *", "brief me")

    assert botsmcp.delete_routine(rid + 1) == f"rejected: no routine {rid + 1}"
    assert botsmcp.update_routine(rid) == "rejected: give cron, prompt or name to change"
    assert botsmcp.update_routine(rid, cron="every morning").startswith("rejected: ")
    assert botsmcp.update_routine(rid, cron="0 0 31 2 *").startswith("rejected: ")
    r = ada.list_routines()[0]
    assert (r["cron"], r["prompt"], r["paused"]) == ("0 8 * * *", "brief me", 0)


@pytest.mark.parametrize("operation", ["delete", "pause", "resume", "update"])
def test_routine_tools_reject_missing_ids(ada: Store, operation: str) -> None:
    tool = getattr(botsmcp, f"{operation}_routine")
    assert tool(99999) == "rejected: no routine 99999"
    assert ada.list_routines() == []


def test_a_group_cannot_own_a_routine(ada: Store) -> None:
    group = ada.create_group("team", "Team", ["ada", "bob"])
    with pytest.raises(ValueError):
        ada.create_routine(group, "Group", "0 8 * * *", "brief group")


def test_bad_cron_update_is_atomic_for_all_fields(ada: Store) -> None:
    rid = ada.create_routine("ada", "Morning", "0 8 * * *", "brief me")
    before = ada.list_routines()
    events = ada.messages(user_conversation("ada"))
    assert botsmcp.update_routine(
        rid, cron="0 0 31 2 *", name="Replaced", prompt="Replaced"
    ).startswith("rejected:")
    assert ada.list_routines() == before
    assert ada.messages(user_conversation("ada")) == events


@pytest.mark.parametrize("notify", [True, False])
def test_send_to_user_notifies_only_when_config_says(
    cfg: Config, store: Store, monkeypatch: pytest.MonkeyPatch, notify: bool
) -> None:
    store.create_bot("ada", "Ada", engine="fake")
    popped: list[list[str]] = []
    monkeypatch.setattr(botsmcp, "BOT_ID", "ada")
    monkeypatch.setattr(botsmcp, "_store", store)
    monkeypatch.setattr(botsmcp, "cfg", lambda: replace(cfg, notify=notify))
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **_: popped.append(argv))
    monkeypatch.setattr(sys, "platform", "darwin")

    botsmcp.send_to_user("done")

    assert store.messages(user_conversation("ada"))[-1].body == "done"
    assert [argv[0] for argv in popped] == (["osascript"] if notify else [])


@pytest.mark.parametrize("installed", [True, False])
def test_send_to_user_on_linux_notifies_through_notify_send_or_not_at_all(
    cfg: Config, store: Store, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, installed: bool
) -> None:
    store.create_bot("ada", "Ada", engine="fake")
    if installed:
        (tmp_path / "notify-send").write_text("#!/bin/sh\n")
        (tmp_path / "notify-send").chmod(0o755)
    popped: list[list[str]] = []
    monkeypatch.setenv("PATH", str(tmp_path))  # no osascript, and notify-send only if made
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(botsmcp, "BOT_ID", "ada")
    monkeypatch.setattr(botsmcp, "_store", store)
    monkeypatch.setattr(botsmcp, "cfg", lambda: replace(cfg, notify=True))
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **_: popped.append(argv))

    assert botsmcp.send_to_user("-n looks like a flag").startswith("sent")

    expected = [
        str(tmp_path / "notify-send"),
        "--app-name=bots",
        "--",
        "Ada",
        "-n looks like a flag",
    ]
    assert popped == ([expected] if installed else [])
