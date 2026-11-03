import sqlite3
import threading
import time
from pathlib import Path

import pytest

from host.config import Config
from host.store import (
    Rejected,
    Store,
    local_zone,
    next_routine_at,
    open_store,
    pair_conversation,
    user_conversation,
)


def test_addressing_unread_and_due_order(store: Store) -> None:
    for bot in ("chief", "researcher", "writer"):
        store.create_bot(bot, bot.title())
    group = store.create_group("team", "Team", ["chief", "researcher", "writer"])

    store.send_to_agent("chief", "researcher", "find sources")
    assert store.due_bots() == ["researcher"]
    # delegation shows up as an event in the chief's chat with me, waking nobody
    assert store.messages(user_conversation("chief"))[-1].body == "-> researcher: find sources"

    store.user_send(group, "@writer draft it")  # mention: only the writer is woken
    assert store.due_bots() == ["writer", "researcher"]  # mine first
    # unaddressed group traffic is still delivered, it just does not wake the chief
    assert store.unread("chief")[group][-1].body == "@writer draft it"

    store.user_send(group, "everyone, stand up")  # no mention: every member
    assert set(store.due_bots()) == {"chief", "researcher", "writer"}


def test_my_unread_counts_only_bot_texts_to_me(store: Store) -> None:
    for bot in ("ada", "bob"):
        store.create_bot(bot, bot.title())
    group = store.create_group("team", "Team", ["ada", "bob"])
    chat = user_conversation("ada")
    for conv in (chat, group):
        store.mark_read("user", conv)
    assert store.user_unread_counts() == {}

    store.send_to_agent("ada", "bob", "Bob, can you check the tests?")
    assert store.messages(chat)[-1].body.startswith("-> bob:")  # the copy is still shown
    store.post_event(chat, "run failed (exit 1). Retrying at 19:40.")
    store.send_to_group("ada", "team", "@bob the tests pass on my side", None)
    store.send_to_group("bob", "team", "@Ada thanks", None)  # by name, same thing
    assert store.user_unread_counts() == {}  # events and chatter never count

    store.user_send(group, "@ada status?")
    store.user_send(chat, "hi")
    assert store.user_unread_counts() == {}  # nor do my own messages

    store.send_to_user("ada", "done, see shared/x.md")
    store.send_to_group("bob", "team", "all green, ready to merge", None)
    assert store.user_unread_counts() == {chat: 1, group: 1}

    store.mark_read("user", chat)  # reading resets it as before
    assert store.user_unread_counts() == {group: 1}


def test_run_ok_advances_cursors_failure_retries_after_backoff(store: Store) -> None:
    store.create_bot("ada", "Ada")
    conv = user_conversation("ada")
    store.user_send(conv, "hi")
    run = store.start_run("ada")
    store.add_delivered(run, {c: ms[-1].id for c, ms in store.unread("ada").items()})
    retry = time.time() + 60
    store.finish_run(run, "failed", error="exit 1", retry_after=retry)
    assert store.bot_state("ada") == "idle" and store.due_bots() == []
    assert store.due_bots(now=retry + 1) == ["ada"]  # no message from me needed
    assert store.failures_in_a_row("ada") == 1

    store.db.execute("UPDATE runs SET retry_after = 0")
    run = store.start_run("ada")
    store.add_delivered(run, {c: ms[-1].id for c, ms in store.unread("ada").items()})
    store.send_to_user("ada", "which repo?")
    store.finish_run(run, "ok")
    assert store.unread("ada") == {}
    assert store.bot_state("ada") == "idle"
    assert store.user_unread_counts() == {user_conversation("ada"): 1}


def test_bots_talk_freely_nonce_dedups(store: Store) -> None:
    store.create_bot("a", "A")
    store.create_bot("b", "B")
    assert store.send_to_agent("a", "b", "ping", nonce="n1")
    assert store.send_to_agent("a", "b", "ping", nonce="n1") is None
    for i in range(20):  # no cap on bot-to-bot traffic; spend is the only limit
        store.send_to_agent("b" if i % 2 else "a", "a" if i % 2 else "b", f"round {i}")
    assert len(store.messages(pair_conversation("a", "b"), 50)) == 21
    with pytest.raises(Rejected):
        store.send_to_agent("a", "nobody", "hello")


