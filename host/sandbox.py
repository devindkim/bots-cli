"""Isolated mode: every engine run inside an OS sandbox, Seatbelt on macOS
(`isolation = "seatbelt"`) or bubblewrap on Linux (`isolation = "bwrap"`, end of this file).

The whole engine process tree runs under `sandbox-exec -p <profile>`: the CLI, its shells,
its file tools and the botsmcp it starts. The profile is built per run. Reads start from
allow-default (system libraries are too many to list), then the data folder, $HOME and the
user's temp folders are denied and the run's own paths allowed back. Writes are denied
everywhere and allowed back only where the bot works. Loopback, other unix sockets, other
processes' signals, Apple Events and LaunchServices are closed, so the bot can't ask an
unsandboxed process to act for it. botsmcp inside the sandbox can't open the database; it
reaches the host's tool functions over a unix socket with the run's capability
(host/mcpsock.py). The canaries are in tests/test_sandbox.py and tests/test_bwrap.py.

A later rule wins in SBPL, so each deny comes before the allows it makes room for.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from host import botdir
from host.config import REPO, Config

SANDBOX_EXEC = "/usr/bin/sandbox-exec"
# Run as found on the PATH when the host starts (check); config.toml can't move them, since
# a bot that could write either would run outside its sandbox.
BWRAP, PASTA = "bwrap", "pasta"

# Programs whose whole job is to have something outside the sandbox act: launchd jobs,
# Apple Events, LaunchServices, the keychain, cron and at. The mach and Apple Event denies
# below are the real fence; denying the binaries makes the refusal plain.
DENIED_EXEC = (
    "/bin/launchctl",
    "/usr/bin/osascript",
    "/usr/bin/open",
    "/usr/bin/security",
    "/usr/bin/crontab",
    "/usr/bin/at",
    "/usr/bin/batch",
)
# LaunchServices (open an app or a script outside the sandbox) and the keychain daemon.
# Engines log in through env (an API key or a setup token), so they need neither.
DENIED_MACH = (
    ("global-name", "com.apple.coreservices.launchservicesd"),
    ("global-name-prefix", "com.apple.lsd."),
    ("global-name", "com.apple.SecurityServer"),
    ("global-name", "com.apple.securityd.xpc"),
)
DNS_SOCKET = "/private/var/run/mDNSResponder"
# Devices any process writes; everything else under /dev stays read-only.
DEVICES = ("/dev/null", "/dev/zero", "/dev/dtracehelper", "/dev/stdout", "/dev/stderr")


class Unavailable(RuntimeError):
    """Isolation is on but can't be had here: the host refuses to start."""


def check(cfg: Config) -> None:
    """Fail closed: a host told to isolate never runs a turn unisolated."""
    if cfg.isolation == "none":
        return
    if real(cfg.root).is_relative_to(real(REPO)):
        raise Unavailable(f"{cfg.root} is inside the source checkout, which isolated bots can read")
    if cfg.isolation == "bwrap":
        return check_bwrap()
    if sys.platform != "darwin" or not os.access(SANDBOX_EXEC, os.X_OK):
        raise Unavailable(f'isolation = "seatbelt" needs macOS and {SANDBOX_EXEC}')


def isolated(cfg: Config) -> bool:
    return cfg.isolation != "none"


def shared(cfg: Config) -> bool:
    """Isolated, with every bot folder open to every bot (isolation_scope = "shared")."""
    return isolated(cfg) and cfg.isolation_scope == "shared"


# In the shared scope a bot works in other bots' folders too, but only inside their
# subfolders. The files at the top of a bot folder are what its prompt is built from
# (SOUL.md, MEMORY.md, AGENTS.md, CLAUDE.md, CLAUDE.local.md), so only its owner writes them
# (SOUL.md: no bot), and so are the engines' project folders, whose settings run hooks.
ENGINE_DIRS = (".claude", ".codex", ".agents")


def socket_path(cfg: Config) -> Path:
    """The host's MCP socket. Under the data folder when the path fits a unix socket name
    (104 bytes on macOS); else in a private folder in /tmp named for the data folder."""
    path = real(cfg.root) / "run" / "mcp.sock"
    if len(str(path).encode()) < 100:
        return path
    tag = hashlib.sha256(str(real(cfg.root)).encode()).hexdigest()[:12]
    return real("/tmp") / f"bots-{os.getuid()}-{tag}" / "mcp.sock"


