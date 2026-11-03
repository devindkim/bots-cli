"""Isolated mode on Linux: bubblewrap in a pasta network namespace. The live tests need Linux."""

import hashlib
import json
import os
import shlex
import shutil
import socket
import struct
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from host import botdir, engine_keys, sandbox
from host.config import Config
from host.mcpsock import McpSocket, peer_uid
from host.scheduler import Scheduler
from host.store import Store, open_store, user_conversation


@pytest.fixture
def iso(cfg: Config) -> Config:
    return replace(cfg, isolation="bwrap", isolation_scope="bot")


ARITY = {"--tmpfs": 1, "--dev": 1, "--proc": 1, "--bind": 2, "--ro-bind": 2, "--symlink": 2}


def mounts(argv: list[str]) -> list[tuple[str, ...]]:
    """bwrap's mount options in order, each with its paths."""
    start = next(i for i, a in enumerate(argv) if Path(a).name == "bwrap")
    bwrap = argv[start + 1 :]
    bwrap = bwrap[: bwrap.index("--")]
    out, i = [], 0
    while i < len(bwrap):
        opt = bwrap[i]
        n = ARITY.get(opt, 0)
        if n:
            out.append((opt, *bwrap[i + 1 : i + 1 + n]))
        i += 1 + n
    return out


def built(iso: Config, reads: list[Path] | None = None) -> tuple[list[str], sandbox.RunDirs]:
    botdir.scaffold(iso, "ada")
    dirs = sandbox.make_run_dirs(iso, "ada", 7, "cap")
    argv = sandbox.bwrap_command(
        iso, "ada", dirs, sandbox.socket_path(iso), reads or [], ["engine", "-p"]
    )
    return argv, dirs


# -- the argv, anywhere -----------------------------------------------------------


def test_bwrap_hides_home_and_data_then_binds_the_runs_paths_back(iso: Config) -> None:
    argv, dirs = built(iso)
    assert Path(argv[0]).name == "pasta" and argv[-2:] == ["engine", "-p"]
    pasta = argv[: argv.index("--")]
    for off in (["-t", "none"], ["-u", "none"], ["-T", "none"], ["-U", "none"]):
        assert any(pasta[i : i + 2] == off for i in range(len(pasta))), off
    assert "--no-map-gw" in pasta
    m = mounts(argv)
    real = sandbox.real
    order = [x[1] for x in m]
    hidden = [x[1] for x in m if x[0] == "--tmpfs"]
    assert {str(real(Path.home())), str(real(iso.root)), "/tmp", "/run"} <= set(hidden)
    bot = str(real(iso.bot_dir("ada")))
    for path in (bot, str(real(iso.shared_dir)), str(real(dirs.tmp)), str(real(dirs.home))):
        assert ("--bind", path, path) in m
        assert all(order.index(h) < order.index(path) for h in hidden)  # tmpfs first
    soul = f"{bot}/SOUL.md"
    assert ("--ro-bind", soul, soul) in m
    assert order.index(soul) > order.index(bot)  # read-only on top of the folder
    assert ("--ro-bind", str(real(dirs.cap_file)), str(real(dirs.cap_file))) in m
    sock = str(real(sandbox.socket_path(iso)))
    assert ("--ro-bind", sock, sock) in m
    assert m[-1][2] == str(real(sandbox.RESOLV_CONF))
    for opt in ("--unshare-pid", "--die-with-parent", "--new-session", "--unshare-user"):
        assert opt in argv
    assert argv[argv.index("--seccomp") + 1] == "3"
    assert (dirs.tmp / ".seccomp.bpf").read_bytes() == sandbox.seccomp_filter()
    assert "--share-net" not in argv and "--unshare-net" not in argv  # pasta's namespace
    dirs.remove()


