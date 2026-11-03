"""The `bots` command. Talks to store.py directly; the running host reads the same database."""

from __future__ import annotations

import argparse
import datetime
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from cli import chat as terminal
from cli import init as first_run
from cli import service
from host import botdir, engine_keys, plugins, sandbox
from host.config import Config, load
from host.engines import engine_names, engines
from host.store import Store, group_conversation, open_store

STOP_WAIT = 20.0  # seconds the host gets to kill a turn; its tick is about a second


def main(argv: list[str] | None = None) -> int:
    try:
        cfg = load()  # before parsing: config.toml's plugins add commands and engines
        args = parser(cfg).parse_args(argv)
        return int(args.func(cfg, args) or 0)
    except (ValueError, RuntimeError, OSError) as e:
        print(f"bots: {e}", file=sys.stderr)
    except sqlite3.Error as e:
        print(f"bots: database: {e}", file=sys.stderr)
    except subprocess.CalledProcessError as e:
        print(f"bots: `{' '.join(e.cmd)}` failed with exit code {e.returncode}", file=sys.stderr)
    return 1


def nonblank(value: str) -> str:
    if not value.strip():
        raise argparse.ArgumentTypeError("must not be blank")
    return value.strip()


def parser(cfg: Config) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bots", description="Persistent named agents.")
    sub = p.add_subparsers(required=True, metavar="command")

    def cmd(name: str, func: Any, help: str, subs: Any = sub) -> argparse.ArgumentParser:
        c: argparse.ArgumentParser = subs.add_parser(name, help=help)
        c.set_defaults(func=func)
        return c

    c = cmd("init", init, "make a data folder with a config and a team")
    c.add_argument("dir", nargs="?", help="default: $BOTS_HOME, else ~/.bots")
    c.add_argument("--engine", choices=engine_names(cfg), help="default: claude or codex, found")

    cmd("serve", serve, "run the host in the foreground")
    c = cmd("install", install, "run the host as a user service (launchd or systemd)")
    c.add_argument(
        "--print", action="store_true", help="print the plist or unit; write and load nothing"
    )
    cmd("uninstall", uninstall, "stop and remove the user service")
    c = cmd("ls", ls, "list bots and their state")
    c.add_argument("--all", action="store_true", help="include archived bots")

    c = cmd("new", new, "create a bot")
    c.add_argument("id", nargs="?", default="", help="default: the next free bot-<n>")
    c.add_argument("--name", required=True, type=nonblank)
    c.add_argument("--title", default="")
    c.add_argument("--engine", choices=engine_names(cfg), default="claude")
    c.add_argument("--model", type=nonblank, help="model for the engine CLI (default: its own)")
    c.add_argument("--soul", type=Path, help="copy this file to the bot's SOUL.md")

    c = cmd("model", model, "set or clear a bot's model")
    c.add_argument("id")
    which = c.add_mutually_exclusive_group(required=True)
    which.add_argument("model", nargs="?", type=nonblank, help="e.g. sonnet, gpt-6.1-sol")
    which.add_argument("--default", action="store_true", help="clear it; use the CLI's default")

    c = cmd("rm", rm, "archive a bot")
    c.add_argument("id")
    c = cmd("restore", restore, "bring an archived bot back")
    c.add_argument("id")

    c = cmd("send", send, "message a bot or group as the user")
    c.add_argument("bot", metavar="bot-or-group")
    c.add_argument("text", type=nonblank)

    c = cmd("read", read, "show bots' messages to you and mark them read")
    c.add_argument("bot", nargs="?", help="a bot or group (default: every unread chat)")
    c.add_argument("-n", type=int, default=20, help="how many recent messages (default 20)")
    c.add_argument("--keep-unread", action="store_true", help="show without marking read")

    c = cmd("chat", chat, "message a bot or group and watch the team work (no text: a prompt loop)")
    c.add_argument("bot", metavar="bot-or-group")
    c.add_argument("text", nargs="?", type=nonblank)

    group = sub.add_parser("group", help="groups").add_subparsers(required=True, metavar="action")
    c = cmd("new", group_new, "create a group", group)
    c.add_argument("id")
    c.add_argument("--name")
    c.add_argument("--members", required=True, help="comma-separated bot ids")
    cmd("ls", group_ls, "list groups and their members", group)
    c = cmd("rm", group_rm, "archive a group (its messages are kept)", group)
    c.add_argument("id")

    routine = sub.add_parser("routine", help="routines").add_subparsers(
        required=True, metavar="action"
    )
    c = cmd("add", routine_add, "schedule a prompt for a bot", routine)
    c.add_argument("bot")
    c.add_argument("cron", help='5-field cron, local time, e.g. "0 8 * * 1-5"')
    c.add_argument("prompt", type=nonblank)
    c.add_argument("--name", type=nonblank)
    c = cmd("ls", routine_ls, "list routines", routine)
    c.add_argument("bot", nargs="?", help="only this bot's")
    for name, func, help in [
        ("rm", routine_rm, "delete a routine"),
        ("pause", routine_pause, "stop a routine firing until resumed"),
        ("resume", routine_resume, "let a paused routine fire again"),
    ]:
        cmd(name, func, help, routine).add_argument("id", type=int)

    c = cmd("stop", stop, "kill a bot's running turn")
    c.add_argument("bot")
    cmd("pause", pause, "stop waking a bot until it is resumed").add_argument("bot")
    cmd("resume", resume, "let a paused bot wake again").add_argument("bot")

    cmd("watch", watch, "follow every conversation and run until Ctrl-C")

    c = cmd("inspect", inspect, "list recent runs, or show one and where its logs are")
    c.add_argument("run", nargs="?", type=int)

    for plugin in plugins.load(cfg):
        if plugin.add_commands:
            plugin.add_commands(sub, cmd)
    return p


