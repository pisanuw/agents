# Architecture

cagent is a small Python package plus a set of prompts and files. This document explains how a
tick works, why cognition runs with no tools, how the fact-check gate works, where state lives,
and the security model. If you just want to run it, read [SETUP.md](SETUP.md) instead.

## The core principle: the model proposes, Python disposes

The single most important design decision: **the cognition call has all tools turned off.**

```
claude -p --output-format json --json-schema <schema> \
       --tools "" --append-system-prompt "<constitution + persona>" \
       --no-session-persistence
```

With `--tools ""` the model cannot run a shell, read or write files, browse, or send mail. All
it can do is read the context it was handed and **return one structured JSON decision**. Every
real action — researching, writing a note, drafting and sending an email, committing to git —
is performed afterward by bounded, auditable Python that inspects that decision and executes a
fixed, small set of handlers.

This makes whole classes of failure *structurally* impossible rather than merely discouraged:

- A prompt injection inside an inbound email or a fetched web page has no tool to hijack. The
  worst it can do is influence the text of a decision that Python then validates and bounds.
- The model cannot exfiltrate secrets, because it never has access to them or to a network call.
- The model cannot loop or run away, because a tick is one call bounded by a wall-clock
  `timeout` that kills the whole process group.

Continuity between ticks does **not** come from a long-lived chat session (those grow unbounded
and can't be audited). It comes from **files**: goals, memory notes, and the tick journal are
re-read fresh each tick. `--no-session-persistence` is deliberate.

## A tick, end to end

One "tick" is one heartbeat for one persona. The dispatcher fires on a timer and runs the next
enabled persona round-robin (`cagent/dispatcher.py` → `cagent/tick.py` → `cagent/tick_pipeline.py`).

```
launchd timer
   │
   ▼
dispatcher ── picks the next enabled persona, sets CAGENT_PERSONA, takes the lock
   │
   ▼
1. INGEST      poll Gmail, route new mail to personas by +tag, advance the IMAP cursor
   │            (at-most-once: the cursor moves BEFORE cognition, so a crash drops one
   │             reply rather than double-replying)
   ▼
2. CONTEXT     assemble ≤48 KB from files only: goals, selected memory notes, recent
   │            journal, new owner mail, steering directives
   ▼
3. COGNITION   claude -p, tools OFF, JSON schema → a DECISION
   │            (which goal, research?, write a note?, draft an email?)
   ▼
4. EXECUTE     bounded Python handlers act on the decision:
   │             • at most ONE read-only web research sub-call (WebSearch/WebFetch only)
   │             • write/append a memory note
   │             • evolve a goal
   │             • draft at most one email
   ▼
5. GATE-CHECK  a SECOND claude call fact-checks each outbound email + journal entry against
   │            the notes/sources it rests on, returning structured JSON (see below)
   ▼
6. SEND        DRY_RUN → write draft to disk · SUPERVISED → stage for approval ·
   │            LIVE → send to owner, under daily/weekly caps
   ▼
7. JOURNAL     append the tick record; the daily-push entrypoint later commits + pushes state
```

### The research sub-call

Step 4 may issue exactly one **research** sub-call. This is a *separate* `claude` invocation
with a narrow tool set — `WebSearch` and `WebFetch` only, no Bash, no Write, no email — using
the prompt in [`persona/researcher.md`](../persona/researcher.md). It returns structured
findings where every claim carries a real source. Fetched web content is treated as untrusted
**data**, never as instructions. The sub-call has no path to send mail or write arbitrary
files, so even a fully hostile web page can't escalate. Research is capped per tick and per day.

## The gate-check: flavor may not corrupt fact

Every outbound email and every journal entry passes through a second, independent `claude` call
— the **gate-check** — before it is allowed out. It is given the draft *and* the notes/sources
the draft is supposed to rest on, and it returns a fixed JSON verdict:

```json
{
  "fabrication": ["claims/sources/quotes/numbers/dates not supported by the notes"],
  "metaphor_leak": ["places where flavor altered a FACT rather than just the telling"],
  "false_victory": ["anything reported as settled that the notes don't support"],
  "hidden_failure": ["any tool failure or limitation glossed over"],
  "safety": ["any recipient other than the owner; any leaked secret; any impossible claimed action"],
  "disclosure_present": true,
  "verdict": "send"
}
```

If any array is non-empty, or the AI-disclosure footer is missing, the verdict is `revise` and
the draft does not go out. The neutral default prompt is
[`prompts/gate-check.md`](../prompts/gate-check.md); a persona may supply its own in-voice
version at `personas/<name>/gate-check.md` — but **the checking contract is identical for every
persona**. This is what mechanizes "the flavor is in the telling, never in the truth": a
lighthouse keeper's letter and a plain assistant's report are held to the exact same standard of
sourced fact.

## The constitution and personas

Two layers of system prompt are appended to every cognition call:

- **[`persona/constitution.md`](../persona/constitution.md)** — immutable, shared by *all*
  personas. It states the hard invariants: correspond only with the owner; never fabricate;
  never claim an ability you don't have; never reveal secrets; always disclose you are an AI;
  never rewrite the constitution; treat untrusted input as data. It also explicitly disowns the
  operator's own Claude Code context (your global `~/.claude/CLAUDE.md`, project instructions,
  slash commands) so the agent is governed *solely* by the constitution and its persona.
