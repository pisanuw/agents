# control/ -- the git control plane

Steer the personas without shell access to the always-on machine. Drop a directive file in
`control/inbox/` (from the laptop, or directly in the GitHub web UI), commit, and push. The
dispatcher on the always-on machine pulls every cycle, applies each new directive, and moves
the raw file to `control/processed/<date>/` as an audit trail.

## Trust model

This channel is authenticated by **repository write access**: only someone who can push to
this repo can place a directive here. That is why it may do things the email channel refuses
(resume a paused persona, inject goals). It still cannot change a persona's MODE or promote it
to LIVE: that requires local `cagentctl` on the machine itself.

## Directive format

One directive per file. Two formats are accepted.

**JSON** (`*.json`):

```json
{ "persona": "<name>", "type": "goal", "text": "Investigate energy-efficient datacenter cooling." }
```

**Header + body** (`*.md` / `*.txt`) -- friendlier to write by hand:

```
persona: scout
type: instruction

Spend the next few ticks consolidating findings rather than opening new threads.
```

The body (after the blank line) becomes `text` when no explicit `text:` header is given.

### Fields

| field     | meaning                                                                       |
|-----------|-------------------------------------------------------------------------------|
| `persona` | target persona name (must be enabled). Omitted/unknown -> the default persona. |
| `type`    | one of `goal`, `instruction`, `note`, `pause`, `resume`.                       |
| `text`    | the payload (the goal text, the instruction, the note).                        |
| `id`      | optional stable id for idempotency. Omitted -> a content hash is used.          |

### Types

| type          | effect                                                                            |
|---------------|-----------------------------------------------------------------------------------|
| `goal`        | upserts a quest into the persona's goals (`text` is the title + description).      |
| `instruction` | a steering note surfaced at the top of the persona's next tick context.            |
| `note`        | recorded in the persona's journal only; does not steer cognition.                  |
| `pause`       | the dispatcher stops scheduling that persona (writes `var/persona/<persona>.STOP`). |
| `resume`      | clears the pause.                                                                  |

## Idempotency

Each directive is applied exactly once. Its id (explicit `id`, else a content hash) is recorded
locally, and the file is moved out of `inbox/`, so re-pulling the same content never re-applies
it. Editing a file's content makes it a new directive (new hash). To re-issue an identical
directive, give it a fresh `id`.
