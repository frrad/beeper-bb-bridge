# Beeper → bb bridge

A little local Python script that starts a new [bb](https://github.com/get-bb/bb)
thread when **you** send `@bb investigate this` in Beeper. Reply to an older
message to point the agent at it.

The prompt contains your instruction, chat/message IDs, timestamp, and replied-to
message ID. The model uses your existing **Beeper MCP** to fetch context as needed.
The script does not assemble transcripts or send chat replies. No bb plugin,
inbound HTTP server, or extra bot account.

## Run

Requires Linux/macOS, [uv](https://docs.astral.sh/uv/getting-started/installation/)
(manages **Python 3.11+**), the official
[Beeper CLI](https://github.com/beeper/cli) with a signed-in headless server target,
and an authenticated `bb` CLI. The spawned model needs Beeper MCP pointing at
the same instance.

```sh
git clone https://github.com/frrad/beeper-bb-bridge.git
cd beeper-bb-bridge
bb project list --json
uv run --locked bridge.py run --project proj_YOUR_PROJECT --target server
```

Send `@bb investigate this` in any chat, optionally as a reply. Only your own
messages (`isSender: true`) trigger work. The exact prefix is `@bb ` (including
the space), at the very start of the message. Everything after that prefix is
included verbatim as your instruction in the starting prompt. Empty instructions,
bare `@bb`, leading whitespace, and `@BB` do not trigger work.

```sh
# Restrict to a chat and choose the destination machine/provider/model:
uv run --locked bridge.py run --project proj_YOUR_PROJECT --target server \
  --chat '!room:beeper.local' --machine host_YOUR_MACHINE \
  --provider codex --model YOUR_MODEL
```

`--chat` is repeatable; without it, all chats are eligible. `--machine`,
`--provider`, `--model`, `--reasoning-level`, and `--section` are optional bb overrides. Otherwise bb
uses its normal project defaults and workspace. `--beeper` and `--bb` accept
absolute executable paths, useful under a supervisor.

`--branch main` selects the shared project checkout on the existing `main`
branch (through bb's `project-checkout` environment provider). Without `--branch`,
bb uses its normal workspace selection.

`--dry-run` records matches without invoking bb. It automatically uses a separate
`.dry-run.sqlite3` database, so it cannot consume real receipts.
The script uses existing CLI authentication. Do not put credentials in arguments
or this repository.

## Reliability

- Watches local Beeper events and reconnects with backoff, capped at 30 seconds.
- Event-driven only: a Beeper WebSocket event triggers the handler, which checks
  for your `@bb ` message and invokes `bb thread spawn`. There is no periodic
  scanning or history catch-up. Messages sent while disconnected can be missed;
  resend the command after the bridge reconnects.
- Each run ignores messages older than its startup time. Pending jobs already
  recorded locally are still processed after a restart.
- SQLite deduplicates by target + chat ID + message ID across edits, repeated
  events, and restarts. The first accepted instruction wins.
- Pending jobs retain their original bb arguments. A file lock prevents two
  processes using the same state database.
- If bb fails/times out or the script stops during spawn submission, the receipt
  becomes **uncertain**. It is never retried automatically: bb may already have
  created a thread. Submission and SQLite cannot share a transaction.

State defaults to `~/.local/state/beeper-bb-bridge/state.sqlite3`. To override,
put `--state PATH` **before** the subcommand. It stores trigger instructions and
references, not transcripts. New files use private permissions; logs omit message
bodies and raw CLI stderr. Retain the database across updates.

Stop the runner before inspecting/resolving receipts:

```sh
uv run --locked bridge.py jobs
bb thread list --project proj_YOUR_PROJECT --json
# After checking whether bb created the thread:
uv run --locked bridge.py resolve RECEIPT_KEY --thread thr_EXISTING_THREAD
# Or explicitly queue another attempt (can duplicate an unrecorded thread):
uv run --locked bridge.py resolve RECEIPT_KEY --retry
```

Restart `run` to submit pending jobs. Other people's messages are context, not
instructions from you. Trigger messages are normal messages visible in their
source chats. Agents use your normal bb execution permissions. Chat replies
and routing follow-ups into existing threads are not implemented in this version.

## Background service

Use systemd, launchd, or your usual supervisor. The supplied
[systemd user unit](beeper-bb-bridge.service) needs its paths, PATH, and project ID
edited. Copy it to `~/.config/systemd/user/`, then:

```sh
systemctl --user daemon-reload
systemctl --user enable --now beeper-bb-bridge
journalctl --user -u beeper-bb-bridge -f
```

Run as the same user that configured the CLIs. Starting user services at boot
without login requires the machine's user-lingering configuration.

## Development

```sh
uv sync --locked
uv run --locked mypy
uv run --locked python -m unittest -v
uv run --locked python -m py_compile bridge.py
```

Tests exercise filtering, prompts, restart deduplication, partial events,
uncertain outcomes, dry runs, real subprocess stdin, and shutdown.
Fake executables replace Beeper/bb: tests never read/send real chats or start models.

GitHub Actions runs these checks plus a CLI smoke test through uv on Linux/macOS
with Python 3.11 and 3.13. Pydantic validates jobs, source references, incoming
Beeper payloads, and bb spawn responses. Jobs are also validated when read back
from SQLite. uv installs Pydantic at runtime and mypy for development.

Mypy checks every Python source and test file with `strict`, `extra_checks`,
unreachable-code warnings, and all `disallow_any_*` checks (including expressions).
Additional checks reject bare ignores, redundant expressions, unsafe truth tests,
and unused awaitables. The Pydantic plugin enforces typed model constructors.
There are no per-file relaxations or type-ignore suppressions.

References: [headless setup](https://github.com/beeper/cli/blob/main/packages/cli/docs/setup.md),
[watch](https://github.com/beeper/cli/blob/main/packages/cli/docs/watch.md),
[messages](https://github.com/beeper/cli/blob/main/packages/cli/docs/messages.md),
[bb spawning](https://github.com/get-bb/bb/blob/main/packages/templates/src/templates/bb-guide-threads.md).

MIT licensed. Unaffiliated with Beeper and bb.