def test_bwrap_roots_the_run_in_the_system_folders_not_all_of_slash(
    iso: Config, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Read-only doesn't stop a connect() to a path socket, so a service's socket under /var/lib
    or /srv would be open to the run if / were mounted. Only the system folders are."""
    argv, dirs = built(iso)
    m = mounts(argv)
    roots = [x for x in m if x[0] in ("--ro-bind", "--symlink") and Path(x[-1]).parent == Path("/")]
    assert roots, m
    for opt, _, dest in roots:
        assert Path(dest) in sandbox.SYSTEM_ROOTS, (opt, dest)
        assert opt == ("--symlink" if Path(dest).is_symlink() else "--ro-bind")
    assert not any(x[-1] in ("/", "/var", "/opt", "/srv", "/mnt") for x in m), m
    if Path("/bin").is_symlink():  # merged /usr: the link stays a link
        assert ("--symlink", os.readlink("/bin"), "/bin") in m
    dirs.remove()


def test_bwrap_mounts_an_engine_link_target_at_the_links_path(
    iso: Config, tmp_path_factory: pytest.TempPathFactory
) -> None:
    install = tmp_path_factory.mktemp("install")
    (install / "claude.js").write_text("")
    link = tmp_path_factory.mktemp("bin") / "claude"
    link.symlink_to(install / "claude.js")
    argv, dirs = built(iso, [link, install])
    m = mounts(argv)
    target = str(sandbox.real(install / "claude.js"))
    assert ("--ro-bind", target, str(link)) in m
    assert ("--ro-bind", str(sandbox.real(install)), str(sandbox.real(install))) in m
    dirs.remove()


def test_bwrap_refuses_a_soul_that_is_a_link(
    iso: Config, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """bwrap would follow it: SOUL.md -> ~/.ssh/id_rsa would mount the key into the run."""
    key = tmp_path_factory.mktemp("outside") / "id_rsa"
    key.write_text("PRIVATE KEY\n")
    folder = botdir.scaffold(iso, "ada")
    (folder / "SOUL.md").unlink()
    (folder / "SOUL.md").symlink_to(key)
    dirs = sandbox.make_run_dirs(iso, "ada", 8, "cap")
    with pytest.raises(RuntimeError, match="not a plain file"):
        sandbox.bwrap_command(iso, "ada", dirs, sandbox.socket_path(iso), [], ["engine"])
    (folder / "SOUL.md").unlink()
    argv = sandbox.bwrap_command(iso, "ada", dirs, sandbox.socket_path(iso), [], ["engine"])
    assert (folder / "SOUL.md").read_text() == "" and not (folder / "SOUL.md").is_symlink()
    assert "--ro-bind" in argv
    dirs.remove()


def run_filter(prog: bytes, arch: int, nr: int) -> int:
    """Classic BPF, just the instructions seccomp_filter uses, over seccomp_data."""
    insns = [struct.unpack_from("=HBBI", prog, i) for i in range(0, len(prog), 8)]
    data, acc, pc = {0: nr, 4: arch}, 0, 0
    while True:
        code, jt, jf, k = insns[pc]
        if code == 0x20:
            acc = data[k]
        elif code == 0x06:
            return int(k)
        elif code in (0x15, 0x35):
            hit = acc == k if code == 0x15 else acc >= k
            pc += jt if hit else jf
        else:
            raise AssertionError(f"unexpected instruction {code:#x}")
        pc += 1


def test_the_seccomp_filter_refuses_the_keyring_syscalls_and_nothing_else() -> None:
    prog = sandbox.seccomp_filter()
    allow, eperm = 0x7FFF0000, 0x00050001
    for arch, numbers in sandbox.KEYRING_SYSCALLS.items():
        for nr in numbers:
            assert run_filter(prog, arch, nr) == eperm, (hex(arch), nr)
        for nr in (0, 1, 56, 63, 64, 221, 247, 251, 435):  # read, write, clone, openat...
            if nr not in numbers:
                assert run_filter(prog, arch, nr) == allow, (hex(arch), nr)
    assert run_filter(prog, 0xC000003E, 0x40000000 | 250) == eperm  # x32 keyctl
    assert run_filter(prog, 0xC00000F3, 0) == eperm  # riscv64: not known here


def test_the_runs_resolv_conf_skips_servers_on_the_hosts_loopback(tmp_path: Path) -> None:
    system, upstream = tmp_path / "resolv.conf", tmp_path / "upstream.conf"
    system.write_text("nameserver 127.0.0.53\noptions edns0 trust-ad\nsearch example.internal\n")
    upstream.write_text("# from DHCP\nnameserver 192.168.5.2\nnameserver ::1\n")
    assert sandbox.resolv_conf(system, upstream) == (
        "nameserver 192.168.5.2\noptions edns0 trust-ad\nsearch example.internal\n"
    )
    system.write_text("nameserver 1.1.1.1\nnameserver 127.0.0.1\n")
    assert sandbox.resolv_conf(system, upstream) == "nameserver 1.1.1.1\n"
    assert sandbox.resolv_conf(tmp_path / "none", tmp_path / "none") == ""


def test_bwrap_fails_closed_where_it_cant_run(iso: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    with pytest.raises(sandbox.Unavailable, match="needs Linux"):
        sandbox.check(iso)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(sandbox.Unavailable, match="needs bwrap and pasta on the PATH"):
        sandbox.check(iso)
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")

    def userns_blocked(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        assert argv[-1] == "true"  # one empty run through both
        err = "Couldn't write to /proc/self/uid_map: Operation not permitted\n"
        return subprocess.CompletedProcess(argv, 1, "", err)

    monkeypatch.setattr(subprocess, "run", userns_blocked)
    with pytest.raises(sandbox.Unavailable, match="uid_map.*apparmor_restrict_unprivileged_userns"):
        sandbox.check(iso)


def test_shared_mounts_other_bots_read_only_with_their_subfolders_writable(
    cfg: Config, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """The shared scope: another bot's folder read-only (its prompt files, its entries), each
    of its subfolders but the engines' writable on top, and a check of each before the
    engine starts. The run's own MEMORY.md is its own mount, so it can't be hard-linked."""
    sh = replace(cfg, isolation="bwrap")
    outside = tmp_path_factory.mktemp("outside")
    bob = botdir.scaffold(sh, "bob")
    for d in (".claude", ".codex", ".agents", "work"):
        (bob / d).mkdir()
    (bob / "linked").symlink_to(outside)
    (sh.bots_dir / "zed").symlink_to(outside)  # a bot folder that is a link: not mounted
    argv, dirs = built(sh)
    m = mounts(argv)
    order = [x[1] for x in m]
    real = sandbox.real
    rb = str(real(bob))
    assert ("--ro-bind", rb, rb) in m
    for sub in ("notes", "work"):
        assert ("--bind", f"{rb}/{sub}", f"{rb}/{sub}") in m
        assert order.index(f"{rb}/{sub}") > order.index(rb)
    assert not any(x[1].startswith((f"{rb}/.", f"{rb}/linked")) for x in m), m
    assert not any("zed" in x[-1] or str(real(outside)) in x[1] for x in m), m
    data = str(real(sh.root))
    assert order.index(data) < order.index(rb)  # bots/ itself stays the tmpfs
    ada = str(real(sh.bot_dir("ada")))
    assert ("--bind", f"{ada}/MEMORY.md", f"{ada}/MEMORY.md") in m
    assert order.index(f"{ada}/MEMORY.md") > order.index(ada)
    inner = argv[argv.index("--", argv.index("--seccomp")) + 1 :]
    assert inner[:3] == ["/bin/sh", "-c", sandbox.SAME_FOLDERS]
    st = (bob / "notes").stat()
    checks = inner[4 : inner.index("--")]
    assert [f"{rb}/notes", f"{st.st_dev}:{st.st_ino}"] == checks[:2] and len(checks) == 4
    assert inner[inner.index("--") + 1 :] == ["engine", "-p"]
    dirs.remove()


def test_bot_scope_mounts_no_other_bot(iso: Config) -> None:
    botdir.scaffold(iso, "bob")
    argv, dirs = built(iso)
    assert not any("bob" in x[-1] for x in mounts(argv))
    assert sandbox.SAME_FOLDERS not in argv
    dirs.remove()


def test_the_skill_names_how_the_bwrap_fence_shows(iso: Config) -> None:
    seatbelt = botdir.render_skill(replace(iso, isolation="seatbelt"), "ada")
    bwrap = botdir.render_skill(iso, "ada")
    assert '"Operation not permitted" hit the fence' in seatbelt
    assert '"Read-only file system"' in bwrap and "Operation not permitted" not in bwrap
    seatbelt_fence, bwrap_fence = botdir.BWRAP_FENCE
    assert bwrap.replace(bwrap_fence, seatbelt_fence) == seatbelt


def test_the_mcp_socket_knows_its_peers_uid_on_this_os() -> None:
    """Every isolated tool call is dropped if the host can't read the caller's uid."""
    a, b = socket.socketpair(socket.AF_UNIX)
    with a, b:
        assert peer_uid(a) == os.getuid()


# -- live: real turns under bwrap ---------------------------------------------------


def _bwrap_works() -> bool:
    try:
        sandbox.check_bwrap()
    except sandbox.Unavailable:
        return False
    return True


live = pytest.mark.skipif(
    not sys.platform.startswith("linux") or not _bwrap_works(),
    reason="needs Linux with bwrap and pasta able to make user namespaces",
)


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
    if store.get_bot(bot_id) is None:
        store.create_bot(bot_id, bot_id.title(), engine="fake")
    folder = botdir.scaffold(cfg, bot_id)
    (folder / "fake_script.json").write_text(json.dumps(steps))
    store.user_send(user_conversation(bot_id), "go")
    deadline = time.time() + 60
    while True:
        sched.tick()
        runs = store.list_runs(bot_id)
        if runs and runs[0]["status"] != "running" and sched._turns.get(bot_id) is None:
            return int(runs[0]["id"])
        assert time.time() < deadline, "the turn did not end"
        time.sleep(cfg.tick)


def outputs(cfg: Config, run_id: int) -> list[tuple[str, int]]:
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


def failed(result: tuple[str, int], secret: str = "") -> bool:
    output, code = result
    return code != 0 and (not secret or secret not in output)


@live
def test_live_the_data_folder_siblings_and_home_are_closed_and_its_own_paths_open(
    iso: Config, host: tuple[Store, Scheduler, McpSocket]
) -> None:
    store, sched, _ = host
    store.create_bot("bob", "Bob", engine="fake")
    bob = botdir.scaffold(iso, "bob")
    (bob / "SOUL.md").write_text("bob's soul")
    iso.secrets_dir.mkdir(exist_ok=True)
    (iso.secrets_dir / "token").write_text("secret-token")
    (iso.root / "engine-home" / "bob").mkdir(parents=True)
    (iso.root / "engine-home" / "bob" / "x").write_text("bob's login")
    caps = iso.root / "run" / "cap"
    caps.mkdir(parents=True, exist_ok=True)
    (caps / "999").write_text("someone else's capability")
    home_canary = Path.home() / f".bots-canary-{os.getpid()}"
    home_canary.write_text("user's own")
    db = iso.db_path
    try:
        got = shells(
            iso,
            store,
            sched,
            f"cat {db}",
            "cat ../../bots.sqlite3",
            f"cat {bob / 'SOUL.md'}",
            f"echo x > {bob / 'x.md'}",
            f"cat {iso.secrets_dir / 'token'}",
            f"cat {iso.root / 'engine-home' / 'bob' / 'x'}",
            f"cat {caps / '999'}",
            f"ln -s {db} link && cat link",
            f"cat {home_canary}",
            f"cat /proc/{os.getpid()}/environ",
            "echo mine > mine.md && cat mine.md",
            "echo shared > ../../shared/s.md && cat ../../shared/s.md",
            "echo remembered >> MEMORY.md && echo ok",
            'echo "$HOME" && touch "$HOME/.state" && echo wrote',
            "echo rules > SOUL.md",
            "mv SOUL.md SOUL.old",
            "rm -f SOUL.md",
            "ln SOUL.md hard",
        )
    finally:
        home_canary.unlink()
    sqlite = "SQLite format"
    secrets = [sqlite, sqlite, "bob's soul", "", "secret-token", "bob's login"]
    secrets += ["someone else's", sqlite, "user's own", "BOTS_HOME"]
    for result, secret in zip(got[:10], secrets, strict=True):
        assert failed(result, secret), result
    assert not (bob / "x.md").exists()
    assert got[10] == ("mine\n", 0)
    assert got[11] == ("shared\n", 0)
    assert got[12][1] == 0
    assert got[13] == (f"{sandbox.real(sandbox.engine_home(iso, 'ada'))}\nwrote\n", 0)
    for result in got[14:]:
        assert failed(result), result
    assert (iso.bot_dir("ada") / "SOUL.md").read_text() == botdir.SOUL_TEMPLATE


@live
def test_live_writes_outside_its_paths_land_nowhere(
    iso: Config, host: tuple[Store, Scheduler, McpSocket], tmp_path_factory: pytest.TempPathFactory
) -> None:
    store, sched, _ = host
    other = tmp_path_factory.mktemp("someone-else")
    (other / "theirs.txt").write_text("not yours")
    canary = f"bots-canary-{os.getpid()}"
    writable = Path.home() / ".local" / "bin"  # the user's, often on the PATH
    writable.mkdir(parents=True, exist_ok=True)
    got = shells(
        iso,
        store,
        sched,
        f"touch {writable / canary}",
        f"cat {other / 'theirs.txt'}",
        f"touch /usr/{canary}",
        f"touch /tmp/{canary} && echo private",  # its own empty /tmp
        'echo "$TMPDIR" && echo t > "$TMPDIR/x" && cat "$TMPDIR/x"',
    )
    assert failed(got[0]) and failed(got[1], "not yours") and failed(got[2])
    assert got[3] == ("private\n", 0)
    assert not (writable / canary).exists() and not Path(f"/tmp/{canary}").exists()
    tmp_line, t = got[4][0].splitlines()
    assert Path(tmp_line).parent == sandbox.real(iso.root / "run" / "tmp") and t == "t"
    assert not Path(tmp_line).exists()


@live
def test_live_this_machine_is_closed_and_the_internet_open(
    iso: Config, host: tuple[Store, Scheduler, McpSocket]
) -> None:
    """Loopback, the machine's own addresses, the gateway (pasta maps it to the host's
    loopback unless told not to), abstract unix sockets (X11, D-Bus) and path sockets
    outside its folder are all closed. DNS and the internet work."""
    store, sched, _ = host
    listener = socket.create_server(("0.0.0.0", 0))
    port = listener.getsockname()[1]
    abstract = socket.socket(socket.AF_UNIX)
    abstract.bind(f"\0bots-canary-{os.getpid()}")
    abstract.listen()
    path_sock = socket.socket(socket.AF_UNIX)
    path_name = f"/tmp/bots-canary-{os.getpid()}.sock"
    path_sock.bind(path_name)
    path_sock.listen()
    route = subprocess.run(
        ["ip", "-4", "route", "show", "default"], check=False, capture_output=True, text=True
    )
    gateway = route.stdout.split()[2]
    own = subprocess.run(
        ["hostname", "-I"], check=False, capture_output=True, text=True
    ).stdout.split()[0]
    connect = (
        "import socket, sys; a = sys.argv[1]; "
        "s = socket.socket(socket.AF_UNIX if a.startswith(('@', '/')) else socket.AF_INET); "
        "s.settimeout(3); "
        "s.connect(chr(0) + a[1:] if a[0] == '@' else a if a[0] == '/' else (a, int(sys.argv[2]))); "
        "print('connected')"
    )
    py = f'{sys.executable} -c "{connect}"'
    try:
        got = shells(
            iso,
            store,
            sched,
            f"{py} 127.0.0.1 {port}",
            f"{py} {own} {port}",
            f"{py} {gateway} {port}",
            f"{py} @bots-canary-{os.getpid()}",
            f"{py} {path_name}",
            "getent hosts example.com && echo resolved",
            f"{py} example.com 443",
        )
    finally:
        listener.close()
        abstract.close()
        path_sock.close()
        os.unlink(path_name)
    for result in got[:5]:
        assert failed(result, "connected"), result
    assert got[5][1] == 0 and "resolved" in got[5][0]
    assert got[6] == ("connected\n", 0)


@live
def test_live_it_cannot_reach_processes_outside_its_run_and_its_own_die_with_it(
    iso: Config, host: tuple[Store, Scheduler, McpSocket]
) -> None:
    store, sched, _ = host
    other = subprocess.Popen(["sleep", "30"])
    marker = f"bots-canary-{os.getpid()}"
    escaped = iso.bot_dir("ada") / "escaped"
    keyring = shutil.which("keyctl")
    if keyring:
        subprocess.run([keyring, "add", "user", marker, "keyring-secret", "@u"], check=True)
    try:
        got = shells(
            iso,
            store,
            sched,
            f"kill -0 {other.pid}",
            f"kill -0 {os.getpid()}",
            "sleep 5 & kill $! && echo own",
            f"systemd-run --user /usr/bin/touch {escaped}",
            f"echo '* * * * * touch {escaped}' | crontab -",
            f"keyctl print %user:{marker}" if keyring else "false",
            f"setsid nohup sh -c 'exec -a {marker} sleep 300' >/dev/null 2>&1 & echo left",
        )
        time.sleep(1)
        left = subprocess.run(["pgrep", "-f", marker], check=False, capture_output=True, text=True)
        cron = subprocess.run(["crontab", "-l"], check=False, capture_output=True, text=True)
    finally:
        other.kill()
        if keyring:
            subprocess.run([keyring, "purge", "user", marker], capture_output=True, check=False)
    assert failed(got[0]) and failed(got[1])
    assert got[2] == ("own\n", 0)
    for result in got[3:6]:
        assert failed(result, "keyring-secret"), result
    assert got[6] == ("left\n", 0)
    assert left.stdout.strip() == "", "a process outlived its run"
    assert marker not in cron.stdout and not escaped.exists()


@live
def test_live_tools_reach_the_host_and_a_forged_bot_id_does_not(
    iso: Config, host: tuple[Store, Scheduler, McpSocket]
) -> None:
    store, sched, _ = host
    store.create_bot("bob", "Bob", engine="fake")
    forged = (
        f"BOT_ID=bob BOTS_HOME={iso.root} {sys.executable} -c "
        "\"import botsmcp; botsmcp.store().send_to_user('bob', 'forged', 'n')\""
    )
    run_id = run_turn(
        iso,
        store,
        sched,
        "ada",
        [{"tool": "send_to_user", "args": {"body": "hello from bwrap"}}, {"shell": forged}],
    )
    assert [m.body for m in store.messages(user_conversation("ada"), 10) if m.sender == "ada"] == [
        "hello from bwrap"
    ]
    assert failed(outputs(iso, run_id)[0])
    assert not [m for m in store.messages(user_conversation("bob"), 10) if m.body == "forged"]


@live
def test_live_keys_from_engines_env_reach_the_engine_and_land_in_no_file(
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
    assert failed(got[1], key)
    places = [iso.bot_dir("ada"), sandbox.engine_home(iso, "ada"), iso.logs_dir, iso.root / "run"]
    leaked = [
        p
        for place in places
        for p in place.rglob("*")
        if p.is_file() and key.encode() in p.read_bytes()
    ]
    assert leaked == []


@live
def test_live_a_bot_cannot_move_its_folder_or_soul_away(iso: Config) -> None:
    """Both are mount points in the run, so a rename fails instead of leaving a link that
    would steer the host's next write (botdir.write_own anchors at the folder)."""
    folder = botdir.scaffold(iso, "ada")
    dirs = sandbox.make_run_dirs(iso, "ada", 993, "synthetic-capability")
    sock = Path(sandbox.socket_path(iso))
    sock.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    sock.touch()
    moved = iso.shared_dir / "ada-moved"
    before = folder.stat().st_ino
    q = shlex.quote
    script = f"mv {q(str(folder))} {q(str(moved))}; mv {q(str(folder / 'SOUL.md'))} x"
    try:
        argv = sandbox.bwrap_command(iso, "ada", dirs, sock, [], ["/bin/sh", "-c", script])
        subprocess.run(argv, check=False, capture_output=True, cwd=folder)
    finally:
        dirs.remove()
    # mv falls back to copying a folder it can't rename: the copy is harmless, in shared/.
    assert folder.is_dir() and not folder.is_symlink() and folder.stat().st_ino == before
    assert (folder / "SOUL.md").read_text() == botdir.SOUL_TEMPLATE


