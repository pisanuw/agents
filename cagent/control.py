"""Phase 6: the git control plane. The owner steers personas by committing directive
files into control/inbox/ (from the laptop, or the GitHub web UI) and pushing. The
always-on dispatcher pulls (git pull --rebase --autostash), routes each NEW directive to
its target persona, applies it, and archives the raw file under control/processed/<date>/.

Trust model: unlike the email-command channel (escalate-only, token-authenticated, because
email is spoofable), the git channel is authenticated by REPOSITORY WRITE ACCESS. Only the
owner can push to control/inbox/, so this channel MAY relax restrictions (resume a paused
persona, inject goals/instructions). It deliberately never touches MODE or autonomy level:
promotion to LIVE still requires local cagentctl, same as everywhere else.

Directive file formats (one directive per file in control/inbox/):
  * .json     -- a JSON object: {"persona": "...", "type": "...", "text": "...", "id"?: "..."}
  * .md/.txt  -- a "key: value" header block, a blank line, then a free-text body. The body
                 becomes `text` when no explicit `text:` key is given. Example:
                     persona: <name>
                     type: goal

                     Investigate the energy cost of large-scale model training.

Directive types:
  goal        -> goals.upsert in the persona's own state namespace (a new/updated quest)
  instruction -> a steering note surfaced in the persona's next tick context, and journaled
  note        -> journaled only (informational; does not steer cognition)
  pause       -> write var/persona/<persona>.STOP so the dispatcher skips it -- takes effect now
  resume      -> remove that STOP file -- takes effect now

Idempotency: a directive is applied once. Its id (explicit `id`, else a content hash) is
recorded in var/control_seen.jsonl (local, gitignored) AND the raw file is moved out of the
inbox into control/processed/, so a re-pull of the same content never re-applies it.
"""
from __future__ import annotations

import hashlib
import json
import subprocess

from cagent import atomicio, clock, config, gitpush, goals as goals_mod

CONTROL_INBOX = config.REPO_ROOT / "control" / "inbox"
CONTROL_PROCESSED = config.REPO_ROOT / "control" / "processed"
SEEN_LEDGER = config.REPO_ROOT / "var" / "control_seen.jsonl"   # local idempotency guard (gitignored)
PERSONA_STOP_DIR = config.REPO_ROOT / "var" / "persona"
PULL_TIMEOUT_S = 60

VALID_TYPES = {"goal", "instruction", "note", "pause", "resume"}


# --- pure parsing / routing (unit-tested, no I/O) ---------------------------------------

def parse_directive(raw: str, filename: str = "") -> dict | None:
    """Parse one directive file's text into a normalized {persona, type, text, id} dict, or
    None if it is unparseable or carries an unknown type. JSON if it looks like an object,
    otherwise a 'key: value' header block followed by a blank line and a free-text body."""
    raw = (raw or "").strip()
    if not raw:
        return None
    d: dict
    if raw.startswith("{"):
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(obj, dict):
            return None
        d = obj
    else:
        d = _parse_headers(raw)

    t = str(d.get("type", "")).strip().lower()
    if t not in VALID_TYPES:
        return None
    return {
        "persona": str(d.get("persona", "")).strip().lower(),
        "type": t,
        "text": str(d.get("text", "")).strip(),
        "id": (str(d.get("id")).strip() if d.get("id") else None),
    }


def _parse_headers(raw: str) -> dict:
    """`key: value` lines until the first blank line or first non-header line; the rest is body."""
    out: dict = {}
    lines = raw.splitlines()
    i = 0
    for i, ln in enumerate(lines):
        if ln.strip() == "":
            i += 1
            break
        if ":" not in ln:
            break
        k, v = ln.split(":", 1)
        out[k.strip().lower()] = v.strip()
    else:
        i = len(lines)
    body = "\n".join(lines[i:]).strip()
    if body and "text" not in out:
        out["text"] = body
    return out


def directive_id(d: dict, raw: str) -> str:
    return d.get("id") or hashlib.sha1(raw.strip().encode("utf-8")).hexdigest()[:16]


def target_persona(d: dict, enabled: list[str], default: str) -> str:
    """Resolve which persona a directive acts on: a named enabled persona, else the default."""
    p = (d.get("persona") or "").strip().lower()
    if p and p in enabled:
        return p
    return default


# --- per-persona paths ---------------

