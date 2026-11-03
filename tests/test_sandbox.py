"""Isolated mode on macOS: real turns of the fake engine under Seatbelt, with canaries."""

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from host import botdir, engine_keys, sandbox
from host.config import Config
from host.engines.claude import ClaudeEngine
from host.engines.codex import CodexEngine
from host.engines.fake import FakeEngine
from host.mcpsock import McpSocket, peer_uid
from host.scheduler import Scheduler
from host.store import Store, open_store, user_conversation

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" or not os.access(sandbox.SANDBOX_EXEC, os.X_OK),
    reason="Seatbelt is macOS only",
)

REAL_HOME = Path.home()
DENIED = "Operation not permitted"


@pytest.fixture
def iso(cfg: Config) -> Config:
    return replace(cfg, isolation="seatbelt", isolation_scope="bot")


@pytest.fixture
def host(iso: Config) -> Iterator[tuple[Store, Scheduler, McpSocket]]:
    store = open_store(iso)
    sock = McpSocket(iso, store, sandbox.socket_path(iso))
    sock.start()
    sched = Scheduler(iso, store, mcp_socket=sock)
    try:
        yield store, sched, sock
    finally:
        sock.stop()


def run_turn(
    cfg: Config, store: Store, sched: Scheduler, bot_id: str, steps: list[dict[str, Any]]
) -> int:
    """One turn of `bot_id` playing `steps`; returns its run id once it has ended."""
    if store.get_bot(bot_id) is None:
        store.create_bot(bot_id, bot_id.title(), engine="fake")
    folder = botdir.scaffold(cfg, bot_id)
    (folder / "fake_script.json").write_text(json.dumps(steps))
    store.user_send(user_conversation(bot_id), "go")
    deadline = time.time() + 30
    while True:
        sched.tick()
        runs = store.list_runs(bot_id)
        if runs and runs[0]["status"] != "running" and sched._turns.get(bot_id) is None:
            return int(runs[0]["id"])
        assert time.time() < deadline, "the turn did not end"
        time.sleep(cfg.tick)


def outputs(cfg: Config, run_id: int) -> list[tuple[str, int]]:
    """(output, exit code) of each shell step, in order, from the run's stdout.log."""
    out = []
    for line in (cfg.logs_dir / str(run_id) / "stdout.log").read_text().splitlines():
        ev = json.loads(line)
        if ev.get("e") == "tool_end" and "code" in ev:
            out.append((ev["output"], ev["code"]))
    return out


def shells(cfg: Config, store: Store, sched: Scheduler, *commands: str) -> list[tuple[str, int]]:
    run_id = run_turn(cfg, store, sched, "ada", [{"shell": c} for c in commands])
    got = outputs(cfg, run_id)
    assert len(got) == len(commands), (cfg.logs_dir / str(run_id) / "stderr.log").read_text()
    return got


def tool_results(cfg: Config, run_id: int) -> list[str]:
    lines = (cfg.logs_dir / str(run_id) / "stdout.log").read_text().splitlines()
    evs = [json.loads(line) for line in lines]
    return [ev["output"] for ev in evs if ev.get("e") == "tool_end" and "code" not in ev]


# -- the data folder, siblings, $HOME -------------------------------------------


def test_the_data_folder_and_siblings_are_closed_and_its_own_paths_open(
    iso: Config, host: tuple[Store, Scheduler, McpSocket]
) -> None:
    store, sched, _ = host
    store.create_bot("bob", "Bob", engine="fake")
    bob = botdir.scaffold(iso, "bob")
    (iso.secrets_dir).mkdir(exist_ok=True)
    (iso.secrets_dir / "token").write_text("secret-token")
    (iso.root / "engine-home" / "bob").mkdir(parents=True)
    (iso.root / "engine-home" / "bob" / "x").write_text("bob's login")
    caps = iso.root / "run" / "cap"
    caps.mkdir(parents=True, exist_ok=True)
    (caps / "999").write_text("someone else's capability")
    db = iso.db_path

    got = shells(
        iso,
        store,
        sched,
        f"cat {db}",
        "cat ../../bots.sqlite3",  # .. traversal
        f"cat {bob / 'SOUL.md'}",
        f"echo x > {bob / 'x.md'}",
        f"cat {iso.secrets_dir / 'token'}",
        f"ls {iso.root / 'engine-home' / 'bob'}",
        f"cat {caps / '999'}",
        f"ln -s {db} link && cat link",  # the link's target is what is judged
        f"ls {iso.root}",
        "echo mine > mine.md && cat mine.md",
        "echo shared > ../../shared/s.md && cat ../../shared/s.md",
        "echo remembered >> MEMORY.md && echo ok",
        "echo rules > SOUL.md",
        "mv SOUL.md SOUL.old",
        "ln SOUL.md hard",  # a hard link would make SOUL.md writable by another name
    )
    for output, code in got[:9]:
        assert code != 0 and DENIED in output, output
    assert got[9] == ("mine\n", 0)
    assert got[10] == ("shared\n", 0)
    assert got[11][1] == 0
    for output, code in got[12:]:
        assert code != 0 and DENIED in output, output
    assert not (bob / "x.md").exists()
    assert (iso.bot_dir("ada") / "SOUL.md").read_text() == botdir.SOUL_TEMPLATE


