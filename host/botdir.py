"""A bot's folder: SOUL.md, MEMORY.md, notes/, and the generated AGENTS.md."""

from __future__ import annotations

import contextlib
import os
import stat
import threading
from pathlib import Path

from host import plugins
from host.config import REPO, Config

SKILL = REPO / "skills" / "being-a-bot.md"
PLUGIN_SECTIONS = "<!-- plugin sections -->"  # where render_skill puts the added sections

SOUL_TEMPLATE = """## Who I report to

The user.

## Ask first

- Pushing to any remote, opening PRs, sending anything outside this machine.
- Deleting files outside my folder and shared/.
"""

MEMORY_TEMPLATE = """# Memory

## What I know
"""


def scaffold(cfg: Config, bot_id: str) -> Path:
    """Create the folder with starter SOUL.md and MEMORY.md. Existing files are left alone."""
    folder = cfg.bot_dir(bot_id)
    (folder / "notes").mkdir(parents=True, exist_ok=True)
    cfg.shared_dir.mkdir(parents=True, exist_ok=True)
    for filename, text in (
        ("SOUL.md", SOUL_TEMPLATE),
        ("MEMORY.md", MEMORY_TEMPLATE),
    ):
        if not (folder / filename).exists() and not (folder / filename).is_symlink():
            write_own(folder, filename, text)
    return folder


def write_agents_md(cfg: Config, bot_id: str, name: str, title: str = "") -> Path:
    """AGENTS.md = name header + SOUL.md + the skill + MEMORY.md; CLAUDE.md symlinks to it.

    Regenerated before every run, so the engine loads memory itself, and a rename shows
    up on the bot's next turn.
    """
    folder = cfg.bot_dir(bot_id)
    memory = _read(folder / "MEMORY.md")
    if _links(folder / "MEMORY.md") > 1:
        # A second name for it, maybe in a subfolder other bots write: a fresh copy cuts it off.
        write_own(folder, "MEMORY.md", memory)
    parts = [
        f"# {name} (id {bot_id})" + (f"\n\n{title}" if title else ""),
        _read(folder / "SOUL.md"),
        render_skill(cfg, bot_id),
        "# MEMORY.md (yours; edit the file, not this copy)\n\n" + memory,
    ]
    agents = write_own(
        folder,
        "AGENTS.md",
        "<!-- generated before every run; edit SOUL.md or MEMORY.md -->\n\n"
        + "\n\n---\n\n".join(p.strip() for p in parts if p.strip())
        + "\n",
    )
    # Any other CLAUDE.md, a file or a link elsewhere (notes/x.md, which other bots may
    # write), would be what the engine loads instead.
    claude = folder / "CLAUDE.md"
    if not claude.is_symlink() or os.readlink(claude) != "AGENTS.md":
        claude.unlink(missing_ok=True)
        claude.symlink_to("AGENTS.md")
    return agents