def test_routines_fire_on_local_time_and_skip_missed(store: Store) -> None:
    from datetime import datetime, timedelta

    store.create_bot("ada", "Ada")
    store.create_routine("ada", "Morning", "0 9 * * *", "plan the day")
    tomorrow = datetime.now() + timedelta(days=1)
    nine = tomorrow.replace(hour=9, minute=0, second=0, microsecond=0).timestamp()
    assert store.fire_due_routines(since=nine - 60, now=nine - 1) == []
    assert store.fire_due_routines(since=nine - 60, now=nine + 1) == [1]
    # host was down over the firing: since = its start time, so it is skipped
    assert store.fire_due_routines(since=nine + 3600, now=nine + 7200) == []


LA = "America/Los_Angeles"


@pytest.mark.parametrize("cron", ["*/15 1 * * *", "* 1 * * *", "0,30 1 * * *"])
def test_fixed_hour_skips_entire_repeated_hour(monkeypatch: pytest.MonkeyPatch, cron: str) -> None:
    monkeypatch.setenv("TZ", LA)
    base = _la(2026, 11, 1, 1, 59)
    assert next_routine_at(cron, base) == _la(2026, 11, 2, 1)


def _la(y: int, mo: int, d: int, h: int, mi: int = 0, fold: int = 0) -> float:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    return datetime(y, mo, d, h, mi, tzinfo=ZoneInfo(LA), fold=fold).timestamp()


@pytest.mark.parametrize(
    ("cron", "base", "fires"),
    [
        # fall back 2026-11-01: last fired before the change, next is still 08:30 wall time
        ("30 8 * * *", _la(2026, 10, 31, 9), [_la(2026, 11, 1, 8, 30), _la(2026, 11, 2, 8, 30)]),
        # spring forward 2027-03-14
        ("30 8 * * *", _la(2027, 3, 13, 9), [_la(2027, 3, 14, 8, 30), _la(2027, 3, 15, 8, 30)]),
        # 02:30 doesn't exist on 2027-03-14: fires once, at 03:00 when the clock jumps
        ("30 2 * * *", _la(2027, 3, 13, 9), [_la(2027, 3, 14, 3), _la(2027, 3, 15, 2, 30)]),
        # 01:30 happens twice on 2026-11-01: a fixed-hour routine fires the first time only
        ("30 1 * * *", _la(2026, 10, 31, 9), [_la(2026, 11, 1, 1, 30), _la(2026, 11, 2, 1, 30)]),
        # an every-hour routine fires in both 01:00 hours, each a real hour apart
        ("0 * * * *", _la(2026, 11, 1, 0, 30), [_la(2026, 11, 1, 1), _la(2026, 11, 1, 1, fold=1)]),
    ],
    ids=[
        "fall-back",
        "spring-forward",
        "skipped-hour-fires-once",
        "repeated-hour-fires-once",
        "hourly-fires-both",
    ],
)
def test_routines_keep_wall_time_across_clock_changes(
    monkeypatch: pytest.MonkeyPatch, cron: str, base: float, fires: list[float]
) -> None:
    monkeypatch.setenv("TZ", LA)
    got = [next_routine_at(cron, base)]
    got.append(next_routine_at(cron, got[0]))
    assert got == fires


