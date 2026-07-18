# Setup

This guide takes you from a fresh clone to a running agent that emails you findings. Do it in
order. You can stop after **step 4** and use the agent purely in `DRY_RUN` (it thinks and
writes to disk, sends nothing) — that is a safe, useful mode on its own.

- [1. Install](#1-install)
- [2. Create a mailbox for the agent](#2-create-a-mailbox-for-the-agent)
- [3. Configure secrets](#3-configure-secrets)
- [4. Verify (DRY_RUN)](#4-verify-dry_run)
- [5. Understand the three modes](#5-understand-the-three-modes)
- [6. Go SUPERVISED, then LIVE](#6-go-supervised-then-live)
- [7. Install the timers](#7-install-the-timers-macos-launchd)
- [8. Day-to-day operation](#8-day-to-day-operation)
- [Troubleshooting](#troubleshooting)

---

## 1. Install

Requirements: macOS, Python 3.11+, and the [`claude` CLI](https://claude.com/claude-code)
installed and logged in under your own account (`claude` uses your **subscription**; cagent
never uses an API key).

```bash
python3 -m venv .venv
./.venv/bin/pip install -e '.[dev]'     # [dev] pulls in pytest + ruff for the test suite
git config core.hooksPath .githooks      # turn on the secret-guard git hooks
```

Confirm the `claude` CLI works and is authenticated:

```bash
claude -p "say hello" --output-format json
```

If that fails, fix your `claude` login first — cagent cannot think without it.

## 2. Create a mailbox for the agent

Give the agent its **own** mailbox. Do not point it at your personal inbox — the agent reads,
marks, and threads mail, and you want a clean audit trail.

1. Create a Gmail or Google Workspace account for it, e.g. `myagent@gmail.com`.
2. Enable 2-step verification on that account.
3. Create an **app password** (Google Account → Security → App passwords). This is a 16-char
   password that grants IMAP/SMTP access and is independently revocable. cagent uses IMAP to
   receive and SMTP to send; your machine only ever makes **outbound** connections.

Gmail's `+tag` addressing (`myagent+scout@gmail.com`) is what routes replies back to the right
persona, so a single mailbox can host several personas. See [PERSONAS.md](PERSONAS.md).

## 3. Configure secrets

Secrets live **outside** the repo, at `~/.config/cagent/.env` (mode 600). They are never
committed — `.gitignore`, the gitleaks hooks, and an in-process scan all guard against it.

```bash
mkdir -p ~/.config/cagent
cp cagent/.env.example ~/.config/cagent/.env
chmod 600 ~/.config/cagent/.env
$EDITOR ~/.config/cagent/.env
```

Fill in at least:

| Variable | Meaning |
|---|---|
| `AGENT_EMAIL` | the agent's own address, e.g. `myagent@gmail.com` |
| `AGENT_FROM_NAME` | default From: display name (a persona can override it) |
| `GMAIL_APP_PASSWORD` | the 16-char app password from step 2 |
| `OWNER_EMAIL` | **you** — the only address the agent may ever write to |
| `OWNER_NAME` | your display name (cosmetic, used in the To: header) |
| `STAGING_RECIPIENT` | where non-LIVE mail goes; use `you+cagent-staging@gmail.com` |
| `COMMAND_TOKEN` | a shared secret you invent; required in email-command subjects |
| `CLAUDE_BIN` | path to the `claude` binary if not on `PATH` |

IMAP/SMTP host/port already default to Gmail. The `.env.example` file also documents optional
**per-persona overlays** (`.env-<mailbox>` / `.env-<owner>`) for giving individual personas
their own sending account or their own owner — you don't need those to start.

## 4. Verify (DRY_RUN)

The repo ships in `DRY_RUN` (`config.toml` → `[agent] mode = "DRY_RUN"`). In this mode the
agent thinks and writes everything to disk but **sends nothing**.

```bash
bin/cagentctl config     # resolved config with secrets redacted — sanity-check the addresses
bin/cagentctl doctor     # preflight: claude auth, structured-output, constitution containment
bin/cagentctl run-tick   # force one tick right now
bin/cagentctl recent     # see the goals, notes, and drafted (unsent) email it produced
```

`doctor` is the important one: it confirms the `claude` CLI returns valid structured JSON and
that the constitution correctly overrides your own global `~/.claude/CLAUDE.md` (so the agent
identifies as itself and ignores your personal Claude Code instructions).

Run `run-tick` a few times. Read `bin/cagentctl recent` and `bin/cagentctl sent` after each.
You are watching for: sensible goal choices, sourced notes, and drafts that end with the
AI-disclosure line. Nothing leaves your machine yet.

## 5. Understand the three modes

Mode is set per-persona (`personas/<name>/config.toml`) and globally (`config.toml`), and can be
overridden at runtime with the `AGENT_MODE` env var.

| Mode | Thinks? | Researches? | Email behavior |
|---|---|---|---|
| `DRY_RUN` | yes | yes | drafts written to disk only; nothing sent |
| `SUPERVISED` | yes | yes | drafts **staged for your approval**; sent to the staging address only after you `approve` |
| `LIVE` | yes | yes | approved content auto-sends to the real owner, under the daily/weekly caps |

In any non-LIVE mode, all mail is forced to `STAGING_RECIPIENT`, never the real owner. This is
a hard guard, not a convention.

## 6. Go SUPERVISED, then LIVE

When DRY_RUN output looks good:

1. Set `mode = "SUPERVISED"` for a persona (or globally). Run some ticks.
2. `bin/cagentctl pending` lists drafts awaiting approval. Inspect one, then:
   - `bin/cagentctl approve <token>` — releases it (to the staging address).
   - `bin/cagentctl reject <token>` — discards it.
3. Watch the staged mail actually arrive at your staging address. Confirm the facts, the
   sources, and the disclosure footer are all correct.
4. Only once you trust it, promote a persona to `LIVE`. Promotion is deliberately a **local**
   action (editing config / local `cagentctl`); no email can promote autonomy.

`bin/cagentctl readiness` prints a graduation snapshot (days running, ticks, gate-check blocks,
unsafe egress) across all personas to help you decide.

## 7. Install the timers (macOS launchd)

The templates in `launchd/` use `$USER` and `$REPO` placeholders. Render them to your real
paths and load them. The core timer is the **dispatcher** (round-robins one enabled persona per
fire):

```bash
export REPO="$PWD"
mkdir -p ~/Library/LaunchAgents
for f in launchd/com.\$USER.cagent.*.plist; do
  out=~/Library/LaunchAgents/$(basename "$f" | sed "s/\$USER/$USER/")
  sed "s|\$REPO|$REPO|g; s|\$USER|$USER|g" "$f" > "$out"
  launchctl load "$out"
done
launchctl list | grep cagent
```

Timers included: `dispatcher` (every :07/:37 — the heartbeat), `dailypush` (commit + push
state), `watchdog` (health check), `usage`/`usagemail` (token accounting). Start with just the
dispatcher if you prefer; add the others once it's stable.

To run one dispatch cycle by hand without waiting for the timer: `bin/cagentctl dispatch`.

## 8. Day-to-day operation

**Local dashboards:**

```bash
bin/cagentctl status       # mode, kill switch, last tick, backoff
bin/cagentctl recent       # latest ticks, quests, notes, email
bin/cagentctl goals        # active goals per persona
bin/cagentctl mail         # inbound mail + read status
bin/cagentctl sent         # outbound mail, newest first
bin/cagentctl personas     # enabled + draft personas, their mode and tag
bin/cagentctl usage        # token/cost per persona
```

**Kill switch:** `bin/cagentctl stop` halts the next tick (writes `var/STOP`);
`bin/cagentctl start` clears it. Add `--persona <name>` to pause/resume just one.

**Steer by email:** reply to the agent with a command line. Commands are authenticated by the
shared `COMMAND_TOKEN` and can only *tighten* restrictions:

| Command | Effect |
|---|---|
| `!HELP` | reply with the full command list |
| `!STATUS` / `!PING` | operational snapshot / liveness check |
| `!GOALS` / `!GOAL <text>` / `!DROP-GOAL <id>` | list / add / archive a goal |
| `!FOCUS <topic>` | bias the next ticks toward a topic |
| `!FEEDBACK <text>` | record feedback into memory for later ticks |
| `!PAUSE` / `!PAUSE-ALL` | pause this / every persona (resume is local-only) |
| `!STOP-SENDING` | halt all outbound mail |
| `!QUIET <hours>` | mute outbound for N hours, then auto-clear |
| `!THROTTLE <n>` | lower today's send cap to n |
| `!NO-RESEARCH` | disable web research until cleared locally |

**Steer by git:** commit a directive file into `control/inbox/` and push — inject a goal, add a
steering note, pause a persona — authenticated by repository write access. See
[control/README.md](../control/README.md).

## Troubleshooting

- **`doctor` fails on auth / structured output** → the `claude` CLI isn't logged in or is an
  incompatible version. Fix `claude -p "hi" --output-format json` first.
- **`doctor` fails "constitution contains global CLAUDE.md"** → your global `~/.claude/CLAUDE.md`
  is bleeding into the agent. The constitution is supposed to override it; re-run and read the
  reply it prints. (This check is why the agent explicitly disowns your personal Claude context.)
- **Nothing is sent** → expected in `DRY_RUN`. Check `bin/cagentctl pending` (SUPERVISED) or the
  mode in `bin/cagentctl config`.
- **Mail not arriving** → verify the app password, and that non-LIVE mail is going to your
  `STAGING_RECIPIENT`, not `OWNER_EMAIL`.
- **A secret got staged for commit** → the pre-commit hook should have blocked it. If you
  bypassed hooks, rotate the credential immediately; the `.env` belongs only in
  `~/.config/cagent/`, never in the repo.
- **Run the tests** to confirm your checkout is intact: `./.venv/bin/pytest -q`. They run
  hermetically with no secrets and no network.