# Isolated mode (host/sandbox.py) changes what a bot can reach, so the skill must not
# promise the rest. Each passage of the shared text it changes, and what it becomes there.
ISOLATED_SWAPS = {
    "- `read_conversation(conversation_id, limit, before)`: read any\n"
    "  conversation, yours or not.": (
        "- `read_conversation(conversation_id, limit, before)`: read a\n"
        "  conversation you are in: your chat with the user, your pairs, your\n"
        "  groups. Others are closed on this host."
    ),
    "- Before starting delegated work, read the conversation between the user\n"
    "  and whoever delegated it (`user:<their id>`) for the original ask; do not\n"
    "  trust a paraphrase.": (
        "- Before starting delegated work, ask whoever delegated it to quote the\n"
        "  user's original ask: their chat with the user is closed to you here."
    ),
}
ISOLATED_SECTION = """## Isolation

This host runs every bot in a sandbox. You can read and write your own
folder (not SOUL.md, which is read-only) and `shared/`, and read the tools
you run on; the rest of the user's home, the other bots' folders and the
host's data are closed, and so are this machine's own network addresses.
Your `$HOME` and `$TMPDIR` are yours alone. A command that fails with
"Operation not permitted" hit the fence: work inside it, or ask the user."""
# The shared scope (isolation_scope = "shared", the default): the same fence around every bot
# together. The files a bot's prompt is built from stay its own (host/sandbox.py).
SHARED_SECTION = """## Isolation

This host runs every bot in a sandbox, and all bots share it. You can read
every bot's folder and `shared/`, and write `shared/`, your own folder (not
SOUL.md, which is read-only) and the subfolders of other bots' folders
(their notes/ and the like). The files at the top of another bot's folder
(SOUL.md, MEMORY.md, AGENTS.md) and its .claude, .codex and .agents folders
are its own: ask it to change them. The rest of the user's home, the host's
data and this machine's own network addresses are closed. Your `$HOME` and
`$TMPDIR` are yours alone. A command that fails with
"Operation not permitted" hit the fence: work inside it, or ask the user."""
SHARED_SWAPS = {
    "Never read other\nbots' folders.": (
        "Other bots'\nfolders are open to you: read them when the work needs it, and tell a\n"
        "bot when you change something in its folder."
    ),
}
# How the fence shows under bwrap (Linux): closed paths are empty or missing, and writes
# outside the bot's paths meet a read-only file system.
BWRAP_FENCE = (
    '"Operation not permitted" hit the fence: work inside it, or ask the user.',
    (
        '"Read-only file system", or can\'t find a path outside your folder, hit\n'
        "the fence: work inside it, or ask the user."
    ),
)


def render_skill(cfg: Config, bot_id: str) -> str:
    """The skill with the plugins' sections (config.toml's order), then each
    `<data dir>/skills/*.md` by name, in place of PLUGIN_SECTIONS.

    That folder is the user's own: lines for their setup that the shared skill
    shouldn't carry. With none, the marker and the blank line after it go. In isolated
    mode the Isolation section comes first, and ISOLATED_SWAPS replace what it closes (with
    SHARED_SWAPS, what the shared scope opens)."""
    if not SKILL.exists():
        return ""
    paths = [*plugins.skill_fragments(cfg), *sorted((cfg.root / "skills").glob("*.md"))]
    added = "".join(p.read_text().strip() + "\n\n" for p in paths)
    text = SKILL.read_text()
    if cfg.isolation != "none":
        shared = cfg.isolation_scope == "shared"
        for old, new in {**ISOLATED_SWAPS, **(SHARED_SWAPS if shared else {})}.items():
            text = text.replace(old, new)
        section = SHARED_SECTION if shared else ISOLATED_SECTION
        if cfg.isolation == "bwrap":
            section = section.replace(*BWRAP_FENCE)
        added = section + "\n\n" + added
    return text.replace(PLUGIN_SECTIONS + "\n\n", added).replace("<id>", bot_id)


def _read(path: Path) -> str:
    """A file the bot keeps, never through a link: a MEMORY.md linked to ~/.ssh/id_rsa would
    copy the key into AGENTS.md, which the bot reads."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:  # missing, or a link
        return ""
    with os.fdopen(fd) as f:
        return f.read() if stat.S_ISREG(os.fstat(fd).st_mode) else ""


def _links(path: Path) -> int:
    """How many names the plain file at `path` has; 0 when it is missing or not a plain file."""
    try:
        st = path.lstat()
    except OSError:
        return 0
    return st.st_nlink if stat.S_ISREG(st.st_mode) else 0


def write_own(folder: Path, name: str, text: str) -> Path:
    """Write folder/name as the host, never through a link the bot left there. The host runs
    unsandboxed, so `AGENTS.md -> ~/.zshrc` would let a write land outside the sandbox. The
    file is replaced, not written into, relative to a held folder descriptor."""
    fd = os.open(folder, _DIR_FLAGS)
    try:
        tmp = f".{name}.{os.getpid()}.{threading.get_ident()}.tmp"
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp, dir_fd=fd)
        out = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=fd)
        with os.fdopen(out, "w") as f:
            f.write(text)
        os.replace(tmp, name, src_dir_fd=fd, dst_dir_fd=fd)
    finally:
        os.close(fd)
    return folder / name


_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