def test_local_zone_reads_tz_then_the_localtime_link(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from zoneinfo import ZoneInfo

    monkeypatch.setenv("TZ", ":Europe/Berlin")
    assert local_zone() == ZoneInfo("Europe/Berlin")
    monkeypatch.setenv(
        "TZ", "EST5EDT,M3.2.0,M11.1.0"
    )  # POSIX rule, not a zone: leave it to the C library
    assert local_zone() is None

    monkeypatch.delenv("TZ")
    link = tmp_path / "localtime"
    link.symlink_to("/var/db/timezone/zoneinfo/Asia/Tokyo")
    assert local_zone(str(link)) == ZoneInfo("Asia/Tokyo")
    for target in ("/etc/UTC", "/somewhere/zoneinfo/Not/AZone"):
        link.unlink()
        link.symlink_to(target)
        assert local_zone(str(link)) is None
    link.unlink()
    link.write_text("not a link")
    assert local_zone(str(link)) is None
    assert local_zone(str(tmp_path / "missing")) is None


def test_routines_fall_back_to_fixed_offset_without_a_zone(monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import datetime

    monkeypatch.setattr("host.store.local_zone", lambda: None)
    base = time.time()
    expected = (
        datetime.fromtimestamp(base).astimezone().replace(second=0, microsecond=0).timestamp() + 60
    )
    assert next_routine_at("* * * * *", base) == expected


def test_archiving_bots_and_groups_closes_them_and_keeps_history(store: Store) -> None:
    for bot in ("a", "b", "c"):
        store.create_bot(bot, bot.upper())
    group = store.create_group("team", "Team", ["a", "b", "c"])
    store.create_routine("c", "Daily", "0 9 * * *", "check in")
    store.user_send(group, "hello")

    store.archive_bot("b")
    assert [g["members"] for g in store.list_groups()] == [["a", "c"]]
    assert "b" not in {r["id"] for r in store.roster()}
    assert store.list_routines("c")[0]["paused"] == 0  # only b's routines pause
    with pytest.raises(ValueError):
        store.user_send(user_conversation("b"), "still there?")

    store.archive_conversation(group)
    assert store.list_groups() == [] and store.due_bots() == []
    assert store.messages(group)  # history kept


def _run_costing(store: Store, bot_id: str, cost: float) -> None:
    run = store.start_run(bot_id)
    store.update_run(run, cost=cost)
    store.finish_run(run, "ok")


def test_the_daily_budget_is_off_unless_configured(tmp_path: Path) -> None:
    assert Config(root=tmp_path).daily_budget == 0
    store = open_store(Config(root=tmp_path))
    store.create_bot("ada", "Ada")
    _run_costing(store, "ada", 500)
    assert not store.over_budget()


def test_one_daily_budget_stops_every_bot_until_midnight(store: Store) -> None:
    store.create_bot("ada", "Ada")
    store.create_bot("bob", "Bob")
    store.daily_budget = 1.0
    _run_costing(store, "ada", 0.6)
    store.user_send(user_conversation("bob"), "go")
    assert store.due_bots() == ["bob"]

    _run_costing(store, "bob", 0.6)  # the bots together cross it
    store.mark_read("bob", user_conversation("bob"))
    store.user_send(user_conversation("ada"), "more")
    assert store.due_bots() == []
    assert store.bot_state("ada") == "over_budget"  # has work it cannot start
    assert store.bot_state("bob") == "idle"  # has none
    assert (store.spent_today(), store.spent_today("ada")) == (1.2, 0.6)
    reached = [m for m in store.messages(user_conversation("bob")) if "budget" in m.body]
    assert len(reached) == 1 and "$1.00 reached" in reached[0].body
    assert "daily_budget in config.toml" in reached[0].body
    _run_costing(store, "ada", 0.6)  # further runs do not repeat it
    assert not [m for m in store.messages(user_conversation("ada")) if "reached" in m.body]

    tomorrow = time.time() + 86400
    assert store.spent_today(now=tomorrow) == 0 and store.due_bots(tomorrow) == ["ada"]


def test_groups_need_two_bots_from_the_start(store: Store) -> None:
    store.create_bot("a", "A")
    store.create_bot("b", "B")
    for members in ([], ["a"], ["a", "a"]):
        with pytest.raises(ValueError, match="at least two"):
            store.create_group("solo", "Solo", members)
    assert store.list_groups() == []


@pytest.mark.parametrize("members", [["a", "missing"], ["a", "old"], ["a", "a"]])
def test_group_member_validation_leaves_groups_unchanged(store: Store, members: list[str]) -> None:
    for bot in ("a", "b", "old"):
        store.create_bot(bot, bot)
    store.archive_bot("old")
    group = store.create_group("team", "Team", ["a", "b"])
    before = store.list_groups()
    events = store.messages(group)
    with pytest.raises(ValueError):
        store.create_group("other", "Other", members)
    assert store.list_groups() == before
    assert store.messages(group) == events


def test_archived_bots_take_no_routines_and_archive_once(store: Store) -> None:
    store.create_bot("a", "A")
    store.archive_bot("a")
    with pytest.raises(ValueError, match="no bot"):
        store.create_routine("a", "Daily", "0 9 * * *", "check in")
    with pytest.raises(ValueError, match="no bot"):
        store.archive_bot("a")
    with pytest.raises(ValueError):  # archiving goes through archive_bot / unarchive_bot only
        store.update_bot("a", archived=0)


def test_unarchived_bot_is_reachable_again(store: Store) -> None:
    store.create_bot("a", "A")
    store.create_routine("a", "Daily", "0 9 * * *", "check in")
    store.archive_bot("a")
    store.unarchive_bot("a")
    assert "a" in {b["id"] for b in store.roster()}
    store.user_send(user_conversation("a"), "welcome back")
    assert store.due_bots() == ["a"]
    assert store.list_routines("a")[0]["paused"] == 1  # mine to turn back on
    with pytest.raises(ValueError):
        store.unarchive_bot("a")


def test_a_bot_made_without_an_id_gets_the_next_neutral_one(store: Store) -> None:
    assert store.create_bot("", "Ada") == "bot-1"
    store.create_bot("bot-7", "Bea")
    store.create_bot("bot-x", "Cy")  # not bot-<n>, so it does not count
    store.create_bot("chief", "Chief")
    store.archive_bot("bot-7")  # archived ids are still taken
    assert store.create_bot("", "Dee") == "bot-8"
    assert store.get_bot("bot-8")["name"] == "Dee"  # type: ignore[index]


def test_bots_made_at_once_get_different_neutral_ids(store: Store) -> None:
    made: list[str] = []
    threads = [  # each thread gets its own connection, as the host's do
        threading.Thread(target=lambda n=n: made.append(store.create_bot("", f"Bot {n}")))
        for n in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(made) == sorted(f"bot-{n}" for n in range(1, 9))


def test_bots_and_groups_share_one_set_of_ids(store: Store) -> None:
    store.create_bot("a", "A")
    store.create_bot("b", "B")
    with pytest.raises(ValueError, match="'a' is a bot"):
        store.create_group("a", "A team", ["a", "b"])
    store.create_group("team", "Team", ["a", "b"])
    with pytest.raises(ValueError, match="'team' is a group"):
        store.create_bot("team", "Teammate")


def test_mentions_ignore_case_and_skip_emails(store: Store) -> None:
    for bot in ("bot-2", "bot-3"):
        store.create_bot(bot, bot.title())
    conv = store.create_group("team", "Team", ["bot-2", "bot-3"])
    assert store.user_send(conv, "@BOT-2 and @Bot-3, see this.").recipients == ["bot-2", "bot-3"]
    everyone = store.user_send(conv, "mail me@bot-2.dev or x.@bot-3")
    assert everyone.recipients == ["bot-2", "bot-3"]  # no mention, so every member
    assert store.user_send(conv, "@bot-3 only").recipients == ["bot-3"]


def test_moved_since_skips_my_own_echoes_and_what_i_was_given(store: Store) -> None:
    for bot in ("a", "b", "c"):
        store.create_bot(bot, bot.upper())
    group = store.create_group("team", "Team", ["a", "b"])
    since = time.time() - 1
    store.send_to_agent("a", "b", "hello")  # echoes "-> b: hello" into user:a
    given = store.user_send(group, "for a and b")
    store.send_to_agent("b", "c", "elsewhere")
    moved = store.moved_since("a", since, {group: given.id})
    assert moved == {pair_conversation("b", "c"): 1}


def test_a_due_bot_waiting_for_a_slot_is_queued(store: Store) -> None:
    store.create_bot("ada", "Ada")
    assert store.bot_state("ada") == "idle"
    store.user_send(user_conversation("ada"), "hi")
    assert store.bot_state("ada") == "queued"
    store.start_run("ada")
    assert store.bot_state("ada") == "working"


def test_group_reply_is_held_when_someone_answered_first(store: Store) -> None:
    from host.store import Held

    for bot in ("a", "b"):
        store.create_bot(bot, bot.upper())
    group = store.create_group("team", "Team", ["a", "b"])
    store.user_send(group, "what's 2+2?")
    runs = {}
    for bot in ("a", "b"):  # both wake together and are given my question
        runs[bot] = store.start_run(bot)
        store.add_delivered(runs[bot], {c: ms[-1].id for c, ms in store.unread(bot).items()})

    assert store.send_to_group("a", group, "4", runs["a"])
    with pytest.raises(Held) as held:
        store.send_to_group("b", group, "it's 4", runs["b"])
    assert [m.body for m in held.value.missed] == ["4"]
    # b has now seen a's answer, so if it still has something to add, it goes through
    assert store.send_to_group("b", group, "and 2*2 is 4 too", runs["b"])


@pytest.mark.parametrize("cron", ["0 0 31 2 *", "not a cron"])
def test_routine_rejects_unreachable_schedule_on_create_and_update(store: Store, cron: str) -> None:
    store.create_bot("ada", "Ada")
    with pytest.raises(ValueError, match="bad cron"):
        store.create_routine("ada", "Invalid", cron, "hello")
    assert store.list_routines() == []
    routine = store.create_routine("ada", "Valid", "* * * * *", "hello")
    with pytest.raises(ValueError, match="bad cron"):
        store.update_routine(routine, cron=cron)
    assert store.list_routines()[0]["cron"] == "* * * * *"


def test_routine_update_with_nothing_is_a_value_error(store: Store) -> None:
    store.create_bot("ada", "Ada")
    routine = store.create_routine("ada", "Valid", "* * * * *", "hello")
    with pytest.raises(ValueError, match="nothing to update"):
        store.update_routine(routine)


@pytest.mark.parametrize("cron", ["0 0 31 2 *", "not a cron"])
def test_an_invalid_routine_is_paused_without_blocking_healthy_routines(
    store: Store, cron: str
) -> None:
    import time

    store.create_bot("ada", "Ada")
    invalid = store.create_routine("ada", "Broken", "* * * * *", "broken")
    healthy = store.create_routine("ada", "Healthy", "* * * * *", "healthy")
    store.db.execute("UPDATE routines SET cron = ? WHERE id = ?", (cron, invalid))
    since = time.time() + 60
    assert store.fire_due_routines(since=since, now=since + 61) == [healthy]
    assert store.list_routines()[0]["paused"] == 1
    events = [m for m in store.messages(user_conversation("ada")) if "paused" in m.body]
    assert len(events) == 1 and "Broken" in events[0].body
    before = store.messages(user_conversation("ada"))
    assert store.fire_due_routines(since=since, now=since + 61) == []
    assert store.messages(user_conversation("ada")) == before


def test_pair_retry_is_a_noop(store: Store) -> None:
    for bot in ("a", "b"):
        store.create_bot(bot, bot.upper())
    for i in range(3):
        store.send_to_agent("a", "b", "ping", nonce=f"pair-{i}")
    before = store.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    assert store.send_to_agent("a", "b", "ping", nonce="pair-2") is None
    assert store.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == before


def test_group_retry_after_turn_cap_is_a_noop(store: Store) -> None:
    for bot in ("a", "b"):
        store.create_bot(bot, bot.upper())
    group = store.create_group("team", "Team", ["a", "b"])
    for i in range(3):
        store.send_to_group("a", group, "ping", run_id=None, nonce=f"group-{i}")
    before = store.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    assert store.send_to_group("a", group, "ping", None, nonce="group-2") is None
    assert store.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == before
    with pytest.raises(Rejected):
        store.send_to_group("a", group, "new message", None)


@pytest.mark.parametrize(
    ("given", "stored"), [(" sonnet ", "sonnet"), ("", None), ("  ", None), (None, None)]
)
def test_model_is_trimmed_and_blank_clears_it(
    store: Store, given: str | None, stored: str | None
) -> None:
    store.create_bot("ada", "Ada", model="opus")
    store.update_bot("ada", model=given)
    assert store.get_bot("ada")["model"] == stored  # type: ignore[index]
    store.create_bot("bea", "Bea", model=given)
    assert store.get_bot("bea")["model"] == stored  # type: ignore[index]


def test_model_must_be_text(store: Store) -> None:
    store.create_bot("ada", "Ada")
    with pytest.raises(TypeError, match="model must be a string"):
        store.update_bot("ada", model=5)
    with pytest.raises(TypeError, match="model must be a string"):
        store.create_bot("bea", "Bea", model=["opus"])  # type: ignore[arg-type]
    assert store.get_bot("bea") is None


def test_close_thread_closes_only_that_threads_connection(store: Store) -> None:
    mine = store.db
    errors: list[str] = []

    def work() -> None:
        conn = store.db
        store.close_thread()
        try:
            conn.execute("SELECT 1")
        except sqlite3.ProgrammingError as e:
            errors.append(str(e))
        assert store.db is not conn  # reopened on next use

    worker = threading.Thread(target=work)
    worker.start()
    worker.join()
    assert errors == ["Cannot operate on a closed database."]
    assert store.db is mine  # this thread's connection is untouched


def test_the_store_refuses_what_the_types_rule_out(store: Store) -> None:
    store.create_bot("ada", "Ada", engine="fake")
    with pytest.raises(sqlite3.IntegrityError):
        store.post(user_conversation("ada"), "user", "hi", kind="note")  # type: ignore[arg-type]
    with pytest.raises(sqlite3.IntegrityError):
        store.db.execute(
            "INSERT INTO conversations (id, kind, members, created_at)"
            " VALUES ('pair:ada:bob', 'group', '[]', 1)"
        )
    run = store.start_run("ada")
    with pytest.raises(sqlite3.IntegrityError):  # running, yet ended
        store.db.execute("UPDATE runs SET ended_at = 1 WHERE id = ?", (run,))
    with pytest.raises(sqlite3.IntegrityError):  # only a failed run waits to retry
        store.update_run(run, retry_after=1.0)
    with pytest.raises(TypeError, match="paused must be true or false"):
        store.update_bot("ada", paused="yes")
    routine = store.create_routine("ada", "r", "0 9 * * *", "go")
    with pytest.raises(TypeError, match="paused must be true or false"):
        store.update_routine(routine, paused=2)
    store.update_bot("ada", paused=True)
    store.update_routine(routine, paused=1)
    assert store.get_bot("ada")["paused"] == 1  # type: ignore[index]
