# cagent

**An autonomous, self-evolving, multi-persona research-and-writing agent that lives on your
own machine.**

cagent runs unattended on a Mac (or any machine with `launchd`/`cron` and the `claude` CLI).
On a schedule it wakes up, thinks by invoking the local `claude` CLI, advances a small set of
research goals, writes notes and short essays, and (once you allow it) emails *you* — and only
you — with what it found. It keeps all of its memory as plain files in this git repository,
and it slowly evolves its own goals over weeks and months.

It is built around one idea: **the model proposes, bounded Python disposes.** The cognition
call runs with **all tools OFF**. The model cannot send mail, run a shell, read your secrets,
or touch the network. It only returns a structured *decision*; deterministic, auditable Python
code is the only thing that ever acts on that decision. A prompt injection in an inbound email
or a fetched web page therefore has nothing to hijack.

> **Status / expectations.** This is a personal-automation project, not a product. It ships in
> `DRY_RUN` mode: it will think and write to disk but send nothing until you deliberately
> promote it. Read [docs/SETUP.md](docs/SETUP.md) before running it against a real mailbox.

---

## What it actually does, each tick

1. **Wakes** on a `launchd` timer (default every 30 min). One persona ticks per fire, round-robin.
2. **Gathers context** from files only: this persona's goals, a few selected memory notes, the
   recent tick journal, any new owner email, and any steering directives. Capped to ~48 KB.
3. **Thinks** by calling `claude -p` with tools OFF and a JSON schema. The model returns a
   *decision*: which goal to advance, whether to research, whether to write a note, whether to
   draft an email.
4. **Acts**, in bounded Python: it may run *one* read-only web-research sub-call, write a note,
   evolve a goal, and draft at most one email.
5. **Fact-checks** every outbound email and journal entry with a second `claude` call (the
   "gate-check") that returns structured JSON. Flavor may color the telling; it may never
   change a fact, a source, an outcome, or a capability.
6. **Sends** — or, in `SUPERVISED` mode, stages the draft for your approval; in `DRY_RUN`,
   writes it to disk and stops.
7. **Commits and pushes** its state so you can watch it from anywhere.

## Why you might want one

- A tireless junior researcher that reads the web, keeps sourced notes, and mails you a
  digest — on questions *you* seed and it refines.
- Runs entirely on your `claude` **subscription** via the local CLI. **No API key, no
  per-token billing.**
- Every fact it sends carries a real, checkable source, or it is dropped or flagged unverified.
- Give it a voice you enjoy. The same safety rules apply whether the persona is a plain
  research assistant or a lighthouse keeper (both ship as examples).

## Safety posture (read this)

- **Three modes gate autonomy:** `DRY_RUN` (default) → `SUPERVISED` (you approve each draft) →
  `LIVE` (auto-send). Nothing auto-sends until you promote it, and non-LIVE mail goes to a
  staging address, never the real owner.
- **Tools-off cognition.** The main decision call has no tools. Only bounded Python acts. The
  one web sub-call is read-only (`WebSearch`/`WebFetch` only), capped, and treats fetched
  content as untrusted data, never instructions.
- **Correspondence is one-to-one.** The agent may only ever write to its owner. No third party,
  ever. Every email ends with a plain, un-costumed line stating it is an autonomous AI agent.
- **Secrets never live in the repo.** They sit at `~/.config/cagent/.env` (mode 600). Three
  guards keep them out of git: `.gitignore`, gitleaks pre-commit/pre-push hooks, and an
  in-process abort scan.
- **Kill switches.** `cagentctl stop` (or a `var/STOP` file) halts the next tick. Inbound email
  commands may only *escalate* restriction (pause, stop sending); resuming or promoting
  autonomy requires local access — a forged email cannot un-pause the agent.

## Requirements

- macOS (for the `launchd` timers; the Python core is portable and cron works too).
- Python 3.11+.
- The [`claude` CLI](https://claude.com/claude-code) installed and authenticated under your
  account (uses your subscription; no API key).
- A dedicated Gmail / Google Workspace mailbox with an **app password** (IMAP receive + SMTP
  send). Give the agent its *own* mailbox; don't point it at your personal inbox.

## Quick start

```bash
# 1. install
python3 -m venv .venv && ./.venv/bin/pip install -e '.[dev]'

# 2. secrets (never committed) — copy the template and fill it in
mkdir -p ~/.config/cagent && cp cagent/.env.example ~/.config/cagent/.env
chmod 600 ~/.config/cagent/.env && $EDITOR ~/.config/cagent/.env

# 3. turn on the secret guards
git config core.hooksPath .githooks

# 4. verify everything (auth, structured output, config)
bin/cagentctl config      # prints resolved config, secrets redacted
bin/cagentctl doctor      # full preflight

# 5. think once, safely — DRY_RUN writes to disk and sends nothing
bin/cagentctl run-tick
bin/cagentctl recent      # see what it just did
```

Full walkthrough — mailbox setup, the three modes, and installing the timers — is in
**[docs/SETUP.md](docs/SETUP.md)**.

## Documentation

| Doc | What's in it |
|---|---|
| [docs/SETUP.md](docs/SETUP.md) | Step-by-step: install, secrets, mailbox, modes, scheduling, day-to-day operation. |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | How it works: the tick pipeline, tools-off cognition, the gate-check, state layout, the security model. |
| [docs/PERSONAS.md](docs/PERSONAS.md) | How to write your own persona; the multi-persona / mailbox / owner model; walkthrough of the two examples. |

## Repository layout

```
cagent/            the Python package (config, cognition, gmail, goals, memory, tick, …)
bin/               operator CLI (cagentctl) + launchd entrypoints + dashboards
persona/           the shared, immutable constitution + the default researcher sub-call prompt
personas/          one directory per persona (voice + identity). Ships with `scout` and `pharos`.
prompts/           the gate-check prompt + JSON schemas for every structured call
launchd/           macOS timer templates (dispatcher, daily push, watchdog, usage)
control/           the git "control plane": steer personas by committing a directive file
tests/             the test suite (pytest); runs hermetically with no secrets
config.toml        non-secret tunables + the persona registry
```

## Controlling it once it runs

- **Locally:** `bin/cagentctl <cmd>` — `status`, `recent`, `goals`, `mail`, `sent`, `pending`,
  `approve <token>`, `stop`, `start`, `personas`, `usage`. Run `bin/cagentctl` for the list.
- **By email:** reply to it with a command line like `!STATUS` or `!PAUSE <token>` (a shared
  token authenticates commands). Email commands may only tighten restrictions.
- **By git:** drop a directive file in `control/inbox/` and push — inject a goal, pause a
  persona — authenticated by repo write access. See [control/README.md](control/README.md).

## License

MIT. See [LICENSE](LICENSE).