def init(cfg: Config, args: argparse.Namespace) -> None:
    first_run.main(cfg, args.dir, args.engine)


def serve(cfg: Config, args: argparse.Namespace) -> None:
    from host.serve import serve as run_host  # lazy: the CLI works without the host's deps

    for line in engine_keys.missing(cfg, used_engines(cfg)):
        print(line, file=sys.stderr)
    run_host(cfg)


# -- the user service (cli/service.py) ----------------------------------------


def install(cfg: Config, args: argparse.Namespace) -> None:
    if args.print:
        sys.stdout.write(service.text(cfg))
        return
    # Before writing anything: a key file the host would refuse stops the install too.
    warnings = engine_keys.missing(cfg, used_engines(cfg))
    print(service.install(cfg))
    for line in warnings:
        print(line, file=sys.stderr)


def used_engines(cfg: Config) -> list[str]:
    """The engines the data folder's bots run on; none before the first bot."""
    if not cfg.db_path.exists():
        return []
    return sorted({b["engine"] for b in open_store(cfg).list_bots()})


def uninstall(cfg: Config, args: argparse.Namespace) -> None:
    print(service.uninstall(cfg))


# -- bots, groups, routines --------------------------------------------------


def ls(cfg: Config, args: argparse.Namespace) -> None:
    store = open_store(cfg)
    rows = []
    for b in store.list_bots(include_archived=args.all):
        if b["archived"]:
            state = "archived"
        else:
            state = "paused" if b["paused"] else store.bot_state(b["id"])
        engine = f"{b['engine']}:{b['model']}" if b["model"] else b["engine"]
        rows.append([b["id"], b["name"], engine, state, b["title"]])
    table(rows)


def new(cfg: Config, args: argparse.Namespace) -> None:
    if args.soul and not args.soul.is_file():
        raise ValueError(f"no such file {args.soul}")
    bot_id, folder = make_bot(
        cfg, args.id, args.name, args.title, args.engine, args.model, args.soul
    )
    print(f"created {bot_id}: {folder}")


def make_bot(
    cfg: Config,
    bot_id: str,
    name: str,
    title: str,
    engine: str,
    model: str | None,
    soul: Path | None,
) -> tuple[str, Path]:
    store = open_store(cfg)
    if (bot := store.get_bot(bot_id)) and bot["archived"]:
        raise ValueError(f"{bot_id} is archived; `bots restore {bot_id}` brings it back")
    bot_id = store.create_bot(bot_id, name, title, engine, model)
    folder = botdir.scaffold(cfg, bot_id)
    if soul:
        shutil.copyfile(soul, folder / "SOUL.md")
    return bot_id, folder