def test_home_and_the_engines_shared_state_are_closed(
    iso: Config, host: tuple[Store, Scheduler, McpSocket]
) -> None:
    """A: the user's ~/.claude and ~/.codex steer every engine run on the machine, so a
    write there is an escape; each bot gets its own engine home instead."""
    store, sched, _ = host
    claude_settings = REAL_HOME / ".claude" / "settings.json"
    before = claude_settings.stat().st_mtime if claude_settings.exists() else None
    append = "import sys; open(sys.argv[1], 'a').close(); print('opened')"  # never writes a byte
    got = shells(
        iso,
        store,
        sched,
        f"ls {REAL_HOME}",
        f'{sys.executable} -c "{append}" {claude_settings}',
        f'{sys.executable} -c "{append}" {REAL_HOME / ".codex" / "config.toml"}',
        f"ls {REAL_HOME / '.ssh'}",
        'echo "$HOME" && touch "$HOME/.state" && echo wrote',
    )
    for output, code in got[:4]:
        assert code != 0 and ("Operation not permitted" in output or "PermissionError" in output)
    home = sandbox.real(sandbox.engine_home(iso, "ada"))
    assert got[4] == (f"{home}\nwrote\n", 0)
    after = claude_settings.stat().st_mtime if claude_settings.exists() else None
    assert after == before


def test_writes_outside_its_paths_are_denied_even_where_the_user_can_write(
    iso: Config, host: tuple[Store, Scheduler, McpSocket], tmp_path_factory: pytest.TempPathFactory
) -> None:
    """B: /opt/homebrew and /usr/local are the user's, so a bot could swap `git` there and
    have it run outside the sandbox. C: the user's temp folders hold others' files."""
    store, sched, _ = host
    other = tmp_path_factory.mktemp("someone-else")
    (other / "theirs.txt").write_text("not yours")
    canary = f"bots-canary-{os.getpid()}"
    got = shells(
        iso,
        store,
        sched,
        f"touch /opt/homebrew/{canary}",
        f"touch /usr/local/{canary}",
        f"cat {other / 'theirs.txt'}",
        f"touch {other / 'new'}",
        f"touch /tmp/{canary}",
        'echo "$TMPDIR" && echo t > "$TMPDIR/x" && cat "$TMPDIR/x"',
    )
    for output, code in got[:5]:
        assert code != 0, output
    assert not Path(f"/opt/homebrew/{canary}").exists()
    assert not Path(f"/tmp/{canary}").exists()
    tmp_line, t = got[5][0].splitlines()
    run_tmp = Path(tmp_line)
    assert run_tmp.parent == iso.root / "run" / "tmp" and t == "t"
    assert not run_tmp.exists()  # removed when the run ended


# -- the shared scope: every bot folder, but each bot's prompt files its own ----


@pytest.fixture
def shared_host(cfg: Config) -> Iterator[tuple[Config, Store, Scheduler]]:
    sh = replace(cfg, isolation="seatbelt")  # isolation_scope = "shared" is the default
    store = open_store(sh)
    sock = McpSocket(sh, store, sandbox.socket_path(sh))
    sock.start()
    try:
        yield sh, store, Scheduler(sh, store, mcp_socket=sock)
    finally:
        sock.stop()


# Each canary is (command run in ada's folder, whether it may succeed). bob is another bot
# with notes/ and a .claude folder; carol is one whose MEMORY.md is missing. test_bwrap.py
# runs the same list. Renames are rename(2) itself: mv falls back to copy and delete.
RENAME = f"{sys.executable} -c 'import os, sys; os.rename(*sys.argv[1:])'"
SHARED_CANARIES = [
    ("echo n > ../bob/notes/n.md && cat ../bob/notes/n.md", True),
    ("echo s > ../../shared/s.md && cat ../../shared/s.md", True),
    ("cat ../bob/MEMORY.md", True),
    ("ln -s ../bob/notes nb && echo v > nb/v.md && cat nb/v.md", True),
    ("echo mine >> MEMORY.md && echo ok", True),
    ("echo x >> ../bob/MEMORY.md", False),
    (f"{RENAME} ../bob/MEMORY.md ../bob/notes/m.md", False),
    ("rm ../bob/MEMORY.md", False),
    ("ln -sf notes/n.md ../bob/MEMORY.md", False),
    ("ln ../bob/MEMORY.md ../bob/notes/hard.md && echo x > ../bob/notes/hard.md", False),
    ("echo x > ../bob/notes/../MEMORY.md", False),
    ("ln -s ../bob link && echo x > link/MEMORY.md", False),  # a linked ancestor
    ("echo x > ../carol/MEMORY.md", False),  # created where it is missing
    ("echo x > ../bob/CLAUDE.local.md", False),
    ("echo x > ../bob/.claude/settings.json", False),
    (f"{RENAME} ../bob/notes ../bob/n2", False),
    (f"{RENAME} ../bob ../../shared/bob", False),
    (f"{RENAME} ../../bots ../../shared/bots", False),
    ("mkdir ../dave", False),  # a bot folder planted before the bot exists
    ("echo x > ../bob/SOUL.md", False),
    ("echo x > SOUL.md", False),
    ("ln MEMORY.md notes/hard.md && echo x > notes/hard.md", False),  # its own, by another name
    ("cat ../../bots.sqlite3", False),
    ("ls ../../engine-home/bob", False),
]