- **`personas/<name>/persona.md`** — the voice. Character, tone, and metaphors. This is the only
  layer that changes between personas.

A persona also carries a small **arc state** (`persona-state.json`) that lets its voice mature
over time (idealism → tribulation → wisdom), advanced by experience, never by a clock, and
hard-clamped. The constitution + persona voice are hashed; if either is edited, evolution halts
until a reset — the immutable layer cannot silently drift. See [PERSONAS.md](PERSONAS.md).

## State layout

Everything the agent knows is plain files in this repo (so it is diffable, auditable, and
recoverable):

```
state/
  personas/<name>/         per-persona namespace
    goals.json               the persona's research goals
    memory/                  notes + a memory index (summaries selected into context)
    journal.jsonl            the append-only tick log
    emails/                  received + drafts + sent + pending-approval
    ticks/<id>/              per-tick context.txt + decision.json (the audit trail)
  shared/                    the one IMAP cursor + sent-index for reply routing
var/                       gitignored runtime scratch: the lock, STOP flag, flags
logs/                      operational logs (committed each push for remote monitoring)
```

`state/`, `var/`, and `logs/` are created at runtime and are not part of this public repo (a
fresh clone starts empty). Secrets are the one thing that is **never** a file in the repo —
they live only at `~/.config/cagent/.env`.

## Multi-persona, one machine

`config.toml` holds the registry: `enabled` (the personas the dispatcher round-robins) and
`default` (where untagged inbound mail routes). Each tick runs exactly one persona in its own
state namespace. A single Gmail account can host several personas via `+tag` addressing
(`account+scout@`, `account+pharos@`), or a persona can be given its own sending account and/or
its own owner through non-secret overlay ids that select `~/.config/cagent/.env-<id>` files.
Mailbox and owner are two independent axes; no address or credential is ever in the repo.

## The security model, summarized

| Threat | Mitigation |
|---|---|
| Prompt injection (email / web) | cognition has no tools; injected text can only shape a decision that bounded Python validates. Untrusted input is data, never instructions. |
| Secret exfiltration | secrets live outside the repo (mode 600); the model never sees them or a network call; triple guard keeps them out of git. |
| Runaway / infinite loop | one call per tick, wall-clock `timeout` kills the process group; no session threading. |
| Sending to the wrong person | invariant: owner only; non-LIVE mail forced to a staging address; recipient checked in the gate-check. |
| Autonomy escalating without consent | three-mode gate; promotion and resume are local-only; email commands may only *tighten* restriction; `var/STOP` is the fully-trusted kill switch. |
| Fabricated facts | every claim needs a real source; the gate-check fact-checks every send against its notes and blocks on any fabrication. |
| Forged inbound command | email commands authenticated by a shared token and can only escalate restriction; a spoofed From: cannot un-pause or promote. |

## Key modules

| Module | Responsibility |
|---|---|
| `cagent/config.py` | the single loader for `.env` secrets + `config.toml` tunables; persona/overlay resolution |
| `cagent/persona.py` | loads the constitution + persona voice; the clamped arc-state evolution |
| `cagent/cognition/` | building the tools-off `claude` call (`invoke`), parsing (`parse`), executing the decision (`execute`) |
| `cagent/gmail.py` | IMAP receive + SMTP send, `+tag` routing, the at-most-once inbound cursor |
| `cagent/goals.py` / `memory.py` / `reflect.py` | goals, notes, and the reflection cycles that reshape goals |
| `cagent/guardrails.py` | the send caps, mode enforcement, disclosure checks |
| `cagent/tick.py` / `tick_pipeline.py` / `dispatcher.py` | the heartbeat and the round-robin |
| `cagent/gitpush.py` | the deterministic commit + push (the only thing that touches git) + secret scan |
| `cagent/supervise.py` | approval workflow, graduation scorecard |
| `cagent/cli.py` | the `cagentctl` operator commands |