def model(cfg: Config, args: argparse.Namespace) -> None:
    """Takes effect at the bot's next turn; a running one keeps the model it started with."""
    store = open_store(cfg)
    if not (bot := store.get_bot(args.id)) or bot["archived"]:
        raise ValueError(f"no bot {args.id!r}")
    store.update_bot(args.id, model=None if args.default else args.model)
    print(f"{args.id}: {'default model' if args.default else args.model} from its next turn")


def rm(cfg: Config, args: argparse.Namespace) -> None:
    store = open_store(cfg)
    if (bot := store.get_bot(args.id)) and bot["archived"]:
        raise ValueError(f"{args.id} is already archived")
    if store.running_run(args.id):
        raise ValueError(f"{args.id} is working; `bots stop {args.id}` first")
    store.archive_bot(args.id)
    print(f"archived {args.id} (folder and messages kept at {cfg.bot_dir(args.id)})")


def restore(cfg: Config, args: argparse.Namespace) -> None:
    open_store(cfg).unarchive_bot(args.id)
    print(f"restored {args.id}; its routines stay paused and it rejoins no groups")


def send(cfg: Config, args: argparse.Namespace) -> None:
    store = open_store(cfg)
    msg = store.user_send(terminal.target(store, args.bot), args.text)
    print(f"sent #{msg.id} to {args.bot}")


def read(cfg: Config, args: argparse.Namespace) -> None:
    if args.n < 1:
        raise ValueError("-n must be at least 1")
    terminal.read(open_store(cfg), args.bot, args.n, args.keep_unread, sys.stdout)


def chat(cfg: Config, args: argparse.Namespace) -> None:
    terminal.chat(open_store(cfg), args.bot, args.text)


def group_new(cfg: Config, args: argparse.Namespace) -> None:
    members = [m.strip() for m in args.members.split(",") if m.strip()]
    conv = open_store(cfg).create_group(args.id, args.name or args.id, members)
    print(f"created {conv}")


def group_ls(cfg: Config, args: argparse.Namespace) -> None:
    groups = open_store(cfg).list_groups()
    table([[g["id"], g["name"], ", ".join(g["members"])] for g in groups])


def group_rm(cfg: Config, args: argparse.Namespace) -> None:
    store = open_store(cfg)
    conv_id = group_conversation(args.id)
    conv = store.get_conversation(conv_id)
    if conv is None or conv["kind"] != "group" or conv["archived"]:
        raise ValueError(f"no group {args.id!r}")
    store.archive_conversation(conv_id)
    print(f"archived {conv_id} (messages kept)")


def routine_add(cfg: Config, args: argparse.Namespace) -> None:
    rid = open_store(cfg).create_routine(
        args.bot, args.name or routine_name(args.prompt), args.cron, args.prompt
    )
    print(f"routine {rid} for {args.bot}: {args.cron}")


def routine_name(prompt: str, width: int = 40) -> str:
    """The prompt's first line, cut to `width` with an ellipsis."""
    line = prompt.strip().splitlines()[0].strip()
    return line if len(line) <= width else line[: width - 1].rstrip() + "…"


def routine_ls(cfg: Config, args: argparse.Namespace) -> None:
    routines = open_store(cfg).list_routines(args.bot)
    table(
        [
            [r["id"], r["bot_id"], r["cron"], "paused" if r["paused"] else "on", r["name"]]
            for r in routines
        ]
    )


def routine_rm(cfg: Config, args: argparse.Namespace) -> None:
    store = open_store(cfg)
    _routine(store, args.id)
    store.delete_routine(args.id)
    print(f"deleted routine {args.id}")


def routine_pause(cfg: Config, args: argparse.Namespace) -> None:
    store = open_store(cfg)
    _routine(store, args.id)
    store.update_routine(args.id, paused=1)
    print(f"paused routine {args.id}")


def routine_resume(cfg: Config, args: argparse.Namespace) -> None:
    store = open_store(cfg)
    bot_id = _routine(store, args.id)["bot_id"]
    if (bot := store.get_bot(bot_id)) and bot["archived"]:
        raise ValueError(f"routine {args.id} belongs to archived {bot_id}; restore it first")
    store.update_routine(args.id, paused=0)
    print(f"resumed routine {args.id}")


def set_paused(cfg: Config, bot_id: str, paused: bool) -> None:
    store = open_store(cfg)
    if not (bot := store.get_bot(bot_id)) or bot["archived"]:
        raise ValueError(f"no bot {bot_id!r}")
    store.update_bot(bot_id, paused=paused)


