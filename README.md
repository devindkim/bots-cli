# bots-cli

Run a team of agents on your own machine with your Claude Code and Codex subscriptions.

Each bot is an agent with its own folder, memory, and schedule. Bots run on the `claude` and `codex` CLIs you already have. A message wakes the bot it addresses.

## Run a team

You need Python 3.12, [uv](https://docs.astral.sh/uv/), and a logged-in `claude` or `codex`.

```sh
uv tool install .
bots init
bots serve
```

In a second terminal, give the chief a job. `bots chat` returns when the whole team has finished:

```sh
bots chat chief "Add a --json flag to the export command in ~/src/app"
```

Run `bots watch` in a third terminal to see every message and run.

## Build your own team

`bots init` makes a chief, a researcher, an engineer, a reviewer, and a builder. They are only an example. Ask the builder for the team you want, or edit a bot's `~/.bots/bots/<id>/SOUL.md` yourself:

```sh
bots chat builder "Make me a bot that reviews new commits in ~/src/app every morning and only tells me if it finds a real bug."
bots chat builder "Add a second reviewer that runs on Codex."
bots routine add researcher "0 8 * * *" "Check the release notes. Stay silent unless something changed."
bots group new core --members chief,researcher,engineer             # a shared conversation
```

The builder runs `bots new`, so it needs default mode. In isolated mode it sends you the commands to run.

There is no web UI to install. Have your team build the one you want. Expect half an hour or more, because the reviewer tests it and can send it back:

```sh
bots chat chief "Build a small local web UI for this bots install. List bots, show messages, send a message. Bind 127.0.0.1 only."
```

## Primitives

| Primitive | What it is |
|---|---|
| Bot | A named agent with a folder under `~/.bots/bots/<id>/`. |
| Soul | `SOUL.md`, the bot's role and rules, in plain prose. |
| Memory | `MEMORY.md`, what the bot keeps between sessions. Each turn is a fresh session. |
| Conversation | Messages between you and a bot, or between two bots. A message wakes its addressee. |
| Group | A shared conversation. A bot's post wakes the members it @-mentions. |
| Routine | A cron schedule that sends a bot a prompt. Bots can schedule themselves. |
| Shared folder | `~/.bots/shared/`, where bots hand each other files. |
| Engine | The CLI a bot runs on: `claude` or `codex`. |

## Commands

| Command | Effect |
|---|---|
| `bots init [dir] [--engine E]` | Make a data folder and the example bots. |
| `bots serve`, `bots install`, `bots uninstall` | Run the host in the foreground, or as a user service. Turns need it. |
| `bots chat <bot-or-group> [text]` | Send, then follow until the team finishes. |
| `bots send <bot-or-group> <text>` | Send without waiting. |
| `bots read [bot-or-group]`, `bots watch` | Show messages to you, or follow everything. |
| `bots ls`, `new`, `model`, `rm`, `restore` | Manage bots. |
| `bots group new`, `ls`, `rm` | Manage groups. |
| `bots routine add <bot> "<cron>" "<prompt>"`, `ls`, `rm`, `pause`, `resume` | Manage schedules. |
| `bots pause <bot>`, `bots resume <bot>`, `bots stop <bot>` | Hold a bot, or kill its running turn. |
| `bots inspect [run]` | List recent runs, or show one and its logs. |

## File access

| | Default | Isolated, `shared` scope | Isolated, `bot` scope |
|---|---|---|---|
| Its own folder | Read, write | Read, write | Read, write |
| Its own `SOUL.md` | Read, write | Read only | Read only |
| `shared/` | Read, write | Read, write | Read, write |
| Other bots' folders | Read, write | Read; write in subfolders only | None |
| Database, `secrets/`, logs | Read, write | None | None |
| Your home and `/tmp` | Read, write | None | None |

Default mode runs bots as you with approvals off, so a bot can reach everything your account can. Give it only agents you would give your shell.

Set these in `~/.bots/config.toml` to run each turn in an OS sandbox. Bots can still reach the internet. If the sandbox is unavailable, the host refuses to start. Isolated mode has not yet run with a real login. See [SECURITY.md](SECURITY.md).

```toml
isolation = "seatbelt"       # or "bwrap" on Linux
isolation_scope = "bot"      # keep bots apart
isolation_read = ["~/.nvm"]  # extra folders a bot may read
```

## Logins

Your own subscription login works for running your own bots. Plan limits assume ordinary individual use. A product that serves other people should use API keys. Isolated mode can't reach your keychain, so set `CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_API_KEY`, or `CODEX_API_KEY` in the host's environment or in `~/.bots/secrets/engines.env` (mode 600). A service can't see shell exports, so API keys for a service go in that file too.

## Develop

```sh
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run mypy .
```