@live
def test_live_a_path_socket_outside_hidden_roots_is_closed(
    iso: Config,
    host: tuple[Store, Scheduler, McpSocket],
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Run with --basetemp outside /home, /tmp, /var/tmp and /run to cover the
    read-only root mount, rather than relying on tmpfs to hide the socket."""
    store, sched, _ = host
    path = tmp_path_factory.mktemp("socket") / "service.sock"
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(path))
    listener.listen()
    script = (
        "import socket,sys; s=socket.socket(socket.AF_UNIX); "
        "s.settimeout(3); s.connect(sys.argv[1]); print('connected')"
    )
    try:
        got = shells(
            iso,
            store,
            sched,
            f"{shlex.quote(sys.executable)} -c {shlex.quote(script)} {shlex.quote(str(path))}",
        )
    finally:
        listener.close()
        path.unlink()
    assert failed(got[0], "connected"), got[0]


@live
def test_live_a_path_socket_in_var_is_closed_under_the_default_basetemp(
    iso: Config, host: tuple[Store, Scheduler, McpSocket]
) -> None:
    """The same channel without a root-made folder: Ubuntu's /var/crash is writable by
    everyone and outside every tmpfs the run gets."""
    crash = Path("/var/crash")
    if not (crash.is_dir() and os.access(crash, os.W_OK)):
        pytest.skip("no writable /var/crash")
    store, sched, _ = host
    path = crash / f"bots-canary-{os.getpid()}.sock"
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(path))
    listener.listen()
    script = (
        "import socket,sys; s=socket.socket(socket.AF_UNIX); "
        "s.settimeout(3); s.connect(sys.argv[1]); print('connected')"
    )
    try:
        got = shells(
            iso,
            store,
            sched,
            f"{shlex.quote(sys.executable)} -c {shlex.quote(script)} {shlex.quote(str(path))}",
            f"ls {crash} && echo listed",
        )
    finally:
        listener.close()
        path.unlink()
    assert failed(got[0], "connected"), got[0]
    assert failed(got[1], "listed"), got[1]


# -- live: the shared scope ---------------------------------------------------------

# test_sandbox.py's SHARED_CANARIES, here without macOS's error text: (command run in ada's
# folder, whether it may succeed). Renames are rename(2) itself: mv falls back to copy and delete.
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
    ("ln -s ../bob link && echo x > link/MEMORY.md", False),
    ("echo x > ../carol/MEMORY.md", False),
    ("echo x > ../bob/CLAUDE.local.md", False),
    ("echo x > ../bob/.claude/settings.json", False),
    (f"{RENAME} ../bob/notes ../bob/n2", False),
    (f"{RENAME} ../bob ../../shared/bob", False),
    (f"{RENAME} ../../bots ../../shared/bots", False),
    ("mkdir ../dave && echo in-the-sandbox-only", True),  # bots/ is the run's own tmpfs
    ("echo x > ../bob/SOUL.md", False),
    ("echo x > SOUL.md", False),
    ("ln MEMORY.md notes/hard.md && echo x > notes/hard.md", False),
    ("cat ../../bots.sqlite3", False),
    ("ls ../../engine-home/bob", False),
]


@pytest.fixture
def shared_host(cfg: Config) -> Iterator[tuple[Config, Store, Scheduler]]:
    sh = replace(cfg, isolation="bwrap")
    store = open_store(sh)
    sock = McpSocket(sh, store, sandbox.socket_path(sh))
    sock.start()
    try:
        yield sh, store, Scheduler(sh, store, mcp_socket=sock)
    finally:
        sock.stop()


@live
def test_live_shared_a_bot_works_in_its_neighbours_notes_but_not_their_prompt_files(
    shared_host: tuple[Config, Store, Scheduler],
) -> None:
    sh, store, sched = shared_host
    for bot_id in ("bob", "carol"):
        store.create_bot(bot_id, bot_id.title(), engine="fake")
    bob, carol = botdir.scaffold(sh, "bob"), botdir.scaffold(sh, "carol")
    (bob / "MEMORY.md").write_text("bob's memory\n")
    (bob / ".claude").mkdir()
    (bob / ".claude" / "settings.json").write_text("{}\n")
    (carol / "MEMORY.md").unlink()
    (sh.root / "engine-home" / "bob").mkdir(parents=True)
    got = shells(sh, store, sched, *(c for c, _ in SHARED_CANARIES))
    for (command, allowed), (output, code) in zip(SHARED_CANARIES, got, strict=True):
        assert (code == 0) == allowed, (command, output)
    assert got[2][0] == "bob's memory\n"
    ada = sh.bot_dir("ada")
    assert (bob / "MEMORY.md").read_text() == "bob's memory\n"
    assert (bob / "SOUL.md").read_text() == botdir.SOUL_TEMPLATE
    assert (ada / "SOUL.md").read_text() == botdir.SOUL_TEMPLATE
    assert (bob / ".claude" / "settings.json").read_text() == "{}\n"
    assert (bob / "notes" / "n.md").read_text() == "n\n" and (bob / "notes" / "v.md").exists()
    assert not (bob / "CLAUDE.local.md").exists() and not (carol / "MEMORY.md").exists()
    assert not (sh.bots_dir / "dave").exists() and (bob / "notes").is_dir()
    assert (ada / "MEMORY.md").read_text().endswith("mine\n")
    assert (ada / "MEMORY.md").stat().st_nlink == 1
    assert (bob / "MEMORY.md").stat().st_nlink == 1
    assert not (sh.shared_dir / "bob").exists() and not (sh.shared_dir / "bots").exists()


@live
def test_live_shared_a_subfolder_swapped_for_a_link_stops_the_run(cfg: Config) -> None:
    """bob may be running, and swap notes/ for a link between the host's listing and bwrap's
    mount, which follows links: notes -> .claude would mount bob's .claude writable. Here the
    swap comes after the argv is built."""
    sh = replace(cfg, isolation="bwrap")
    bob = botdir.scaffold(sh, "bob")
    (bob / ".claude").mkdir()
    ada = botdir.scaffold(sh, "ada")
    dirs = sandbox.make_run_dirs(sh, "ada", 8, "cap")
    sandbox.socket_path(sh).parent.mkdir(parents=True, exist_ok=True)
    sandbox.socket_path(sh).touch()
    probe = ["/bin/sh", "-c", f"echo x > {bob / 'notes' / 'settings.json'} && echo wrote"]
    argv = sandbox.bwrap_command(sh, "ada", dirs, sandbox.socket_path(sh), [], probe)
    try:
        (bob / "notes").rename(bob / "notes.old")
        (bob / "notes").symlink_to(".claude")
        done = subprocess.run(argv, cwd=ada, capture_output=True, text=True, check=False)
        assert done.returncode == 126 and "changed as the sandbox was built" in done.stderr
        assert list((bob / ".claude").iterdir()) == []
        (bob / "notes").unlink()
        (bob / "notes.old").rename(bob / "notes")
        argv = sandbox.bwrap_command(sh, "ada", dirs, sandbox.socket_path(sh), [], probe)
        done = subprocess.run(argv, cwd=ada, capture_output=True, text=True, check=False)
        assert done.stdout == "wrote\n", done.stderr
    finally:
        dirs.remove()