def pause(cfg: Config, args: argparse.Namespace) -> None:
    set_paused(cfg, args.bot, True)
    print(f"paused {args.bot}; a running turn finishes")


def resume(cfg: Config, args: argparse.Namespace) -> None:
    set_paused(cfg, args.bot, False)
    print(f"resumed {args.bot}")


def stop(cfg: Config, args: argparse.Namespace) -> int:
    """Mark the live run and wait for the host to kill it. Its messages count as read, so
    only a new message wakes the bot."""
    store = open_store(cfg)
    run_id = store.request_stop(args.bot)
    if run_id is None:
        print(f"{args.bot} had no running turn")
        return 0
    deadline = time.monotonic() + STOP_WAIT
    while (run := store.get_run(run_id)) and run["status"] == "running":
        if time.monotonic() > deadline:
            print(f"bots: {args.bot}'s turn did not stop; is the host running?", file=sys.stderr)
            return 1
        time.sleep(0.1)
    print(f"stopped {args.bot}'s turn")
    return 0


def watch(cfg: Config, args: argparse.Namespace) -> None:
    terminal.watch(open_store(cfg), sys.stdout)


def inspect(cfg: Config, args: argparse.Namespace) -> None:
    store = open_store(cfg)
    if args.run is None:
        table(
            [
                [
                    r["id"],
                    r["bot_id"],
                    r["status"],
                    f"${r['cost'] or 0:.2f}",
                    terminal.stamp(r["started_at"]),
                ]
                for r in store.list_runs()
            ]
        )
        return
    run = store.get_run(args.run)
    if not run:
        raise ValueError(f"no run {args.run}")
    for key, value in run.items():
        if key in ("started_at", "ended_at", "last_event_at", "retry_after") and value:
            value = datetime.datetime.fromtimestamp(value).isoformat(timespec="seconds")
        print(f"{key:<14} {value}")
    bot = store.get_bot(run["bot_id"])
    print(f"{'transcript':<14} {transcript(cfg, bot['engine'] if bot else '', run)}")
    logs = cfg.logs_dir / str(run["id"])
    for f in sorted(logs.iterdir()) if logs.is_dir() else []:
        print(f"{'log':<14} {f}")
    if not logs.is_dir():
        print(f"{'log':<14} (none at {logs})")


def transcript(cfg: Config, engine: str, run: dict[str, Any]) -> str:
    """Where the engine left its session file. Engine-specific, read-only."""
    session = run["session_id"]
    if not session:
        return "(no session id recorded)"
    # By the CLI the engine runs (a plugin engine may run claude too). Found by id, not by folder,
    # so runs from before a move resolve.
    spec = engines(cfg).get(engine)
    command = spec.command if spec else ""
    # An isolated run's engine kept its state in the bot's own engine home; look there too.
    homes = [Path.home()]
    if run.get("bot_id"):
        homes.append(sandbox.engine_home(cfg, run["bot_id"]))
    if command == "claude":
        hits = [
            h for home in homes for h in (home / ".claude" / "projects").glob(f"*/{session}.jsonl")
        ]
        return str(max(hits)) if hits else f"(no ~/.claude/projects/*/{session}.jsonl)"
    if command == "codex":
        pattern = f"**/rollout-*-{session}.jsonl"
        hits = [h for home in homes for h in (home / ".codex" / "sessions").glob(pattern)]
        return str(max(hits)) if hits else f"(no ~/.codex/sessions/{pattern})"
    return f"(engine {engine!r} keeps no transcript; session {session})"


def table(rows: list[list[Any]]) -> None:
    """Left-aligned columns as wide as their widest cell; the last column is not padded."""
    if not rows:
        return
    widths = [max(len(str(row[i])) for row in rows) for i in range(len(rows[0]) - 1)]
    for row in rows:
        cells = [f"{c!s:<{w}}" for c, w in zip(row, widths, strict=False)] + [str(row[-1])]
        print("  ".join(cells).rstrip())


def _routine(store: Store, routine_id: int) -> dict[str, Any]:
    for r in store.list_routines():
        if r["id"] == routine_id:
            return r
    raise ValueError(f"no routine {routine_id}")


if __name__ == "__main__":
    sys.exit(main())
