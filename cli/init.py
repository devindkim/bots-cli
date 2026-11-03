"""`bots init`: a fresh data folder with a config and a team. Refuses a folder already in use."""

from __future__ import annotations

import shlex
import shutil
import sys
from pathlib import Path

from host import engine_keys
from host.config import HOME, REPO, Config, load

SOULS = REPO / "skills" / "souls"
LEAD = "chief"
UI_PROMPT = (
    "Build a small local web UI for this bots install. "
    "List bots, show messages, send a message. Bind 127.0.0.1 only."
)


# The env var each engine's CLI logs in with when it runs headless (API-key mode): the
# first of its engine_keys.LOGINS.
KEYS = {engine: names[0] for engine, (names, _) in engine_keys.LOGINS.items()}

CONFIG = """\
# Made by `bots init`. Options: config.toml.example in the bots repo.
max_concurrent = 2         # engine runs at once
daily_budget = 0           # USD per local day, engine-reported; 0: off. Subscription logins report notional cost

# "none": engines run as you, with everything your account can reach. "seatbelt" (macOS)
# or "bwrap" (Linux; needs bubblewrap and passt) runs every turn in a sandbox that closes
# this data folder, your home and this machine's addresses to it. Isolated engines log in
# by env only (ANTHROPIC_API_KEY, CLAUDE_CODE_OAUTH_TOKEN, CODEX_API_KEY).
isolation = "none"
"""


def taken(cfg: Config) -> list[str]:
    """What in the folder says a host already lives there."""
    marks = [cfg.root / "config.toml", cfg.db_path, cfg.bots_dir]
    return [p.name for p in marks if p.exists() or p.is_symlink()]


def default_engine(cfg: Config) -> str:
    """claude if its CLI is on the PATH, else codex if that one is, else claude."""
    for engine in KEYS:
        if shutil.which(cfg.engine_binary(engine)):
            return engine
    return "claude"


def run(root: Path, engine: str) -> str:
    """Make the folder and return what to print. Checks everything it can before writing."""
    from cli.main import make_bot  # cli.main imports this module

    cfg = Config(root=root)
    if found := taken(cfg):
        raise ValueError(
            f"{root} already holds a host's data ({', '.join(found)}); pick an empty or new folder"
        )
    souls = sorted(SOULS.glob("*.md"))
    if SOULS / f"{LEAD}.md" not in souls:
        raise RuntimeError(f"the example souls in {SOULS} are missing from this install")
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.toml").write_text(CONFIG)
    cfg = load(root)
    for soul in souls:
        make_bot(cfg, soul.stem, soul.stem.title(), soul.stem.title(), engine, None, soul)
    return report(cfg, engine, [soul.stem for soul in souls])


def report(cfg: Config, engine: str, bots: list[str]) -> str:
    # Quoted: the user pastes these into a shell, and a folder name may have spaces or $.
    env = "" if cfg.root == HOME.resolve() else f"BOTS_HOME={shlex.quote(str(cfg.root))} "
    lines = [
        f"made {cfg.root}: config.toml and {len(bots)} example bots on {engine}:",
        f"  {', '.join(bots)}",
        "",
        f"They are only an example. Edit each {cfg.bots_dir}/<id>/SOUL.md, or use `bots new`",
        "and `bots rm` to build your own team.",
        "",
    ]
    if key := KEYS.get(engine):
        lines += [
            f"Already logged in to {engine}? You are done. Otherwise:",
            "",
            f"  export {key}=...",
            f"  # for `bots install`, put {key}=... in {engine_keys.path(cfg)} (chmod 600)",
            "",
        ]
    lines += [
        "Run these in two terminals:",
        "",
        f"  {env}bots serve",
        f"  {env}bots chat {LEAD}",
        "",
        "Then try:",
        "",
        f'  {env}bots chat builder "Make me a bot that reviews new commits in ~/src/app every morning and only tells me if it finds a real bug."',
        f'  {env}bots chat {LEAD} "{UI_PROMPT}"',
        "",
        "Bots run as you, with everything your account can reach. To fence them in, see",
        f"isolation in {cfg.root / 'config.toml'}.",
    ]
    if engine in KEYS and not shutil.which(cfg.engine_binary(engine)):
        lines += ["", f"{engine} is not on your PATH yet; install its CLI before the first turn."]
    return "\n".join(lines)


def main(cfg: Config, directory: str | None, engine: str | None) -> None:
    root = Path(directory).expanduser().resolve() if directory else cfg.root
    print(run(root, engine or default_engine(cfg)))
    sys.stdout.flush()