def shared_setup(cfg: Config, store: Store) -> tuple[Path, Path]:
    for bot_id in ("bob", "carol"):
        store.create_bot(bot_id, bot_id.title(), engine="fake")
    bob, carol = botdir.scaffold(cfg, "bob"), botdir.scaffold(cfg, "carol")
    (bob / "MEMORY.md").write_text("bob's memory\n")
    (bob / ".claude").mkdir()
    (bob / ".claude" / "settings.json").write_text("{}\n")
    (carol / "MEMORY.md").unlink()
    (cfg.root / "engine-home" / "bob").mkdir(parents=True)
    return bob, carol


def assert_shared_canaries_held(cfg: Config, bob: Path, carol: Path) -> None:
    ada = cfg.bot_dir("ada")
    assert (bob / "MEMORY.md").read_text() == "bob's memory\n"
    assert (bob / "SOUL.md").read_text() == botdir.SOUL_TEMPLATE
    assert (ada / "SOUL.md").read_text() == botdir.SOUL_TEMPLATE
    assert (bob / ".claude" / "settings.json").read_text() == "{}\n"
    assert (bob / "notes" / "n.md").read_text() == "n\n" and (bob / "notes" / "v.md").exists()
    assert not (bob / "CLAUDE.local.md").exists() and not (carol / "MEMORY.md").exists()
    assert not (cfg.bots_dir / "dave").exists() and (bob / "notes").is_dir()
    assert (ada / "MEMORY.md").read_text().endswith("mine\n")
    assert (ada / "MEMORY.md").stat().st_nlink == 1
    assert (bob / "MEMORY.md").stat().st_nlink == 1


def test_shared_a_bot_works_in_its_neighbours_notes_but_not_their_prompt_files(
    shared_host: tuple[Config, Store, Scheduler],
) -> None:
    sh, store, sched = shared_host
    bob, carol = shared_setup(sh, store)
    got = shells(sh, store, sched, *(c for c, _ in SHARED_CANARIES))
    for (command, allowed), (output, code) in zip(SHARED_CANARIES, got, strict=True):
        if allowed:
            assert code == 0, (command, output)
        else:
            assert code != 0 and (DENIED in output or "PermissionError" in output), (
                command,
                output,
            )
    assert got[2][0] == "bob's memory\n"
    assert_shared_canaries_held(sh, bob, carol)