def queue_path(persona: str):
    """Per-persona control queue the dispatcher writes and the persona's tick drains. Resolved
    through the single validated Facade (config.state_root) rather than a hand-rolled copy."""
    return config.state_root(persona) / "control" / "queue.jsonl"


def stop_path(persona: str):
    return PERSONA_STOP_DIR / f"{persona}.STOP"


def is_paused(persona: str) -> bool:
    return stop_path(persona).exists()


# --- git pull + inbox processing (dispatcher side) --------------------------------------

def pull(log) -> bool:
    """git pull --rebase --autostash on the agent branch. Best-effort: a missing upstream or no
    network logs and returns False; it never raises, so the dispatcher always proceeds to run a
    tick. Refuses to pull when HEAD is off gitpush.EXPECTED_BRANCH, so a stray `git checkout` in
    this shared working tree is never rebased (and the operator gets a loud log line)."""
    ok, branch = gitpush.on_expected_branch()
    if not ok:
        log.info("control: NOT on %r (HEAD=%r); skipping git pull so a stray branch is not rebased",
                 gitpush.EXPECTED_BRANCH, branch or "(detached)")
        return False
    try:
        r = subprocess.run(
            ["git", "pull", "--rebase", "--autostash"],
            cwd=str(config.REPO_ROOT), capture_output=True, text=True, timeout=PULL_TIMEOUT_S)
        if r.returncode != 0:
            log.info("control: git pull --rebase failed (%s)",
                     (r.stderr or r.stdout).strip().splitlines()[-1:] or "")
            _abort_any_rebase(log)
        return r.returncode == 0
    except Exception as e:
        log.info("control: git pull error (continuing): %s", e)
        _abort_any_rebase(log)
        return False


def _abort_any_rebase(log) -> None:
    """A failed `git pull --rebase` (e.g. a conflict, expected on this two-host setup where both
    hosts commit state/ on main) leaves the working tree WEDGED mid-rebase. Abort it so the
    dispatcher's later tick + auto-push run against a clean HEAD instead of committing conflict
    markers / half-rebased state. `git rebase --abort` is a harmless no-op (non-zero, ignored) when
    no rebase is in progress, so this is safe to call after any pull failure."""
    try:
        ab = subprocess.run(["git", "rebase", "--abort"], cwd=str(config.REPO_ROOT),
                            capture_output=True, text=True, timeout=PULL_TIMEOUT_S)
        if ab.returncode == 0:
            log.info("control: aborted an in-progress rebase to restore a clean HEAD")
    except Exception as e:
        log.info("control: rebase --abort failed (continuing): %s", e)


def _load_seen() -> set[str]:
    return {d["id"] for d in atomicio.read_jsonl(SEEN_LEDGER) if isinstance(d, dict) and "id" in d}


def _record_seen(did: str, d: dict, persona: str) -> None:
    SEEN_LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with open(SEEN_LEDGER, "a") as f:
        f.write(json.dumps({"ts": clock.iso(), "id": did, "type": d["type"], "persona": persona}) + "\n")


def _enqueue(persona: str, d: dict) -> None:
    q = queue_path(persona)
    q.parent.mkdir(parents=True, exist_ok=True)
    with open(q, "a") as f:
        f.write(json.dumps({"ts": clock.iso(), "type": d["type"], "text": d["text"]}) + "\n")


def enqueue(persona: str, d: dict) -> None:
    """Public: queue a {type, text} directive for a persona's tick to drain (goal -> upsert,
    instruction -> steering, note -> journal). Used by the email-command channel to retarget a
    GOAL/FOCUS at another persona: it is applied in THAT persona's own tick, where its state
    namespace resolves correctly. Queuing for the CURRENT persona is drained this same tick
    (control.drain runs after command parsing), so a self-targeted FOCUS still biases now."""
    _enqueue(persona, d)


def _apply_routed(d: dict, persona: str, log) -> None:
    """pause/resume act immediately (scheduling is the dispatcher's domain); goal/instruction/
    note are enqueued to the persona's control queue and applied inside its own tick."""
    t = d["type"]
    if t == "pause":
        sp = stop_path(persona)
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text(f"paused by git control directive at {clock.iso()}\n")
        log.info("control: paused persona %s", persona)
    elif t == "resume":
        stop_path(persona).unlink(missing_ok=True)
        log.info("control: resumed persona %s", persona)
    else:
        _enqueue(persona, d)