def engine_home(cfg: Config, bot_id: str) -> Path:
    """The bot's own HOME for its engine: its CLAUDE_CONFIG_DIR, CODEX_HOME, caches and
    login. Never the user's ~/.claude or ~/.codex, which steer every run on the machine."""
    return cfg.root / "engine-home" / bot_id


def real(path: Path | str) -> Path:
    """Seatbelt judges resolved paths (/tmp is /private/tmp), so every rule uses one."""
    return Path(os.path.realpath(path))


@dataclass(frozen=True)
class RunDirs:
    """What the host makes for one isolated run, and removes after it."""

    tmp: Path  # the run's TMPDIR: <data>/run/tmp/<run id>
    cap_file: Path  # the capability, readable by this run only: <data>/run/cap/<run id>
    home: Path  # the bot's engine home

    def remove(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.cap_file.unlink(missing_ok=True)


def clear_runs(cfg: Config) -> None:
    """At start: what a host that died left of its runs. Its capabilities died with it, so
    the files are inert, but the temp folders can be large."""
    for name in ("tmp", "cap"):
        shutil.rmtree(cfg.root / "run" / name, ignore_errors=True)


def make_run_dirs(cfg: Config, bot_id: str, run_id: int, cap: str) -> RunDirs:
    run = cfg.root / "run"
    tmp, caps, home = run / "tmp" / str(run_id), run / "cap", engine_home(cfg, bot_id)
    for d in (run, run / "tmp", caps, home.parent):
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
    shutil.rmtree(tmp, ignore_errors=True)  # a run id left over from a crashed host
    tmp.mkdir(mode=0o700)
    home.mkdir(mode=0o700, exist_ok=True)
    cap_file = caps / str(run_id)
    cap_file.unlink(missing_ok=True)
    fd = os.open(cap_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(cap)
    return RunDirs(tmp, cap_file, home)


def new_capability() -> str:
    return secrets.token_urlsafe(32)


def python_reads() -> list[Path]:
    """What botsmcp needs to start inside the sandbox: this interpreter, its venv, the repo."""
    paths = {real(sys.prefix), real(sys.base_prefix), real(REPO)}
    paths.add(real(sys.executable).parent.parent)
    return sorted(paths)


def user_temp_root() -> Path:
    """/private/var/folders/xx/yyyy: the user's T (temp), C (caches) and 0, shared by every
    process the user runs. Asked of the OS, since the host's own TMPDIR may be anything."""
    try:
        temp = os.confstr("CS_DARWIN_USER_TEMP_DIR")
    except (ValueError, OSError):
        temp = None
    return real(temp or tempfile.gettempdir()).parent


def _q(path: Path | str) -> str:
    s = str(path)
    if '"' in s or "\\" in s or "\n" in s:
        raise ValueError(f"path not allowed in a sandbox profile: {s!r}")
    return f'"{s}"'


def _subpaths(paths: list[Path]) -> str:
    return " ".join(f"(subpath {_q(p)})" for p in paths)


def _regex(path: Path) -> str:
    """`path` as the start of an SBPL regex, its special characters escaped."""
    s = str(_q(path))[1:-1]
    return "".join("\\" + c if c in ".^$*+?()[]{}|" else c for c in s)


def _shared_rules(bots: Path, bot: Path) -> list[str]:
    """Writes in the shared scope: every bot folder, then the top of each closed again (the
    folder itself and every entry in it: its files, and its subfolders' own names, so none
    can be renamed, removed or replaced by a link) with the engines' folders, then the run's
    own folder opened back. A hard link would give a top-level file a second name inside a
    subfolder, so none can be made, by its owner either."""
    top, engine = _regex(bots), "|".join(d.replace(".", "\\.") for d in ENGINE_DIRS)
    return [
        f"(allow file-write* (subpath {_q(bots)}))",
        (
            f'(deny file-write* (literal {_q(bots)}) (regex #"^{top}/[^/]+$")'
            f' (regex #"^{top}/[^/]+/[^/]+$") (regex #"^{top}/[^/]+/({engine})/"))'
        ),
        f"(allow file-write* (subpath {_q(bot)}))",
        f'(deny file-link (regex #"^{top}/[^/]+/[^/]+$"))',
    ]


def _ancestors(paths: list[Path]) -> list[Path]:
    """Every folder above an allowed path, for stat only: runtimes lstat each component
    of a path (realpath), and a folder's metadata says nothing about what's in it."""
    out: set[Path] = set()
    for p in paths:
        out.update(p.parents)
    return sorted(out)


def profile(
    cfg: Config,
    bot_id: str,
    dirs: RunDirs,
    socket: Path,
    reads: list[Path] | None = None,
) -> str:
    """The Seatbelt profile for one run of `bot_id`."""
    home = real(Path.home())
    data, bot, shared = real(cfg.root), real(cfg.bot_dir(bot_id)), real(cfg.shared_dir)
    tmp, engine, cap = real(dirs.tmp), real(dirs.home), real(dirs.cap_file)
    user_tmp = user_temp_root()
    extra = [*(reads or ()), *(Path(p).expanduser() for p in cfg.isolation_read)]
    # A link (~/.local/bin/claude) is judged as its target, but reading the link itself is
    # judged as the link: both are allowed.
    readable = sorted({*python_reads(), *(real(p) for p in extra), *(p.absolute() for p in extra)})
    writable = [bot, shared, tmp, engine]
    bots = real(cfg.bots_dir)
    scope = _shared_rules(bots, bot) if cfg.isolation_scope == "shared" else []
    reachable = [bots, *writable[1:]] if scope else writable
    denied_reads = sorted({data, home, user_tmp, Path("/private/tmp")})
    lines = [
        "(version 1)",
        "(allow default)",
        # Reads: the data folder, $HOME and the temp folders are closed, the run's paths open.
        f"(deny file-read* {_subpaths(denied_reads)})",
        f"(allow file-read* {_subpaths([*reachable, *readable])} (literal {_q(cap)}))",
        "(allow file-read-metadata "
        + " ".join(f"(literal {_q(p)})" for p in _ancestors([*reachable, *readable, cap]))
        + ")",
        # Writes: nowhere, then the bot's working paths. Never its SOUL.md.
        "(deny file-write*)",
        f"(allow file-write* {_subpaths(writable)} "
        + " ".join(f"(literal {_q(d)})" for d in DEVICES)
        + ' (regex #"^/dev/tty") (regex #"^/dev/fd/"))',
        *scope,
        # No bot's SOUL.md, its own included (the shared scope's rules already close others').
        f"(deny file-write* (literal {_q(bot / 'SOUL.md')}))",
        # Nor the working folders themselves: a bot that moved its folder away and left a
        # link there would send the host's writes into it (botdir.write_own anchors there).
        "(deny file-write* " + " ".join(f"(literal {_q(d)})" for d in writable) + ")",
        # Network: no loopback (local services), no unix socket but the
        # host's MCP socket and DNS. The internet and the LAN stay open.
        '(deny network-outbound (remote ip "localhost:*"))',
        "(deny network-outbound (remote unix-socket))",
        (
            f"(allow network-outbound (remote unix-socket (path-literal {_q(socket)}))"
            f" (remote unix-socket (path-literal {_q(DNS_SOCKET)})))"
        ),
        # Other processes: no signals outside this run, no asking anything else to act.
        "(deny signal)",
        "(allow signal (target same-sandbox))",
        "(deny appleevent-send)",
        "(deny mach-lookup " + " ".join(f"({kind} {_q(name)})" for kind, name in DENIED_MACH) + ")",
        "(deny process-exec " + " ".join(f"(literal {_q(p)})" for p in DENIED_EXEC) + ")",
    ]
    return "\n".join(lines) + "\n"


def wrap(argv: list[str], profile_text: str) -> list[str]:
    return [SANDBOX_EXEC, "-p", profile_text, *argv]


def command(
    cfg: Config, bot_id: str, dirs: RunDirs, socket: Path, reads: list[Path], argv: list[str]
) -> list[str]:
    """`argv` run inside this host's sandbox, for one run of `bot_id`."""
    if cfg.isolation == "bwrap":
        return bwrap_command(cfg, bot_id, dirs, socket, reads, argv)
    return wrap(argv, profile(cfg, bot_id, dirs, socket, reads))


# -- Linux: bubblewrap in a pasta network namespace -----------------------------
#
# bwrap gives the run its own mount, pid, ipc and uts namespaces. Its root is empty but for
# the system folders, read-only, and empty tmpfs at $HOME, the data folder, /tmp, /var/tmp
# and /run; then the run's own paths are bound back. Read-only doesn't stop a connect() to a
# path socket, and services keep theirs anywhere (/run, /var/lib, a home), so the root is a
# list of folders no service keeps a socket in rather than all of / with holes. Other
# processes are invisible, so neither signals nor /proc reach them. pasta gives it its own
# network namespace with the internet and the LAN but not this machine: its loopback is its
# own (no port forwarding in either direction, no gateway mapped to the host), and abstract
# unix sockets (X11, D-Bus) are per namespace.
# Both programs need unprivileged user namespaces; check_bwrap probes that at start.

# Both directions of port forwarding off, and the gateway not mapped to the host's
# loopback: the run's 127.0.0.1 is its own and the host's is unreachable.
PASTA_ARGS = (
    *("--config-net", "--quiet"),
    *("-t", "none", "-u", "none", "-T", "none", "-U", "none", "--no-map-gw"),
)
BWRAP_ARGS = (
    "--die-with-parent",
    "--new-session",  # no TIOCSTI into the host's terminal
    "--unshare-user",
    "--unshare-pid",
    "--unshare-ipc",
    "--unshare-uts",
    "--unshare-cgroup-try",
)
# The run's whole root, beside its own paths. Merged-/usr distros make the top-level ones
# links into /usr, and they stay links. /opt, /srv, /var, /mnt, /media and /home are not
# here: an engine installed in one is mounted by its install paths or isolation_read.
SYSTEM_ROOTS = tuple(
    Path(p)
    for p in ("/usr", "/etc", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32", "/sys")
)
RESOLV_CONF = Path("/etc/resolv.conf")
# systemd-resolved's list of the real servers, when /etc/resolv.conf names its stub.
RESOLVED_UPSTREAM = Path("/run/systemd/resolve/resolv.conf")
UBUNTU_HINT = (
    "; on Ubuntu 23.10 and later AppArmor blocks this by default: "
    "`sudo sysctl kernel.apparmor_restrict_unprivileged_userns=0`, kept in /etc/sysctl.d/"
)


# The kernel keyring is the user's across namespaces: a run could read a key any of the
# user's processes stored (measured: `keyctl print %user:x` from inside bwrap). A seccomp
# filter refuses its three syscalls, as Flatpak's does; any other architecture than these
# is refused everything, since its numbers aren't known here.
KEYRING_SYSCALLS = {
    0xC000003E: (248, 249, 250),  # x86_64: add_key, request_key, keyctl
    0x40000003: (286, 287, 288),  # i386, which an x86_64 kernel also runs
    0xC00000B7: (217, 218, 219),  # aarch64
    0x40000028: (309, 310, 311),  # arm, which an aarch64 kernel may also run
}
_X32_BIT = 0x40000000  # x86_64's x32 ABI: syscall numbers with this bit set
_LD_ABS, _JEQ, _JGE, _RET = 0x20, 0x15, 0x35, 0x06
_ALLOW, _EPERM = 0x7FFF0000, 0x00050000 | 1  # SECCOMP_RET_ALLOW, SECCOMP_RET_ERRNO | EPERM


def seccomp_filter() -> bytes:
    """A classic BPF program for bwrap --seccomp: EPERM for the keyring syscalls."""
    prog: list[tuple[int, int, int, int]] = [(_LD_ABS, 0, 0, 4)]  # seccomp_data.arch
    for arch, numbers in KEYRING_SYSCALLS.items():
        body: list[tuple[int, int, int, int]] = [(_LD_ABS, 0, 0, 0)]  # seccomp_data.nr
        tests = [(_JGE, _X32_BIT)] if arch == 0xC000003E else []
        tests += [(_JEQ, n) for n in numbers]
        size = 1 + len(tests) + 2
        for i, (op, k) in enumerate(tests):
            body.append((op, size - 1 - (1 + i) - 1, 0, k))  # to the EPERM at the end
        body += [(_RET, 0, 0, _ALLOW), (_RET, 0, 0, _EPERM)]
        prog.append((_JEQ, 0, len(body), arch))
        prog += body
        prog.append((_LD_ABS, 0, 0, 4))  # the next block reads the arch again
    prog[-1] = (_RET, 0, 0, _EPERM)  # no block matched: an architecture not listed
    return b"".join(struct.pack("=HBBI", *insn) for insn in prog)


def _with_filter(filter_path: Path, bwrap_argv: list[str]) -> list[str]:
    """bwrap reads its filter from a descriptor, and pasta closes the ones it doesn't
    know, so a shell inside pasta's namespace opens the file as fd 3 and execs bwrap."""
    shell = 'f=$1; shift; exec "$@" 3<"$f"'
    return ["/bin/sh", "-c", shell, "sh", str(filter_path), *bwrap_argv]


def check_bwrap() -> None:
    """bwrap and pasta on the PATH, and one empty run through both, so a machine that can't
    make user namespaces refuses to start instead of failing every turn."""
    if not sys.platform.startswith("linux"):
        raise Unavailable('isolation = "bwrap" needs Linux')
    missing = [name for name in (BWRAP, PASTA) if not shutil.which(name)]
    if missing:
        raise Unavailable(
            f'isolation = "bwrap" needs {" and ".join(missing)} on the PATH '
            "(packages bubblewrap and passt)"
        )
    bwrap = [shutil.which(BWRAP) or BWRAP, *BWRAP_ARGS, "--seccomp", "3"]
    bwrap += ["--ro-bind", "/", "/", "--proc", "/proc", "--", "true"]
    with tempfile.NamedTemporaryFile(prefix="bots-seccomp-") as f:
        f.write(seccomp_filter())
        f.flush()
        probe = [
            shutil.which(PASTA) or PASTA,
            *PASTA_ARGS,
            "--",
            *_with_filter(Path(f.name), bwrap),
        ]
        try:
            done = subprocess.run(probe, capture_output=True, text=True, timeout=30, check=False)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise Unavailable(f"bwrap and pasta could not start a sandbox: {e}{UBUNTU_HINT}") from e
    if done.returncode != 0:
        why = done.stderr.strip().splitlines()[-1:] or [f"exit {done.returncode}"]
        raise Unavailable(f"bwrap and pasta could not start a sandbox: {why[0]}{UBUNTU_HINT}")


def resolv_conf(system: Path = RESOLV_CONF, upstream: Path = RESOLVED_UPSTREAM) -> str:
    """The run's /etc/resolv.conf. A server on loopback (systemd-resolved's 127.0.0.53, a
    local dnsmasq) is the host's and unreachable from the run's network, so it is replaced
    by systemd-resolved's own upstream list when there is one."""

    def servers(path: Path) -> tuple[list[str], list[str]]:
        try:
            lines = path.read_text().splitlines()
        except OSError:
            return [], []
        names = [
            ln.split()[1] for ln in lines if ln.startswith("nameserver") and len(ln.split()) > 1
        ]
        rest = [ln for ln in lines if ln.split()[:1] in (["search"], ["options"])]
        return names, rest

    names, rest = servers(system)
    reachable = [n for n in names if not n.startswith("127.") and n != "::1"]
    if not reachable:
        reachable = [n for n in servers(upstream)[0] if not n.startswith("127.") and n != "::1"]
    return "".join(f"nameserver {n}\n" for n in reachable) + "".join(f"{ln}\n" for ln in rest)


def _bwrap_path(path: Path) -> str:
    s = str(path)
    if "\0" in s:
        raise ValueError(f"path not allowed in a sandbox: {s!r}")
    return s


def own_soul(bot: Path) -> Path:
    """The bot's SOUL.md, made empty if missing, as a path safe to mount: never a link,
    which bwrap would follow to a file of the user's (a bot running unisolated before can
    have left one). No run of this bot is alive now (--die-with-parent, and the pid
    namespace ends with its first process), so nothing can swap it before bwrap opens it."""
    soul = bot / "SOUL.md"
    try:
        st = soul.lstat()
    except FileNotFoundError:
        botdir.write_own(bot, "SOUL.md", "")
        return soul
    if not stat.S_ISREG(st.st_mode):
        raise RuntimeError(f"{soul} is not a plain file; the host won't mount it into the sandbox")
    return soul


def bwrap_command(
    cfg: Config, bot_id: str, dirs: RunDirs, socket: Path, reads: list[Path], argv: list[str]
) -> list[str]:
    """pasta and bwrap argv around `argv` for one run of `bot_id`. Later mounts land on top
    of earlier ones, so each tmpfs comes before what it makes room for."""
    home = real(Path.home())
    data, bot, shared = real(cfg.root), real(cfg.bot_dir(bot_id)), real(cfg.shared_dir)
    tmp, engine, cap = real(dirs.tmp), real(dirs.home), real(dirs.cap_file)
    extra = [*reads, *(Path(p).expanduser() for p in cfg.isolation_read)]
    readable = sorted({*python_reads(), *(real(p) for p in extra if Path(p).exists())})
    # Written before the run starts, so before anything of the bot's can touch them.
    resolv, seccomp = tmp / ".resolv.conf", tmp / ".seccomp.bpf"
    resolv.write_text(resolv_conf())
    seccomp.write_bytes(seccomp_filter())
    args = [*BWRAP_ARGS, "--seccomp", "3"]
    for root in SYSTEM_ROOTS:
        if root.is_symlink():
            args += ["--symlink", os.readlink(root), str(root)]
        elif root.is_dir():
            args += ["--ro-bind", str(root), str(root)]
    args += ["--dev", "/dev", "--proc", "/proc"]
    for hidden in sorted({Path("/tmp"), Path("/var/tmp"), Path("/run"), home, data}):
        args += ["--tmpfs", _bwrap_path(hidden)]
    # A link (~/.local/bin/claude) under a hidden folder is gone with it: its target is
    # mounted at the link's path too.
    for path in sorted({p.absolute() for p in extra if Path(p).is_symlink()}):
        args += ["--ro-bind", _bwrap_path(real(path)), _bwrap_path(path)]
    for path in readable:
        args += ["--ro-bind", _bwrap_path(path), _bwrap_path(path)]
    checks: list[str] = []
    if cfg.isolation_scope == "shared":
        others, checks = _other_bots(real(cfg.bots_dir), bot)
        args += others
    for path in (bot, shared, tmp, engine):
        args += ["--bind", _bwrap_path(path), _bwrap_path(path)]
    memory = bot / "MEMORY.md"
    if cfg.isolation_scope == "shared" and _lstat(memory, stat.S_ISREG):
        # Its own mount, so a hard link to it from a subfolder, which other bots write,
        # crosses mounts and fails (EXDEV).
        args += ["--bind", _bwrap_path(memory), _bwrap_path(memory)]
    # Never its SOUL.md. A file mounted on is also one it can't rename, delete or link.
    args += ["--ro-bind", _bwrap_path(own_soul(bot)), _bwrap_path(bot / "SOUL.md")]
    args += ["--ro-bind", _bwrap_path(cap), _bwrap_path(cap)]
    args += ["--ro-bind", _bwrap_path(real(socket)), _bwrap_path(real(socket))]
    args += ["--ro-bind", _bwrap_path(resolv), _bwrap_path(real(RESOLV_CONF))]
    if checks:
        argv = ["/bin/sh", "-c", SAME_FOLDERS, "sh", *checks, "--", *argv]
    pasta, bwrap = shutil.which(PASTA) or PASTA, shutil.which(BWRAP) or BWRAP
    return [pasta, *PASTA_ARGS, "--", *_with_filter(seccomp, [bwrap, *args, "--", *argv])]


# Inside the sandbox, before the engine starts: each of another bot's subfolders mounted is
# the folder the host listed. That bot may be running, and could swap one for a link between
# the listing and bwrap's mount, which follows links: the link's target would be mounted
# writable. A swap shows as another device and inode, and the run stops there.
SAME_FOLDERS = """while [ "$1" != -- ]; do
  [ "$(stat -c %d:%i -- "$1")" = "$2" ] || { echo "bots: $1 changed as the sandbox was built" >&2; exit 126; }
  shift 2
done
shift
exec "$@"
"""


def _lstat(path: Path, kind: Callable[[int], bool]) -> os.stat_result | None:
    """`path`'s own stat when it is that kind of file (never through a link), else None."""
    try:
        st = path.lstat()
    except OSError:
        return None
    return st if kind(st.st_mode) else None


def _other_bots(bots: Path, bot: Path) -> tuple[list[str], list[str]]:
    """The shared scope's mounts for every other bot folder: the folder read-only, then each
    of its subfolders but the engines' writable on top. What stays read-only is what its
    prompt is built from: the files at the top (MEMORY.md, AGENTS.md, ...), and the folder's
    entries, so none can be created, renamed, removed or replaced. The subfolders are mount
    points, which can't be renamed or removed either. Listed now, so a bot added later is in
    the next run's. Returns the mounts and, per writable subfolder, its path and dev:inode."""
    args: list[str] = []
    checks: list[str] = []
    for other in sorted(bots.iterdir()) if _lstat(bots, stat.S_ISDIR) else []:
        if other == bot or not _lstat(other, stat.S_ISDIR):
            continue
        args += ["--ro-bind", _bwrap_path(other), _bwrap_path(other)]
        for sub in sorted(other.iterdir()):
            st = _lstat(sub, stat.S_ISDIR)
            if st is None or sub.name in ENGINE_DIRS:
                continue
            args += ["--bind", _bwrap_path(sub), _bwrap_path(sub)]
            checks += [_bwrap_path(sub), f"{st.st_dev}:{st.st_ino}"]
    return args, checks