def test_shared_a_bot_added_during_a_run_is_covered_at_once(
    shared_host: tuple[Config, Store, Scheduler],
) -> None:
    """The profile's rules are patterns over bots/, not a list of the bots there now."""
    sh, _, _ = shared_host
    ada = botdir.scaffold(sh, "ada")
    dirs = sandbox.make_run_dirs(sh, "ada", 993, "synthetic-capability")
    profile = sandbox.profile(sh, "ada", dirs, sandbox.socket_path(sh))
    erin = botdir.scaffold(sh, "erin")  # after the profile was built
    try:
        done = subprocess.run(
            sandbox.wrap(
                ["/bin/sh", "-c", "echo x > ../erin/notes/n.md; echo x >> ../erin/MEMORY.md"],
                profile,
            ),
            cwd=ada,
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        dirs.remove()
    assert (erin / "notes" / "n.md").read_text() == "x\n"
    assert DENIED in done.stderr and (erin / "MEMORY.md").read_text() == botdir.MEMORY_TEMPLATE


def test_shared_extra_indirect_and_metadata_writes_are_closed(
    shared_host: tuple[Config, Store, Scheduler],
) -> None:
    sh, store, sched = shared_host
    bob, _ = shared_setup(sh, store)
    for name in (".codex", ".agents"):
        (bob / name).mkdir()
        (bob / name / "config").write_text("original\n")
    commands = [
        "echo x > ../bob/.codex/config",
        "echo x > ../bob/.agents/config",
        "chmod 777 ../bob/MEMORY.md",
        "ln -s ../MEMORY.md ../bob/notes/m && echo x > ../bob/notes/m",
        "ln -s ../.claude ../bob/notes/c && echo x > ../bob/notes/c/settings.json",
        "mkdir ../bob/new-top-level-folder",
    ]
    for command, (output, code) in zip(commands, shells(sh, store, sched, *commands), strict=True):
        assert code != 0, (command, output)
    assert (bob / "MEMORY.md").read_text() == "bob's memory\n"
    for name in (".codex", ".agents"):
        assert (bob / name / "config").read_text() == "original\n"


# -- the network and other processes -------------------------------------------


def test_loopback_and_other_unix_sockets_are_closed(
    iso: Config, host: tuple[Store, Scheduler, McpSocket], tmp_path: Path
) -> None:
    store, sched, _ = host
    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    unix = socket.socket(socket.AF_UNIX)
    unix_path = f"/private/tmp/bots-canary-{os.getpid()}.sock"
    unix.bind(unix_path)
    unix.listen()
    connect = (
        "import socket, sys; s = socket.socket(int(sys.argv[1])); "
        "s.connect(sys.argv[2] if sys.argv[1] == '1' else (sys.argv[2], int(sys.argv[3]))); "
        "print('connected')"
    )
    try:
        got = shells(
            iso,
            store,
            sched,
            f'{sys.executable} -c "{connect}" 2 127.0.0.1 {port}',
            f'{sys.executable} -c "{connect}" 2 localhost {port}',
            f'{sys.executable} -c "{connect}" 1 {unix_path}',
            f"curl -sS -m 3 http://127.0.0.1:{port}/",
        )
    finally:
        listener.close()
        unix.close()
        os.unlink(unix_path)
    for output, code in got:
        assert code != 0 and "connected" not in output, output


def test_it_cannot_signal_or_drive_processes_outside_its_run(
    iso: Config, host: tuple[Store, Scheduler, McpSocket]
) -> None:
    """D: launchd jobs, Apple Events, LaunchServices, cron, at and the keychain all hand work
    to something unsandboxed. Their programs are denied outright (a copy of one elsewhere is
    killed by the OS: its signature no longer matches its path); the mach denies behind them
    are tested on their own below."""
    store, sched, _ = host
    other = subprocess.Popen(["sleep", "30"])
    escaped = iso.bot_dir("ada") / "escaped"
    label = f"bots.canary.{os.getpid()}"
    try:
        got = shells(
            iso,
            store,
            sched,
            f"kill -0 {other.pid}",
            f"kill -0 {os.getpid()}",
            "sleep 5 & kill $! && echo own",
            f"launchctl submit -l {label} -- /usr/bin/touch {escaped}",
            "osascript -e 'return 1'",
            "open -g -j -b com.apple.TextEdit",
            "security find-generic-password -s 'Claude Code-credentials'",
            "crontab -l",
            "at now < /dev/null",
            'cp /usr/bin/security "$TMPDIR/sec" && "$TMPDIR/sec" list-keychains',
        )
        time.sleep(1)
        listed = subprocess.run(["launchctl", "list", label], capture_output=True, check=False)
    finally:
        other.kill()
        subprocess.run(["launchctl", "remove", label], capture_output=True, check=False)
    assert got[0][1] != 0 and got[1][1] != 0
    assert got[2] == ("own\n", 0)
    for output, code in got[3:]:
        assert code != 0, output
    assert not escaped.exists()
    assert listed.returncode != 0  # no job was submitted


@pytest.fixture
def no_exec_denies(iso: Config, tmp_path: Path) -> tuple[list[str], dict[str, str], Path]:
    """The real profile minus its program denies, as sandbox-exec argv: what a bot that
    brought its own launchctl, open or security would meet."""
    folder = botdir.scaffold(iso, "ada")
    dirs = sandbox.make_run_dirs(iso, "ada", 1, "cap")
    text = sandbox.profile(iso, "ada", dirs, sandbox.socket_path(iso))
    text = "".join(line for line in text.splitlines(True) if "(deny process-exec" not in line)
    env = {**os.environ, "TMPDIR": str(dirs.tmp)}
    return [sandbox.SANDBOX_EXEC, "-p", text], env, folder


def test_the_keychain_is_closed_without_the_exec_deny(
    no_exec_denies: tuple[list[str], dict[str, str], Path],
) -> None:
    """A keychain file the sandbox may read is still no use without the keychain daemon."""
    wrap, env, folder = no_exec_denies
    keychain = folder / "canary.keychain"
    subprocess.run(["security", "create-keychain", "-p", "pw", str(keychain)], check=True)
    try:
        add = ["add-generic-password", "-s", "bots-canary", "-a", "a", "-w", "kc-secret"]
        subprocess.run(["security", *add, str(keychain)], check=True)
        find = ["security", "find-generic-password", "-w", "-s", "bots-canary", str(keychain)]
        got = subprocess.run(
            [*wrap, *find], capture_output=True, text=True, env=env, timeout=30, check=False
        )
    finally:
        subprocess.run(
            ["security", "delete-keychain", str(keychain)], capture_output=True, check=False
        )
    assert got.returncode != 0 and "kc-secret" not in got.stdout


def test_launchservices_and_launchd_are_closed_without_the_exec_deny(
    no_exec_denies: tuple[list[str], dict[str, str], Path],
) -> None:
    wrap, env, folder = no_exec_denies
    running = (
        subprocess.run(["pgrep", "-x", "TextEdit"], capture_output=True, check=False).returncode
        == 0
    )
    label = f"bots.canary.{os.getpid()}"
    escaped = folder / "escaped"
    try:
        opened = subprocess.run(
            [*wrap, "open", "-g", "-j", "-b", "com.apple.TextEdit"],
            capture_output=True,
            env=env,
            timeout=30,
            check=False,
        )
        submit = ["launchctl", "submit", "-l", label, "--", "/usr/bin/touch", str(escaped)]
        subprocess.run([*wrap, *submit], capture_output=True, env=env, timeout=30, check=False)
        time.sleep(1)
        listed = subprocess.run(["launchctl", "list", label], capture_output=True, check=False)
    finally:
        subprocess.run(["launchctl", "remove", label], capture_output=True, check=False)
        if not running and subprocess.run(["pgrep", "-x", "TextEdit"], check=False).returncode == 0:
            subprocess.run(
                ["osascript", "-e", 'quit app "TextEdit"'], capture_output=True, check=False
            )
    assert opened.returncode != 0
    assert listed.returncode != 0 and not escaped.exists()


# -- MCP over the socket, and the capability ----------------------------------


def test_tools_reach_the_host_as_the_run_and_the_capability_dies_with_it(
    iso: Config, host: tuple[Store, Scheduler, McpSocket]
) -> None:
    """E: the capability is the only identity, cut from what the host logs, and useless once
    the run ends. botsmcp started by the bot with a forged BOT_ID has no database to open."""
    store, sched, sock = host
    store.create_bot("bob", "Bob", engine="fake")
    store.post_event(user_conversation("bob"), "bob's private chat")
    forged = (
        f"BOT_ID=bob BOTS_HOME={iso.root} {sys.executable} -c "
        "\"import botsmcp; botsmcp.store().send_to_user('bob', 'forged', 'n')\""
    )
    steps: list[dict[str, Any]] = [
        {"tool": "send_to_user", "args": {"body": "hello from the sandbox"}},
        {"tool": "read_conversation", "args": {"conversation_id": "user:bob"}},
        {"tool": "read_conversation", "args": {"conversation_id": "user:ada"}},
        {"shell": "cat ../../run/cap/$(basename $TMPDIR) > kept-cap; cat kept-cap"},
        {"shell": forged},
    ]
    run_id = run_turn(iso, store, sched, "ada", steps)
    results = tool_results(iso, run_id)
    assert results[0].startswith("sent (message")
    assert results[1].startswith("no conversation 'user:bob'")
    assert "hello from the sandbox" in results[2]
    (cap_echo, _), (_forged_out, forged_code) = outputs(iso, run_id)
    kept = (iso.bot_dir("ada") / "kept-cap").read_text().strip()
    assert len(kept) > 20 and kept not in cap_echo and "[redacted]" in cap_echo
    log_text = "".join(p.read_text() for p in (iso.logs_dir / str(run_id)).iterdir())
    assert kept not in log_text
    assert forged_code != 0
    msgs = store.messages(user_conversation("ada"), 10)
    assert [m.body for m in msgs if m.sender == "ada"] == ["hello from the sandbox"]
    assert not [m for m in store.messages(user_conversation("bob"), 10) if m.body == "forged"]
    stale = json.dumps({"cap": kept, "tool": "send_to_user", "args": {"body": "late"}})
    assert "error" in sock.handle(stale.encode())
    assert not (iso.root / "run" / "cap" / str(run_id)).exists()


def test_a_capability_is_valid_only_for_a_live_run_and_not_after_a_restart(
    iso: Config, store: Store
) -> None:
    store.create_bot("ada", "Ada", engine="fake")
    run_id = store.start_run("ada")
    sock = McpSocket(iso, store, sandbox.socket_path(iso))
    sock.grant("c" * 43, "ada", run_id)
    call = json.dumps({"cap": "c" * 43, "tool": "list_routines", "args": {}}).encode()
    assert sock.handle(call)["result"].startswith("no routines")
    assert "error" in sock.handle(call.replace(b"ccc", b"ccd", 1))
    assert "error" in McpSocket(iso, store, sandbox.socket_path(iso)).handle(call)  # restarted
    store.finish_run(run_id, "ok")
    assert "error" in sock.handle(call)
    for bad in (b"not json", b"[]", json.dumps({"cap": "c" * 43}).encode()):
        assert "error" in sock.handle(bad)


def test_the_socket_is_the_users_alone(iso: Config, store: Store) -> None:
    sock = McpSocket(iso, store, sandbox.socket_path(iso))
    sock.start()
    try:
        assert sock.path.stat().st_mode & 0o777 == 0o600
        assert sock.path.parent.stat().st_mode & 0o777 == 0o700
        client = socket.socket(socket.AF_UNIX)
        client.connect(str(sock.path))
        server_side, _ = socket.socketpair(socket.AF_UNIX)
        assert peer_uid(server_side) == os.getuid()
        client.close()
        server_side.close()
    finally:
        sock.stop()
    assert not sock.path.exists()


def test_isolated_mode_reads_only_the_bots_own_conversations(iso: Config, store: Store) -> None:
    """Default mode keeps team visibility; isolated mode is limited to the bot's own user
    chat, its pairs and its groups, and list_groups to the groups it is in."""
    import botsmcp

    for b in ("ada", "bob", "cy"):
        store.create_bot(b, b.title(), engine="fake")
    store.create_group("team", "Team", ["ada", "bob"])
    store.create_group("other", "Other", ["bob", "cy"])
    store.send_to_agent("bob", "cy", "between them", "n1")
    store.send_to_agent("bob", "ada", "to ada", "n2")
    botsmcp.attach(iso, store)
    try:
        open_ = botsmcp.Caller("ada", 0, isolated=False)
        closed = botsmcp.Caller("ada", 0, isolated=True)
        for conv in ("user:bob", "pair:bob:cy", "group:other"):
            assert not botsmcp.call(
                open_, "read_conversation", {"conversation_id": conv}
            ).startswith("no conversation")
            assert botsmcp.call(closed, "read_conversation", {"conversation_id": conv}) == (
                f"no conversation {conv!r}"
            )
        for conv in ("user:ada", "pair:ada:bob", "group:team"):
            assert not botsmcp.call(
                closed, "read_conversation", {"conversation_id": conv}
            ).startswith("no conversation")
        assert "other" in botsmcp.call(open_, "list_groups", {})
        groups = botsmcp.call(closed, "list_groups", {})
        assert "team" in groups and "other" not in groups
        assert botsmcp.call(closed, "nope", {}) == "rejected: no tool 'nope'"
        assert botsmcp.call(closed, "list_routines", {"x": 1}).startswith("rejected: ")
    finally:
        botsmcp.attach(None, None)  # type: ignore[arg-type]


# -- the host's own writes, start-up, engines -----------------------------------


def test_the_host_never_writes_or_reads_through_a_link_the_bot_left(
    iso: Config, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """The host is unsandboxed: AGENTS.md -> ~/.zshrc would have it write the bot's memory
    there, MEMORY.md -> a key copy the key into AGENTS.md."""
    outside = tmp_path_factory.mktemp("outside")
    target, key = outside / "zshrc", outside / "id_rsa"
    target.write_text("user's own\n")
    key.write_text("PRIVATE KEY\n")
    folder = botdir.scaffold(iso, "ada")
    (folder / "MEMORY.md").unlink()
    (folder / "MEMORY.md").symlink_to(key)
    (folder / "AGENTS.md").symlink_to(target)

    agents = botdir.write_agents_md(iso, "ada", "Ada")

    assert target.read_text() == "user's own\n"
    assert not agents.is_symlink() and "PRIVATE KEY" not in agents.read_text()


def test_a_sandboxed_bot_cannot_swap_its_own_folder_for_a_link(
    iso: Config, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """The host anchors its writes at the bot folder, so the folder itself must stay put."""
    import shlex

    outside = tmp_path_factory.mktemp("swap-outside")
    folder = botdir.scaffold(iso, "ada")
    dirs = sandbox.make_run_dirs(iso, "ada", 992, "synthetic-capability")
    profile = sandbox.profile(iso, "ada", dirs, sandbox.socket_path(iso))
    q = shlex.quote
    moved = iso.shared_dir / "ada-moved"
    try:
        subprocess.run(
            sandbox.wrap(
                [
                    "/bin/sh",
                    "-c",
                    f"mv {q(str(folder))} {q(str(moved))}; ln -s {q(str(outside))} {q(str(folder))}",
                ],
                profile,
            ),
            check=False,
            capture_output=True,
        )
    finally:
        dirs.remove()
    assert folder.is_dir() and not folder.is_symlink()
    assert not moved.exists()


def test_a_restarted_host_clears_what_its_dead_runs_left(iso: Config) -> None:
    dirs = sandbox.make_run_dirs(iso, "ada", 7, "old-capability")
    (dirs.tmp / "big").write_text("x")
    sandbox.clear_runs(iso)
    assert not dirs.tmp.exists() and not dirs.cap_file.exists()
    assert dirs.home.exists()  # the bot's engine home (its login) stays


def test_the_host_refuses_to_start_when_isolation_cant_be_had(
    iso: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox.check(iso)
    sandbox.check(replace(iso, isolation="none"))
    with pytest.raises(sandbox.Unavailable, match="needs Linux"):
        sandbox.check(replace(iso, isolation="bwrap"))
    monkeypatch.setattr(sandbox, "SANDBOX_EXEC", "/nonexistent/sandbox-exec")
    with pytest.raises(sandbox.Unavailable, match="needs macOS"):
        sandbox.check(iso)


def test_keys_from_engines_env_reach_the_engine_and_land_in_no_file(
    iso: Config, host: tuple[Store, Scheduler, McpSocket], monkeypatch: pytest.MonkeyPatch
) -> None:
    """engines.env's key gets in by env, the way an exported one does; the bot can't read
    the file, and the key is in nothing the turn leaves in its folder, engine home or logs."""
    store, sched, _ = host
    key = "sk-ant-engines-env-" + os.urandom(8).hex()
    secrets = iso.secrets_dir
    secrets.mkdir(exist_ok=True)
    (secrets / "engines.env").write_text(f"ANTHROPIC_API_KEY={key}\n")
    (secrets / "engines.env").chmod(0o600)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")  # restored after load() fills it
    engine_keys.load(iso)
    digest = 'printf %s "$ANTHROPIC_API_KEY" | '
    got = shells(
        iso,
        store,
        sched,
        f"{digest}sha256sum 2>/dev/null || {digest}shasum -a 256",
        "cat ../../secrets/engines.env",
    )
    assert got[0][0].split()[0] == hashlib.sha256(key.encode()).hexdigest()  # not in the script
    assert got[1][1] != 0 and DENIED in got[1][0]
    places = [iso.bot_dir("ada"), sandbox.engine_home(iso, "ada"), iso.logs_dir, iso.root / "run"]
    leaked = [
        p
        for place in places
        for p in place.rglob("*")
        if p.is_file() and key.encode() in p.read_bytes()
    ]
    assert leaked == []


def test_an_engine_without_an_isolation_profile_fails_its_turn(
    iso: Config, host: tuple[Store, Scheduler, McpSocket]
) -> None:
    store, sched, _ = host

    class NoIsolation:
        name = "plain"
        parse = FakeEngine.parse

        def command(self, prompt: str, cwd: Path, mcp: Any) -> tuple[list[str], dict[str, str]]:
            raise AssertionError("never run unisolated")

    sched.engines = lambda name, model: NoIsolation()
    run_id = run_turn(iso, store, sched, "ada", [])
    run = store.get_run(run_id)
    assert run is not None and run["status"] == "failed"
    assert "has no isolation profile" in (run["error"] or "")


def test_isolated_engines_take_the_prompt_on_stdin_and_their_own_home(tmp_path: Path) -> None:
    """Any process of the user can read another's argv (KERN_PROCARGS2), sandboxed or not,
    so the envelope never goes there; and each engine's state is under the bot's home."""
    from host.engines.base import McpServer

    mcp = McpServer("python", ["-m", "botsmcp"], {"BOTS_MCP_SOCKET": "/x"})
    for engine in (ClaudeEngine("claude"), CodexEngine("codex"), FakeEngine()):
        argv, _ = engine.command("THE ENVELOPE", tmp_path, mcp, stdin=True)
        assert not any("THE ENVELOPE" in a for a in argv)
        argv, _ = engine.command("THE ENVELOPE", tmp_path, mcp)
        assert "THE ENVELOPE" in argv
    assert ClaudeEngine("claude").isolation(tmp_path, tmp_path / "t").env == {
        "CLAUDE_CONFIG_DIR": str(tmp_path / ".claude"),
        "CLAUDE_CODE_TMPDIR": str(tmp_path / "t"),
    }
    codex = CodexEngine("codex").isolation(tmp_path, tmp_path / "t")
    assert codex.env == {"CODEX_HOME": str(tmp_path / ".codex")}


def test_inspect_finds_an_isolated_runs_transcript_in_the_bots_engine_home(iso: Config) -> None:
    from cli.main import transcript

    home = sandbox.engine_home(iso, "ada")
    claude = home / ".claude" / "projects" / "-x-bots-ada" / "s1.jsonl"
    codex = home / ".codex" / "sessions" / "2026" / "11" / "rollout-2026-11-02-s2.jsonl"
    for p in (claude, codex):
        p.parent.mkdir(parents=True)
        p.write_text("{}\n")
    assert transcript(iso, "claude", {"session_id": "s1", "bot_id": "ada"}) == str(claude)
    assert transcript(iso, "codex", {"session_id": "s2", "bot_id": "ada"}) == str(codex)


def test_the_profile_refuses_a_path_that_would_break_out_of_its_quotes(iso: Config) -> None:
    dirs = sandbox.RunDirs(Path("/x/tmp"), Path("/x/cap"), Path("/x/home"))
    with pytest.raises(ValueError, match="not allowed"):
        sandbox.profile(iso, "ada", dirs, Path('/x/") (allow file-write* (subpath "/'))


# -- the real engine CLIs under the profile, against a stand-in API --------------


def _sse(*events: dict[str, Any]) -> bytes:
    return b"".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode() for e in events)


def _reply(blocks: list[dict[str, Any]], deltas: list[dict[str, Any]], stop: str) -> bytes:
    message: dict[str, Any] = {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "stand-in",
        "content": [], "stop_reason": None, "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }  # fmt: skip
    events: list[dict[str, Any]] = [{"type": "message_start", "message": message}]
    for i, (block, delta) in enumerate(zip(blocks, deltas, strict=True)):
        events += [
            {"type": "content_block_start", "index": i, "content_block": block},
            {"type": "content_block_delta", "index": i, "delta": delta},
            {"type": "content_block_stop", "index": i},
        ]
    events += [
        {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
         "usage": {"output_tokens": 1}},
        {"type": "message_stop"},
    ]  # fmt: skip
    return _sse(*events)


class StandInApi(ThreadingHTTPServer):
    """The Anthropic Messages API, enough for one turn: the first call that offers the bots
    tools gets the turn's tool calls one by one, every other call a short text reply."""

    daemon_threads = True

    def __init__(self, body: str) -> None:
        self.paths: list[str] = []

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(h) -> None:
                raw = h.rfile.read(int(h.headers.get("Content-Length") or 0))
                self.paths.append(h.path)
                req = json.loads(raw or b"{}") if raw[:1] == b"{" else {}
                tools = {t.get("name") for t in req.get("tools") or []}
                answered = raw.decode(errors="replace").count('"tool_result"')
                # MCP tools are deferred: find the tool, call it, then end the turn.
                steps = [
                    ("ToolSearch", {"query": "select:mcp__bots__send_to_user", "max_results": 1}),
                    ("mcp__bots__send_to_user", {"body": body}),
                ]
                if "ToolSearch" in tools and answered < len(steps):
                    name, args = steps[answered]
                    out = _reply(
                        [
                            {
                                "type": "tool_use",
                                "id": f"toolu_{answered}",
                                "name": name,
                                "input": {},
                            }
                        ],
                        [{"type": "input_json_delta", "partial_json": json.dumps(args)}],
                        "tool_use",
                    )
                else:
                    out = _reply(
                        [{"type": "text", "text": ""}],
                        [{"type": "text_delta", "text": "done"}],
                        "end_turn",
                    )
                h.send_response(200)
                h.send_header("Content-Type", "text/event-stream")
                h.send_header("Content-Length", str(len(out)))
                h.end_headers()
                h.wfile.write(out)

            def do_GET(h) -> None:
                self.paths.append(h.path)
                h.send_response(404)
                h.send_header("Content-Length", "0")
                h.end_headers()

            def log_message(h, *args: Any) -> None:
                pass

        super().__init__(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.serve_forever, daemon=True).start()


@pytest.mark.skipif(not shutil.which("claude"), reason="needs the claude CLI")
def test_the_real_claude_cli_runs_isolated_and_its_tools_reach_the_host(
    iso: Config, host: tuple[Store, Scheduler, McpSocket], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real CLI under the real profile, logged in by env as isolated mode requires, its
    config in the bot's own engine home, its MCP calls over the host's socket. The API is a
    stand-in, so nothing is spent and no login is used. The sandbox closes every address of
    this machine (its LAN ones too), so the one change to the real profile is a last rule
    opening the stand-in's port."""
    store, sched, _ = host
    api = StandInApi("hello from claude in the sandbox")
    port = api.server_address[1]
    build = sandbox.profile
    hole = f'(allow network-outbound (remote ip "localhost:{port}"))\n'
    monkeypatch.setattr(sandbox, "profile", lambda *a, **k: build(*a, **k) + hole)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-stand-in-0000000000")
    monkeypatch.setenv("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1")
    store.create_bot("ada", "Ada", engine="claude", model="haiku")
    sched.engines = lambda name, model: ClaudeEngine(shutil.which("claude") or "", model)
    sched.cfg = replace(iso, idle_timeout=60)  # the CLI is silent while it starts
    try:
        run_id = run_turn(iso, store, sched, "ada", [])
    finally:
        api.shutdown()
        api.server_close()
    run = store.get_run(run_id)
    stderr = (iso.logs_dir / str(run_id) / "stderr.log").read_text()
    assert run is not None and run["status"] == "ok", (run, stderr[-2000:])
    sent = [m.body for m in store.messages(user_conversation("ada"), 10) if m.sender == "ada"]
    assert sent == ["hello from claude in the sandbox"]
    home = sandbox.engine_home(iso, "ada") / ".claude"
    assert home.is_dir() and any(home.iterdir())
    assert any(p.startswith("/v1/messages") for p in api.paths)


@pytest.mark.skipif(not shutil.which("codex"), reason="needs the codex CLI")
def test_the_real_codex_cli_starts_isolated_and_reaches_its_api(
    iso: Config, host: tuple[Store, Scheduler, McpSocket], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex under the real profile: it starts, reads its install, keeps its state in the
    bot's CODEX_HOME and sends the envelope (from stdin) to the API, here a stand-in that
    refuses it, so the turn fails without spending. Its tool calls are not played: its
    Responses stream is not stood in for here."""
    store, sched, _ = host
    seen: list[bytes] = []

    class Refuse(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(h) -> None:
            seen.append(h.rfile.read(int(h.headers.get("Content-Length") or 0)))
            h.send_response(400)
            h.send_header("Content-Length", "0")
            h.end_headers()

        def log_message(h, *args: Any) -> None:
            pass

    api = ThreadingHTTPServer(("127.0.0.1", 0), Refuse)
    threading.Thread(target=api.serve_forever, daemon=True).start()
    port = api.server_address[1]
    build = sandbox.profile
    hole = f'(allow network-outbound (remote ip "localhost:{port}"))\n'
    monkeypatch.setattr(sandbox, "profile", lambda *a, **k: build(*a, **k) + hole)
    monkeypatch.setenv("CODEX_API_KEY", "sk-stand-in-0000000000")
    provider = {
        "model_provider": '"standin"',
        "model_providers.standin.name": '"standin"',
        "model_providers.standin.base_url": f'"http://127.0.0.1:{port}/v1"',
        "model_providers.standin.env_key": '"CODEX_API_KEY"',
        "model_providers.standin.wire_api": '"responses"',
        "model_providers.standin.request_max_retries": "0",
        "model_providers.standin.stream_max_retries": "0",
    }

    class StandIn(CodexEngine):
        def command(self, *a: Any, **k: Any) -> tuple[list[str], dict[str, str]]:
            argv, env = super().command(*a, **k)
            flags = [x for key, value in provider.items() for x in ("-c", f"{key}={value}")]
            return [argv[0], argv[1], *flags, *argv[2:]], env

    store.create_bot("ada", "Ada", engine="codex")
    sched.engines = lambda name, model: StandIn(shutil.which("codex") or "", model)
    sched.cfg = replace(iso, idle_timeout=60)
    try:
        run_id = run_turn(iso, store, sched, "ada", [])
    finally:
        api.shutdown()
        api.server_close()
    stderr = (iso.logs_dir / str(run_id) / "stderr.log").read_text()
    assert seen, stderr[-2000:]
    assert b"This turn is a fresh session" in seen[0]  # the envelope, through stdin
    assert DENIED not in stderr
    home = sandbox.engine_home(iso, "ada") / ".codex"
    assert home.is_dir() and any(home.iterdir())