def _archive(f, did: str) -> None:
    dest_dir = CONTROL_PROCESSED / clock.today()
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{did}-{f.name}"
    # Two same-named files that both REJECT on one day share did='rejected' -> the same dest; without
    # this, f.replace would silently clobber the earlier bounced file and lose it from the audit trail
    # (P2-1). Suffix a counter so each is preserved. Valid directives use a content-hash did, so they
    # only collide on identical content (a harmless re-archive).
    if dest.exists():
        base, suf = dest.stem, dest.suffix
        k = 1
        while dest.exists():
            dest = dest_dir / f"{base}-{k}{suf}"
            k += 1
    try:
        f.replace(dest)
    except Exception:
        f.unlink(missing_ok=True)


def process_inbox(enabled: list[str], default: str, log) -> list[dict]:
    """Read every new directive in control/inbox/, route+apply it, archive the raw file.
    Returns one summary dict per directive applied this cycle."""
    if not CONTROL_INBOX.exists():
        return []
    seen = _load_seen()
    out: list[dict] = []
    for f in sorted(CONTROL_INBOX.iterdir()):
        if not f.is_file() or f.name.startswith("."):
            continue
        try:
            raw = f.read_text()
        except UnicodeDecodeError:
            log.warning("control: non-UTF-8 file %s; archiving as rejected to unblock the queue", f.name)
            _archive(f, "rejected")
            continue
        d = parse_directive(raw, f.name)
        if d is None:
            log.info("control: unparseable/invalid directive %s; archiving as rejected", f.name)
            _archive(f, "rejected")
            continue
        did = directive_id(d, raw)
        if did in seen:
            _archive(f, did)   # already applied on a previous cycle; just clean it up
            continue
        persona = target_persona(d, enabled, default)
        if persona not in enabled:
            # Guard the silent black hole: an empty/misconfigured [personas].default (or a directive
            # naming no valid persona) would otherwise route to persona "" -- writing var/persona/.STOP
            # and a flat queue.jsonl no persona drains -- then mark it seen+archived, swallowing the
            # owner's directive with no error. Reject visibly instead so it can be re-issued.
            log.error("control: directive %s resolves to %r which is not an enabled persona "
                      "(named=%r, default=%r); archiving as REJECTED, not applied",
                      f.name, persona, d.get("persona"), default)
            _archive(f, "rejected")
            continue
        _apply_routed(d, persona, log)
        _record_seen(did, d, persona)
        seen.add(did)
        _archive(f, did)
        out.append({"id": did, "type": d["type"], "persona": persona})
    return out


# --- tick side: drain this persona's queue ----------------------------------------------

def _steering_path():
    return config.state_root() / "steering.jsonl"


def _append_steering(d: dict) -> None:
    p = _steering_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a") as f:
        f.write(json.dumps({"ts": clock.iso(), "text": d["text"]}) + "\n")


def recent_steering(n: int = 5) -> list[dict]:
    # atomicio.read_jsonl is the codebase's tolerant JSONL reader (skips torn lines); take the tail.
    return atomicio.read_jsonl(_steering_path())[-n:]


def drain(cfg, log) -> list[dict]:
    """Apply this persona's queued directives at the top of its tick: goal -> goals.upsert,
    instruction -> steering (surfaced in context) + journal, note -> journal only. The queue
    file is removed once drained. Inert no-op when nothing is queued."""
    q = config.state_root() / "control" / "queue.jsonl"
    items = atomicio.read_jsonl(q)
    applied: list[dict] = []
    for d in items:
        t = d.get("type")
        text = (d.get("text") or "").strip()
        if t == "goal" and text:
            goals_mod.upsert({"title": text[:120], "description": text},
                             rationale="owner directive via git control/inbox")
            applied.append({"type": "goal", "title": text[:60]})
        elif t == "instruction" and text:
            _append_steering(d)
            applied.append({"type": "instruction", "text": text[:60]})
        elif t == "note":
            applied.append({"type": "note", "text": text[:60]})
    _journal_directives(applied)
    q.unlink(missing_ok=True)
    if applied:
        log.info("control: drained %d directive(s): %s", len(applied), [a["type"] for a in applied])
    return applied


def _journal_directives(applied: list[dict]) -> None:
    if not applied:
        return
    journal = config.state_root() / "journal.jsonl"
    journal.parent.mkdir(parents=True, exist_ok=True)
    with open(journal, "a") as f:
        for a in applied:
            f.write(json.dumps({"ts": clock.iso(), "kind": "directive",
                                "summary": f"{a['type']}: {a.get('title') or a.get('text', '')}"}) + "\n")
