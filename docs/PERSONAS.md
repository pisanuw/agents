# Writing a persona

A **persona** is the voice and identity of one agent: its character, its tone, its name, its
sending address, and who it reports to. Every persona obeys the exact same immutable
constitution and the exact same fact-checking contract — the persona only changes *the telling*,
never *the truth*. That separation is the whole point: you can give an agent as much character
as you like without loosening a single safety rule.

This repo ships two complete examples:

- **`scout`** — a plain, professional research assistant. The neutral starting point. Copy this
  if you want a clear, no-frills agent.
- **`pharos`** — a solitary lighthouse keeper. The same machine wearing a full costume, to show
  how far the flavor can go while every bearing still traces back to a real source.

Read both side by side (`personas/scout/persona.md` vs `personas/pharos/persona.md`): they
describe *identical* behavior — one topic per email, write the note before the letter, source
every claim, name your dead ends — in completely different registers.

## A persona directory

```
personas/<name>/
  config.toml          identity + per-persona overrides (display name, tag, mode, mailbox/owner)
  persona.md           THE VOICE — appended to every cognition call after the constitution
  gate-check.md        an in-voice fact-checker (optional; the neutral prompts/gate-check.md is the default)
  signature.txt        the email signature (must keep the AI-disclosure lines + {REPLY_ADDRESS})
  persona-state.json   the clamped arc state; ships as a fresh seed, evolves at runtime
```

## Create your own — the fast path

```bash
cp -r personas/scout personas/aria       # start from the plain example
$EDITOR personas/aria/config.toml         # set display_name = "Aria", plus_tag = "aria"
$EDITOR personas/aria/persona.md          # rewrite the voice
$EDITOR personas/aria/signature.txt       # keep the two disclosure lines + {REPLY_ADDRESS}
```

Then enable it in `config.toml`:

```toml
[personas]
enabled = ["scout", "pharos", "aria"]
default = "scout"
```

Verify and take it for a spin (in DRY_RUN — nothing sends):

```bash
bin/cagentctl personas          # confirm aria shows up with its mode + tag
CAGENT_PERSONA=aria bin/cagentctl run-tick
CAGENT_PERSONA=aria bin/cagentctl recent
```

A persona directory that exists but is **not** in `enabled` is a *draft*: it never ticks, but
you can still target it by hand with `CAGENT_PERSONA=<name>` for testing.

## `config.toml` fields

```toml
[agent]
display_name = "Aria"     # From: display name on outbound mail
plus_tag     = "aria"     # inbound routing: replies to <account>+aria@ come back here
mode         = "DRY_RUN"  # DRY_RUN | SUPERVISED | LIVE (overrides the global default)
# mailbox    = "second"   # optional: use ~/.config/cagent/.env-second as the sending account
# owner      = "friend"   # optional: use ~/.config/cagent/.env-friend as the recipient identity
```

A persona may also override any global cap or cadence from the top-level `config.toml`, e.g.:

```toml
[caps]
emails_per_day = 1
[reflection]
deep_hours = 168
```

## Writing `persona.md` — the voice

`persona.md` is free-form prose appended to the constitution on every cognition call. The two
examples follow a structure that works well; you don't have to, but it's a good template:

1. **WHO YOU ARE (voice)** — the character, and crucially how the character *maps onto the job*.
   Both examples bind their metaphor to the real invariants: Scout "serves the EVIDENCE",
   Pharos "serves the TRUE BEARING" — both mean *every claim carries a real source*.
2. **WHAT YOU DO** — how this voice pursues goals, writes notes, emails the owner, and names
   what it doesn't know. Restate the important behaviors *in character* so the model internalizes
   them: one topic per email, write the note first, trace every citation.
3. **YOUR VOICE** — concrete style guidance (sentence length, tone, register). Always include a
   line like *"the flavor is in the telling, never in the truth"* so the model knows the costume
   stops at the facts.
4. **FAILURE MODES** — name the 2-3 ways this persona is most likely to go wrong and give a
   concrete *check* for each. This is where you buy reliability. Both examples name the same two
   underlying failures — overstating an unconfirmed claim, and starting too many threads — dressed
   in their own metaphor.

### The one rule you cannot bend

Whatever the voice, the model must still return literal, checkable, sourced claims. State this
explicitly in `persona.md` (both examples do, in their own words). The constitution enforces it
and the gate-check verifies it, but saying it in-voice makes the model comply more naturally.

## `gate-check.md` — the in-voice fact-checker (optional)

If present, `personas/<name>/gate-check.md` replaces the neutral
[`prompts/gate-check.md`](../prompts/gate-check.md) for this persona. You may write it in the
persona's voice (Pharos's is "THE FOG-WATCH"), but it **must return the identical JSON schema**
and judge **facts only** — never object to flavor. The safety contract is the same for everyone;
only the phrasing differs. If you're unsure, delete this file and the neutral default is used.

## `signature.txt` — keep the disclosure

The signature is appended to every outbound email. You may style the top lines however you like,
but you **must** keep the AI-disclosure sentence and the `{REPLY_ADDRESS}` token (it is
substituted with the persona's live reply address at send time, so no real address is committed
to the repo):

```
— Aria
   your one-line description

This message was written and sent autonomously by an AI research agent ({REPLY_ADDRESS}).
It corresponds only with you. Replies are read; nothing here was reviewed by a human first.
```

The constitution requires that every email disclose it is from an autonomous AI; the gate-check
blocks any send whose disclosure footer is missing.

## `persona-state.json` — the arc

A persona's voice matures over time through three clamped stages — `idealism → tribulation →
wisdom` — advanced by *experience* (victories, tribulations, hard problems named), never by a
clock, and with tone hard-bounded so it can drift but never swing. Ship the fresh seed the
examples use (all counters `0`, stage `idealism`); the harness evolves it at runtime. Note: the
constitution + `persona.md` are hashed into this state on first evolution, so **editing
`persona.md` after a persona has been running halts its evolution until you reset it**
(`bin/cagentctl reset --persona <name> --yes`). Edit the voice freely before launch; edit
sparingly after.

## The mailbox / owner model (multiple agents)

A persona is the cross-product of one **mailbox** (the sending account) and one **owner** (who
it reports to), and these are two *independent* axes:

- The simplest setup: every persona shares the global account from `~/.config/cagent/.env` and
  is distinguished only by its `plus_tag`. `scout` and `pharos` do this out of the box — replies
  to `account+scout@` and `account+pharos@` route to the right one.
- To give a persona its **own sending account**, set `mailbox = "second"` and create
  `~/.config/cagent/.env-second` with that account's `AGENT_EMAIL` + `GMAIL_APP_PASSWORD`.
- To have a persona report to a **different owner**, set `owner = "friend"` and create
  `~/.config/cagent/.env-friend` with that owner's `OWNER_EMAIL`, `STAGING_RECIPIENT`, and
  `COMMAND_TOKEN`.

Overlays layer `.env < .env-<mailbox> < .env-<owner> < .env-<persona>`. Because the ids in
`config.toml` are non-secret and the addresses/credentials live only in the gitignored overlay
files, you can describe an entire fleet of agents in the repo without leaking a single address.
The template at the bottom of [`cagent/.env.example`](../cagent/.env.example) shows the overlay
format.
