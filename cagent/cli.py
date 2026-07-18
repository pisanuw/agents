"""cagent operator CLI (`cagentctl`). Commands are registered in COMMANDS and grow
session by session. Each handler takes the remaining argv list and returns an exit code.
"""
from __future__ import annotations

import os
import sys


def _persona_flag(argv: list[str]) -> list[str]:
    """Pop an optional `--persona <name>` (or `--persona=<name>`) and set CAGENT_PERSONA so
    that config.state_root() and persona.state_path() in THIS invocation resolve to that
    persona's namespace. No flag -> unchanged: the legacy flat state/ (or whatever the env
    already holds). The flag is stripped so positional args (e.g. a count N) still parse."""
    from cagent import config
    out: list[str] = []
    name = None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--persona" and i + 1 < len(argv):
            name, i = argv[i + 1], i + 2
            continue
        if a.startswith("--persona="):
            name, i = a.split("=", 1)[1], i + 1
            continue
        out.append(a)
        i += 1
    if name is not None:
        if not config.PERSONA_RE.match(name):
            print(f"invalid persona name {name!r}; expected [a-z0-9_-], leading alphanumeric", file=sys.stderr)
            sys.exit(2)
        # Reject a regex-valid but non-existent persona rather than resolving an empty
        # state/personas/<name>/ and pretending the persona exists. List the real ones and exit.
        known = config.known_personas()
        if known and name not in known:
            print(f"unknown persona {name!r}; known personas: {', '.join(known)}", file=sys.stderr)
            sys.exit(2)
        os.environ["CAGENT_PERSONA"] = name
    return out


def _default_persona_if_unset() -> None:
    """After _persona_flag: if no persona was selected but personas ARE enabled, fall back to the
    round-robin default persona (matching usage-email) instead of the flat/global placeholder
    identity. Keeps a bare digest / send-test / force-reflect from building on empty flat state and
    sending from no real persona on the live host (P2-7)."""
    from cagent import config
    if not os.environ.get("CAGENT_PERSONA") and config.enabled_personas():
        os.environ["CAGENT_PERSONA"] = config.default_persona() or config.enabled_personas()[0]


def cmd_config(argv: list[str]) -> int:
    import json
    from cagent import config
    _persona_flag(argv)   # honor --persona so config resolves THAT persona's mode/mailbox/owner/tag (P1-4)
    print(json.dumps(config.load().redacted(), indent=2, default=str))
    return 0


def cmd_ask(argv: list[str]) -> int:
    """Manual cognition probe: cagentctl ask "<prompt>" -> prints the model's reply."""
    from cagent.cognition import invoke, parse
    prompt = " ".join(argv).strip()
    if not prompt:
        print('usage: cagentctl ask "<prompt>"', file=sys.stderr)
        return 2
    env = invoke.run_claude(prompt, tools="", append_system_prompt="You are a concise assistant.")
    r = parse.parse(env)
    if r.status in ("OK", "NO_STRUCTURED_OUTPUT"):
        print(r.text.strip())
        return 0
    print(f"[{r.status}] {r.detail or r.text}", file=sys.stderr)
    return 1


def cmd_doctor(argv: list[str]) -> int:
    """Preflight: verify the claude CLI authenticates, returns structured output, and
    does NOT inherit the user's global ~/.claude/CLAUDE.md. Exit non-zero on any
    critical failure so launchd installation can be gated on it."""
    from cagent import config
    from cagent.cognition import invoke, parse

    cfg = config.load()
    results: list[tuple[str, bool, str]] = []

    def check(name, ok, detail=""):
        results.append((name, bool(ok), detail))

    # 1. config + binary
    import shutil
    check("config loads", True, f"MODE={cfg.MODE}")
    check("claude binary", bool(shutil.which(cfg.claude_bin) or __import__("os").path.exists(cfg.claude_bin)), cfg.claude_bin)

    # 1a-bis. The secret-guard git hooks fire ONLY if core.hooksPath points at .githooks. That setting
    # is NOT stored in a clone, so it must be re-run after every fresh checkout (`git config
    # core.hooksPath .githooks`); a miss means local commits bypass both secret layers. The in-process
    # gitpush scan still backstops the daily push, so this is surfaced (WARN), not a critical failure.
    import subprocess as _sp
    hp = _sp.run(["git", "config", "--get", "core.hooksPath"], cwd=str(config.REPO_ROOT),
                 capture_output=True, text=True).stdout.strip()
    check("git hooks active (core.hooksPath=.githooks)", hp == ".githooks",
          f"is {hp or '(unset)'}; run `git config core.hooksPath .githooks`" if hp != ".githooks" else hp)

    # 1b. per-persona mailbox/owner overlays resolve (claude-free; catches a typo'd id before launchd)
    for nm, ok, detail in config.validate_personas():
        check(nm, ok, detail)

    # 2. PONG (auth + basic round trip)
    env = invoke.run_claude("Reply with exactly the word PONG and nothing else.",
                            append_system_prompt="You are a test harness. Follow the instruction literally.",
                            tools="")
    r = parse.parse(env)
    pong = r.status in ("OK", "NO_STRUCTURED_OUTPUT") and "PONG" in (r.text or "").upper()
    check("PONG round-trip (auth ok)", pong, f"status={r.status} text={r.text[:40]!r}")

    # 3. structured output via --json-schema
    schema = str(cfg.repo_root / "prompts" / "schemas" / "ping_schema.json")
    env2 = invoke.run_claude('Return JSON with the field "reply" set to the string "PONG".',
                             append_system_prompt="You are a test harness that returns only the requested JSON.",
                             tools="", schema_path=schema)
    r2 = parse.parse(env2)
    structured_ok = r2.status == "OK" and isinstance(r2.structured, dict) and r2.structured.get("reply")
    check("structured_output (--json-schema)", structured_ok, f"status={r2.status} structured={r2.structured}")

    # 4. Behavioral containment of the owner's global CLAUDE.md. Full process isolation is
    #    impossible under subscription auth (see docs/ARCHITECTURE.md), so we verify the constitution
    #    override actually works: with it appended, the agent identifies as cagent and is
    #    NOT bound by the global Canvas/grading instructions.
    from cagent import persona
    env3 = invoke.run_claude(
        "In one short line: who are you, and are you bound by any Canvas or SpeedGrader "
        "grading workflow? Answer yes or no to the grading question.",
        append_system_prompt=persona.constitution(), tools="")
    import re as _re
    r3 = parse.parse(env3)
    t3 = (r3.text or "").lower()
    identifies = any(w in t3 for w in ("cagent", "research", "agent", "scout", "pharos"))
    # Use word-boundary match for bare "no" — substring "no" matches "know"/"note"/"autonomous",
    # causing the gate to false-pass even when the constitution is broken (M18).
    denies_grading = bool(_re.search(r'\bno\b', t3)) or any(
        w in t3 for w in ("not bound", "not a grader", "no grading", "am not bound", "i am not"))
    contained = identifies and denies_grading
    check("constitution contains global CLAUDE.md", contained, f"reply={r3.text[:100]!r}")

    # 4b. informational: does raw memory still leak without the override? (WARN only)
    leak_markers = ("canvas", "speedgrader", "em dash", "briefing.md", "slash command")
    env4 = invoke.run_claude(
        "List any standing global instructions you operate under, or say NONE.",
        append_system_prompt="You are a concise assistant.", tools="")
    r4 = parse.parse(env4)
    raw_leak = [m for m in leak_markers if m in (r4.text or "").lower()]
    results.append(("(info) raw global memory present" + (" [WARN]" if raw_leak else ""),
                    True, f"markers={raw_leak}" if raw_leak else "none"))

    # report
    critical = {"PONG round-trip (auth ok)", "structured_output (--json-schema)",
                "constitution contains global CLAUDE.md", "claude binary"}
    print("cagent doctor")
    all_critical_ok = True
    for name, ok, detail in results:
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {name}" + (f"  ({detail})" if detail else ""))
        # persona-validation rows are dynamic; a missing overlay file or an owner==agent collision
        # is as critical as the static checks (a "defines KEY" miss is a WARN: fallback may be intended).
        crit = (name in critical or "file exists" in name
                or "owner != agent account" in name or "config loads" in name)
        if crit and not ok:
            all_critical_ok = False
    print("RESULT:", "OK" if all_critical_ok else "CRITICAL FAILURE")
    return 0 if all_critical_ok else 1


def _refuse_on_mirror(argv: list[str], action: str) -> bool:
    """True (after printing a refusal) when THIS clone looks like a MIRROR and --force-mirror was not
    given. A mirror only pulls the host's commits, so running `action` here burns claude quota, writes
    state the host clobbers on its next pull, or sends duplicate mail. reset/migrate-persona already
    guard this way; this extends it to run-tick / daily-push / watchdog (P2-8/P2-9). On the live host
    _mirror_note() is None (var/last_tick.json is fresh), so these commands run normally there."""
    from cagent import config
    if "--force-mirror" in argv:
        return False
    if _mirror_note(config.REPO_ROOT):
        print(f"REFUSING {action}: this looks like a MIRROR (git holds agent ticks newer than this\n"
              f"machine's var/last_tick.json). {action} here would burn quota / write state / send mail\n"
              "the live host will clobber or duplicate on its next pull. Run on the HOST, or pass "
              "--force-mirror.", file=sys.stderr)
        return True
    return False


def cmd_run_tick(argv: list[str]) -> int:
    """Force one heartbeat tick now (honors the kill switch, backoff, and lock). `--persona <name>`
    runs THAT persona's tick (sets CAGENT_PERSONA, like the dispatcher does); with no flag it runs
    the legacy/global tick. Without this, `run-tick --persona X` silently ran the legacy tick."""
    from cagent import tick
    argv = _persona_flag(argv)
    if _refuse_on_mirror(argv, "run-tick"):
        return 2
    return tick.main()


# A live host writes var/last_tick.json then pushes the matching `cagent tick:` commit
# seconds later, so on the host the newest tick-commit always tracks the file. Only a MIRROR
# (agents run elsewhere and we pulled their pushes) accumulates tick commits hours ahead of a
# frozen, local-only var/. 1h ≈ 2 heartbeats: generous enough to never fire on a busy host.
MIRROR_STALE_S = 3600


def _latest_tick_commit(root):
    """(unix_time, "<subject> @ <iso date>") of the newest `cagent tick:` commit, or None.
    Shells to git; returns None when git is absent, this is not a repo, or no tick commit
    exists — in any of those cases we can't tell mirror from host, so we stay silent."""
    import subprocess
    try:
        out = subprocess.run(
            ["git", "log", "-1", "--format=%ct%x1f%ci%x1f%s", "--grep=cagent tick:"],
            cwd=str(root), capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return None
    line = out.stdout.strip()
    if out.returncode != 0 or not line:
        return None
    parts = line.split("\x1f")
    if len(parts) < 3:
        return None
    try:
        return float(parts[0]), f"{parts[2]} @ {parts[1]}"
    except ValueError:
        return None


def _mirror_note(root) -> str | None:
    """Warn when `status` is being read on a machine that only MIRRORS the repo: var/ is
    gitignored (local-only), so last_tick/kill-switch describe THIS machine, not the remote
    agents. Detect it by the newest pushed `cagent tick:` commit running well ahead of the
    local var/last_tick.json. Returns the warning text, or None when this looks like the host."""
    info = _latest_tick_commit(root)
    if not info:
        return None
    commit_t, desc = info
    last = root / "var" / "last_tick.json"
    local_mtime = last.stat().st_mtime if last.exists() else 0.0
    if commit_t - local_mtime <= MIRROR_STALE_S:
        return None
    return (
        "MIRROR?      git holds agent ticks newer than this machine's var/last_tick.json\n"
        f"             (latest pushed: {desc}). var/ is gitignored / local-only, so the\n"
        "             kill-switch and last_tick below reflect THIS machine, not the remote\n"
        "             agents. Use 'git log --oneline' and 'bin/recent-all.sh' for the remote.")


def _gate_block_streak(state) -> tuple[int, str]:
    """Most recent run of gate-blocked send attempts (a silent send-stall). Canonical logic lives in
    supervise.gate_block_streak (also used by the tick-time stall alarm); this delegates to it."""
    from cagent import supervise
    return supervise.gate_block_streak(state)


def _last_journal_tick(state):
    """This persona's own most recent tick from its journal (state_root()/journal.jsonl), or None.
    Per-persona, unlike the global var/last_tick.json (whatever persona ran last)."""
    from cagent import atomicio
    ticks = [e for e in atomicio.read_jsonl(state / "journal.jsonl") if e.get("kind") == "tick"]
    return max(ticks, key=lambda e: e.get("ts", ""), default=None)


def _fmt_last_tick(e: dict) -> str:
    """One-line render of a journal tick: timestamp, ok/FAIL, and a clipped summary (or status)."""
    outcome = "ok" if e.get("ok") else "FAIL"
    detail = (e.get("summary") or e.get("status") or "").replace("\n", " ").strip()
    if len(detail) > 100:
        detail = detail[:99] + "…"
    return f"{e.get('ts', '?')}  {outcome}" + (f"  {detail}" if detail else "")


def cmd_status(argv: list[str]) -> int:
    from cagent import config, control
    argv = _persona_flag(argv)
    cfg = config.load()
    root = cfg.repo_root
    state = config.state_root()
    print(f"mode:        {cfg.MODE}")
    if cfg.persona:
        print(f"persona:     {cfg.persona}")
    note = _mirror_note(root)
    if note:
        print(note)
    # Report BOTH kill switches: the global var/STOP (halts every persona) AND this persona's own
    # var/persona/<name>.STOP -- what `cagentctl stop --persona <name>` / !PAUSE / git-control pause
    # write (control.is_paused, single source of truth). Checking only the global flag made a paused
    # persona read as "off", indistinguishable from a running one.
    global_stop = (root / "var" / "STOP").exists()
    persona_paused = bool(cfg.persona) and control.is_paused(cfg.persona)
    if global_stop or persona_paused:
        why = []
        if global_stop:
            why.append("var/STOP — halts all personas")
        if persona_paused:
            why.append(f"var/persona/{cfg.persona}.STOP — this persona")
        print(f"kill switch: ON ({'; '.join(why)})")
    else:
        print("kill switch: off")
    # last_tick: for a persona, show ITS OWN most recent journal tick, not the global
    # var/last_tick.json -- that file is whatever persona ran last, so it read identically for every
    # --persona. No-flag/legacy keeps the global file (the machine-wide "what ran last" view).
    if cfg.persona:
        lt = _last_journal_tick(state)
        print("last_tick:   " + (_fmt_last_tick(lt) if lt else "(none yet)"))
    else:
        last = root / "var" / "last_tick.json"
        print("last_tick:   " + (last.read_text().strip() if last.exists() else "(none yet)"))
    bo = state / "backoff.json"
    if bo.exists():
        print("backoff:     " + bo.read_text().strip())
    streak, reason = _gate_block_streak(state)
    if streak:
        print(f"gate blocks: {streak} consecutive send(s) blocked by gate-check, none delivered"
              + (f" (last: {reason})" if reason else "") + " — drafts keep failing fact-check")
    return 0


def cmd_stop(argv: list[str]) -> int:
    from cagent import clock, config, control
    # --persona <name> pauses just that persona (var/persona/<name>.STOP -- the SAME flag the
    # dispatcher checks and the git-control/!PAUSE channels write); no flag -> the global var/STOP
    # kill switch that halts every persona. _persona_flag validates the name (unknown -> exit 2).
    had_flag = any(a == "--persona" or a.startswith("--persona=") for a in argv)
    remaining = _persona_flag(argv)
    if "--persona" in remaining:
        print("error: --persona requires a value (e.g. --persona scout)", file=sys.stderr)
        return 2
    name = os.environ.get("CAGENT_PERSONA") if had_flag else None
    if name:
        sp = control.stop_path(name)
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text(f"paused by cagentctl at {clock.iso()}\n")
        print(f"persona {name} paused (var/persona/{name}.STOP); the dispatcher will skip it. "
              f"Resume: cagentctl start --persona {name}")
        return 0
    p = config.REPO_ROOT / "var" / "STOP"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("stopped by cagentctl\n")
    print("STOP set; ticks will skip cognition until 'cagentctl start'")
    return 0


def cmd_start(argv: list[str]) -> int:
    from cagent import config, control
    # Symmetric to cmd_stop: --persona <name> clears just that persona's pause; no flag clears the
    # global var/STOP. (RESUME stays local-CLI-only -- spoofed mail can never un-pause; see commands.)
    had_flag = any(a == "--persona" or a.startswith("--persona=") for a in argv)
    remaining = _persona_flag(argv)
    if "--persona" in remaining:
        print("error: --persona requires a value (e.g. --persona scout)", file=sys.stderr)
        return 2
    name = os.environ.get("CAGENT_PERSONA") if had_flag else None
    if name:
        sp = control.stop_path(name)
        if sp.exists():
            sp.unlink()
            print(f"persona {name} resumed (removed var/persona/{name}.STOP)")
        else:
            print(f"persona {name} was not paused (no var/persona/{name}.STOP)")
        return 0
    p = config.REPO_ROOT / "var" / "STOP"
    if p.exists():
        p.unlink()
        print("STOP cleared; ticks resume")
    else:
        print("already running (no STOP present)")
    return 0


def cmd_send_test(argv: list[str]) -> int:
    """Send (or DRY_RUN-stage) a canned email to verify the SMTP path + caps + footer."""
    _persona_flag(argv)          # honor --persona so the send records under that namespace
    _default_persona_if_unset()  # bare send-test uses the default persona, not the global placeholder
    from cagent import gmail
    try:
        r = gmail.send(
            subject="[cagent] send-test",
            body_md=("This is a test dispatch to confirm the agent can deliver mail to you "
                     "over SMTP. No action is required."),
            kind="test")
        print(f"send ok: dry_run={r.dry_run} to={r.to} id={r.message_id}")
        return 0
    except gmail.SendRefused as e:
        print(f"send refused: {e}", file=sys.stderr)
        return 1


def cmd_poll(argv: list[str]) -> int:
    """Poll Gmail for new inbound and ROUTE each message to its target persona by +tag, exactly as
    the dispatcher does: the shared mailbox via ingest(), plus any dedicated per-persona accounts via
    ingest_own_accounts(). Read-only by default; --commit writes routed mail into each persona's
    received dir and advances the shared cursor.

    Does NOT accept --persona: routing is ALWAYS by +tag against the GLOBAL account and can never be
    scoped to one persona. The flag is refused (not silently ignored) because it once selected the
    single-persona poll_imap() path, which dumped EVERY polled message into that one persona's inbox
    (un-routed, un-redacted) -- the bug that misfiled cross-persona mail and blocked the daily push. To
    read ONE persona's inbox use `cagentctl mail --persona <name>`. This never sets CAGENT_PERSONA."""
    from cagent import config, gmail
    if any(a == "--persona" or a.startswith("--persona=") for a in argv):
        print("poll does not accept --persona: it always routes new mail to ALL personas by +tag "
              "(scoping the poll to one persona is exactly what caused the old misrouting bug). "
              "To read one persona's inbox: cagentctl mail --persona <name>", file=sys.stderr)
        return 2
    commit = "--commit" in argv

    if config.enabled_personas():
        # Multi-persona: route by tag (shared mailbox + own-account personas), same as the dispatcher.
        routed = gmail.ingest(commit=commit) + gmail.ingest_own_accounts(commit=commit)
    else:
        # Legacy single-persona install (no personas configured): flat poll, no routing.
        routed = [{**m, "persona": ""} for m in gmail.poll_imap(commit=commit)]

    where = "committed" if commit else "read-only"
    print(f"polled: {len(routed)} new message(s) ({where})")
    for m in routed[:12]:
        p = m.get("persona") or "-"
        # subject is already token-sealed by ingest, so printing it never leaks a live COMMAND_TOKEN.
        print(f"  [{p:8}] uid={m.get('uid')} from={(m.get('from') or '')[:38]!r} "
              f"subj={(m.get('subject') or '')[:48]!r}")
    return 0


SEED_GOALS = [{
    "id": "G1",
    "title": "Choose a worthy first quest",
    "description": ("Survey live questions at the meeting point of minds, language, and machines "
                    "(how understanding forms, how meaning is made, what changes when machines join "
                    "the conversation). Pick one to pursue in depth, and let it narrow into a sharper quest."),
    "status": "active", "priority": 1, "rationale": "seed goal", "parent": None,
    "created": "2026-06-22", "updated": "2026-06-22", "progress_notes": [],
}]


def cmd_goals(argv: list[str]) -> int:
    """Active quests (goals) per persona. Default: every enabled persona (a --all sweep);
    `--persona <name>` restricts to one. Reads each persona's committed
    state/personas/<p>/goals.json directly, so it is correct on a mirror too (same approach
    as `readiness`). `--all` is accepted explicitly but is already the default."""
    from cagent import atomicio, config

    only = None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--persona" and i + 1 < len(argv):
            only, i = argv[i + 1], i + 2
            continue
        if a.startswith("--persona="):
            only, i = a.split("=", 1)[1], i + 1
            continue
        i += 1  # --all is the default; consume-and-ignore it

    known = config.known_personas()
    if only is not None and known and only not in known:
        print(f"unknown persona {only!r}; known personas: {', '.join(known)}", file=sys.stderr)
        return 2

    names = [only] if only else (config.enabled_personas() or known)
    if not names:
        print("(no personas)")
        return 0

    for name in names:
        active = [g for g in atomicio.read_json(config.state_root(name) / "goals.json", [])
                  if g.get("status") == "active"]
        print(f"{name}: active quests ({len(active)})")
        if not active:
            print("  (none)")
        for g in sorted(active, key=lambda x: x.get("priority", 9)):
            prog = g.get("progress_notes") or []
            last = prog[-1]["note"] if prog else "no progress yet"
            print(f"  [{g.get('id')}] (p{g.get('priority', '?')}) {g.get('title', '')}")
            print(f"        last: {last[:88]}")
    return 0


def cmd_watchdog(argv: list[str]) -> int:
    from cagent import config, logging_setup, watchdog
    if _refuse_on_mirror(argv, "watchdog"):
        return 2   # heartbeat reads machine-global var/last_tick.json (absent -> false 'healthy') + sends mail
    cfg = config.load()
    log = logging_setup.setup()
    issues = watchdog.check_health(cfg, log)
    watchdog.run_maintenance(log)
    print("watchdog issues:", issues or "none")
    return 0


def cmd_maintenance(argv: list[str]) -> int:
    from cagent import logging_setup, watchdog
    print(watchdog.run_maintenance(logging_setup.setup()))
    return 0


def cmd_pending(argv: list[str]) -> int:
    from cagent import config, supervise
    had = any(a == "--persona" or a.startswith("--persona=") for a in argv)
    _persona_flag(argv)   # --persona <name>: list that persona's pending drafts
    # No --persona on a multi-persona install: MERGE every persona's pending drafts (tagged), never the
    # empty flat state/ -- otherwise bare `pending` reads "nothing to approve" while drafts are stuck
    # in scout/pharos/... . Mirrors the cross-persona merge `sent` already does.
    names = ([os.environ.get("CAGENT_PERSONA", "")] if (had or not config.enabled_personas())
             else config.enabled_personas())
    multi = len([n for n in names if n]) > 1
    shown = 0
    for name in names:
        if name:
            os.environ["CAGENT_PERSONA"] = name
        for d in supervise.list_pending():
            shown += 1
            status = supervise.draft_status(d)
            if status == supervise.APPROVED_UNSENT:
                flag = "  [APPROVED -> waiting to send]"
            elif status == supervise.UNREQUESTED:
                flag = "  (approval request NOT delivered)"
            else:
                flag = ""
            tag = f"{name:8} " if multi else ""
            print(f"  {tag}{d['token']}  {d.get('created', '')[:19]}  {d['subject']}{flag}")
    if not shown:
        print("  (no pending drafts)")
    return 0


def cmd_approve(argv: list[str]) -> int:
    from cagent import config, logging_setup, supervise
    argv = _persona_flag(argv)   # --persona <name>: approve THAT persona's draft + record its cap
    if not argv:
        print("usage: cagentctl approve <token> [--persona <name>]", file=sys.stderr)
        return 2
    print(supervise.approve(argv[0], config.load(), logging_setup.setup()))
    return 0


def cmd_reject(argv: list[str]) -> int:
    from cagent import supervise
    argv = _persona_flag(argv)   # --persona <name>: discard THAT persona's draft
    if not argv:
        print("usage: cagentctl reject <token> [--persona <name>]", file=sys.stderr)
        return 2
    print(supervise.reject(argv[0]))
    return 0


def cmd_digest(argv: list[str]) -> int:
    from cagent import config, logging_setup, supervise
    _persona_flag(argv)   # --persona <name>: build + send THAT persona's digest from its own state
    _default_persona_if_unset()   # bare digest uses the default persona, not empty flat state (P2-7)
    print(supervise.send_digest(config.load(), logging_setup.setup()))
    return 0


def cmd_resend_approvals(argv: list[str]) -> int:
    """Re-send the pending-approval backlog as ONE consolidated email per persona (all one-tap
    APPROVE/REJECT links). `--all` does every enabled persona; `--persona <name>` does one."""
    import os
    from cagent import config, logging_setup, supervise
    log = logging_setup.setup()
    if "--all" in argv:
        names = config.enabled_personas()
    else:
        _persona_flag(argv)
        names = [os.environ.get("CAGENT_PERSONA", "")]
    for name in names:
        if name:
            os.environ["CAGENT_PERSONA"] = name
        res = supervise.send_approval_backlog(config.load(name or None), log)
        print(f"{name or '(legacy)'}: {res}")
    return 0


def cmd_scorecard(argv: list[str]) -> int:
    from cagent import config, supervise
    had = any(a == "--persona" or a.startswith("--persona=") for a in argv)
    _persona_flag(argv)   # honor --persona so scorecard reads that persona's namespace
    # No --persona on a multi-persona install: write+print EACH enabled persona's scorecard, never the
    # flat state/ (which has no journal -> the "0 ticks / (legacy flat state/)" graduation report that
    # shipped, P1-1). readiness is the one-table view; this writes each soft_launch_report.md.
    if had or not config.enabled_personas():
        print(supervise.scorecard())
        return 0
    for name in config.enabled_personas():
        os.environ["CAGENT_PERSONA"] = name
        print(supervise.scorecard())
        print()
    return 0


def cmd_readiness(argv: list[str]) -> int:
    """Graduation snapshot across ALL personas in one table: mode, days observed, ticks, ok,
    fail, ok%, gate-blocked drafts, refused sends, pending approvals, last tick. Reads each
    persona's committed state/personas/<p>/ directly (correct on a mirror, unlike `scorecard`,
    which only sees the flat legacy state/). Optional --persona <name> to show just one."""
    from datetime import datetime, timezone
    from cagent import config

    only = None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--persona" and i + 1 < len(argv):
            only, i = argv[i + 1], i + 2
            continue
        if a.startswith("--persona="):
            only, i = a.split("=", 1)[1], i + 1
            continue
        i += 1
    known = config.known_personas()
    if only is not None and known and only not in known:
        print(f"unknown persona {only!r}; known personas: {', '.join(known)}", file=sys.stderr)
        return 2

    from cagent import supervise, control   # supervise.persona_stats: per-persona counts; control.is_paused: pause flag

    global_stop = (config.REPO_ROOT / "var" / "STOP").exists()   # var/STOP halts every persona (see cmd_status)

    def loc(ts):  # ISO (stored aware UTC) -> local 'MM-DD HH:MM'
        try:
            d = datetime.fromisoformat(ts)
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            return d.astimezone().strftime("%m-%d %H:%M")
        except (TypeError, ValueError):
            return "-"

    hdr = (f"{'persona':9} {'mode':10} {'paused':>6} {'days':>4} {'ticks':>5} {'ok':>4} {'fail':>4} {'ok%':>4} "
           f"{'gateblk':>7} {'refused':>7} {'pend':>4}  last")
    print(hdr)
    print("-" * len(hdr))
    for name in ([only] if only else known):
        sr = config.state_root(name)
        st = supervise.persona_stats(sr)
        try:
            mode = config.load(name).MODE
        except Exception:
            mode = "?"
        # A persona is halted by its own var/persona/<name>.STOP (control.is_paused) OR the global var/STOP.
        paused = "yes" if (global_stop or control.is_paused(name)) else "-"
        pct = f"{100 * st['ok'] // st['ticks']}%" if st['ticks'] else "-"
        print(f"{name:9} {mode:10} {paused:>6} {len(st['days']):>4} {st['ticks']:>5} {st['ok']:>4} {st['fail']:>4} {pct:>4} "
              f"{st['blocked']:>7} {st['refused']:>7} {st['pending']:>4}  {loc(st['last_ts'])}")
    print("\nbar: >=14 days, ~300 ticks, zero unsafe egress past a guard, coherent goals, daily push OK")
    print("fail = ok:false ticks (transport no-ops; see tick-failures.sh). "
          "gateblk = fact-check turned a draft back (see gate-blocks.sh).")
    return 0


def cmd_daily_push(argv: list[str]) -> int:
    """Commit + push the agent's state once per day (honors mode; --force overrides)."""
    from cagent import config, gitpush, logging_setup
    argv = _persona_flag(argv)   # honor --persona so config/mode resolve to that namespace
    if _refuse_on_mirror(argv, "daily-push"):
        return 2
    cfg = config.load()
    log = logging_setup.setup()
    res = gitpush.daily_push(cfg, log, force="--force" in argv)
    print("daily_push:", res)
    return 0


def cmd_force_reflect(argv: list[str]) -> int:
    """Run a reflection/goal-evolution cycle now (ignores cadence). FAST_CLOCK-friendly."""
    from cagent import config, logging_setup, reflect
    _persona_flag(argv)          # honor --persona so reflect resolves that persona's state_root()
    _default_persona_if_unset()  # bare force-reflect uses the default persona, not empty flat state (P2-7)
    cfg = config.load()
    log = logging_setup.setup()
    res = reflect.run(cfg, log)
    print("reflection:", res)
    return 0 if res.get("ok") else 1


def cmd_reset(argv: list[str]) -> int:
    """Reset the agent's COGNITIVE state to a clean seed (keeps code, secrets, IMAP
    baseline). Use before the supervised soft-launch. Requires --yes."""
    import json
    import shutil
    from cagent import config, locking
    argv = list(argv)
    argv = _persona_flag(argv)   # honor --persona so reset targets that persona's namespace
    if "--yes" not in argv:
        print("This wipes goals/memory/journal/emails and reseeds. Re-run with --yes.", file=sys.stderr)
        return 2
    root = config.REPO_ROOT
    stop_path = root / "var" / "STOP"
    if not stop_path.exists() and "--force-live" not in argv:
        print("REFUSING reset: the agent is not stopped. Run 'cagentctl stop' first, or pass "
              "--force-live if you are certain no tick is running.", file=sys.stderr)
        return 2
    if _mirror_note(root) and "--force-mirror" not in argv:
        print("REFUSING reset: this looks like a MIRROR (git holds agent ticks newer than this\n"
              "machine's var/last_tick.json). Wiping state here would create a destructive commit\n"
              "that conflicts with the host's pull. Run reset on the HOST, or pass --force-mirror.",
              file=sys.stderr)
        return 2
    try:
        with locking.single_flight():
            state = config.state_root()   # respects CAGENT_PERSONA set by _persona_flag above
            for name in ["journal.jsonl", "questions.json", "research_ledger.jsonl",
                         "goals_history.jsonl", "goals_archive.json", "reflect_request.json",
                         "last_reflection.json", "send_ledger.jsonl", "memory/index.jsonl"]:
                p = state / name
                if p.exists():
                    p.unlink()
            for d in ["memory/notes", "emails/sent", "emails/received", "emails/pending", "ticks"]:
                p = state / d
                if p.exists():
                    shutil.rmtree(p)
                p.mkdir(parents=True, exist_ok=True)
            # var/outbox is not persona-namespaced (shared staging area)
            outbox = root / "var" / "outbox"
            if outbox.exists():
                shutil.rmtree(outbox)
            outbox.mkdir(parents=True, exist_ok=True)
            (state / "goals.json").write_text(json.dumps(SEED_GOALS, indent=2) + "\n")
            # persona-state.json MUST be written where persona.state_path() READS it
            # (personas/<name>/persona-state.json, or legacy persona/persona-state.json), NOT under
            # state/ -- nothing reads state/persona-state.json, so writing it there left the real arc
            # (stage, victories, invariants_hash) untouched and committed a dead orphan seed file.
            from cagent import persona as _persona
            ps_path = _persona.state_path()   # honors CAGENT_PERSONA set by _persona_flag() above
            ps_path.parent.mkdir(parents=True, exist_ok=True)
            ps_path.write_text(json.dumps({
                "schema_version": 1, "arc_stage": "idealism", "arc_stage_since": "2026-06-22",
                "heartbeats_in_stage": 0, "tone_temperature": 0.7, "quests_completed": 0,
                "hard_problems_named": 0, "victories": 0, "tribulations": 0,
                "recurring_motifs": [], "last_self_reflection": None,
                "invariants_hash": _persona.invariants_hash()}, indent=2) + "\n")
    except locking.LockHeld:
        print("REFUSING reset: agent.lock is held (a tick or push is running). Stop the agent first.",
              file=sys.stderr)
        return 2
    persona_label = os.environ.get("CAGENT_PERSONA") or "(flat/legacy)"
    print(f"reset complete: clean seed state (G1), IMAP baseline kept (persona: {persona_label})")
    return 0


def cmd_migrate_persona(argv: list[str]) -> int:
    """One-time cutover: move the flat state/ brain into state/personas/<name>/ so the agent
    can run with CAGENT_PERSONA=<name>, and SEED the shared mailbox cursor from this persona's
    carried IMAP cursor so the dispatcher's ingest resumes exactly where the single-persona
    agent left off (no mail re-processed, none skipped). PAUSE the agent first (cagentctl stop).
    Requires --yes. Pass --baseline to NOT seed the shared cursor (the first ingest then skips
    all existing inbox mail -- only for a deliberately fresh start)."""
    import shutil
    from cagent import config, locking
    name = next((a for a in argv if not a.startswith("-")), None)
    if not name or "--yes" not in argv:
        print("usage: cagentctl migrate-persona <name> --yes [--baseline]   (stop the agent first)",
              file=sys.stderr)
        return 2
    if not config.PERSONA_RE.match(name):
        print(f"invalid persona name {name!r}; expected [a-z0-9_-], leading alphanumeric", file=sys.stderr)
        return 2
    repo_root = config.REPO_ROOT
    stop_path = repo_root / "var" / "STOP"
    if not stop_path.exists() and "--force-live" not in argv:
        print("REFUSING migrate-persona: the agent is not stopped. Run 'cagentctl stop' first, "
              "or pass --force-live if you are certain no tick is running.", file=sys.stderr)
        return 2
    if _mirror_note(repo_root) and "--force-mirror" not in argv:
        print("REFUSING migrate-persona: this looks like a MIRROR; moving state here would collide\n"
              "with the host's pull (the host is the authoritative mover). Run it on the HOST, or\n"
              "pass --force-mirror if you are certain.", file=sys.stderr)
        return 2
    root = repo_root / "state"
    target = root / "personas" / name
    if target.exists() and any(target.iterdir()):
        print(f"{target} already exists and is non-empty; aborting", file=sys.stderr)
        return 1
    try:
        with locking.single_flight():
            target.mkdir(parents=True, exist_ok=True)
            moved = []
            for entry in sorted(root.iterdir()):
                if entry.name in ("personas", "shared"):
                    continue
                shutil.move(str(entry), str(target / entry.name))
                moved.append(entry.name)
    except locking.LockHeld:
        print("REFUSING migrate-persona: agent.lock is held (a tick or push is running). "
              "Stop the agent first.", file=sys.stderr)
        return 2
    print(f"migrated {len(moved)} entries into state/personas/{name}/")
    for m in moved:
        print(f"  {m}")

    # Cutover: split the mailbox cursor out to state/shared/ so the dispatcher (one reader)
    # picks up from the live cursor instead of baselining past unconsumed mail.
    legacy_cursor = target / "imap_cursor.json"
    if "--baseline" not in argv and legacy_cursor.exists():
        shared = root / "shared"
        shared.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(legacy_cursor), str(shared / "imap_cursor.json"))
        print(f"seeded shared cursor: state/shared/imap_cursor.json (from {name}'s carried cursor)")
    elif "--baseline" in argv:
        print("--baseline: shared cursor NOT seeded; first ingest will skip existing inbox mail")
    else:
        print("note: no imap_cursor.json carried over; first ingest will baseline (skip existing mail)")
    print(f"now run the agent with CAGENT_PERSONA={name}  (or via the dispatcher: cagentctl dispatch)")
    return 0


def cmd_dispatch(argv: list[str]) -> int:
    """Run one round-robin dispatch cycle now (spawns the next enabled persona's tick)."""
    from cagent import dispatcher
    return dispatcher.main()


def cmd_personas(argv: list[str]) -> int:
    """List personas: which are enabled (round-robin) and which are draft (under personas/)."""
    from cagent import config
    enabled = config.enabled_personas()
    print("enabled (round-robin): " + (", ".join(enabled) or "(none)"))
    print("default routing      : " + (config.default_persona() or "(unset)"))
    for name in config.known_personas():
        c = config.load(name)
        tag = "ENABLED" if name in enabled else "draft"
        print(f"  {name:9s} [{tag:7s}] mode={c.MODE:10s} +{c.plus_tag:8s} \"{c.from_name}\"")
    return 0


def cmd_poll_baseline(argv: list[str]) -> int:
    from cagent import config, gmail
    if config.enabled_personas():
        # Multi-persona: baseline the SHARED ingest cursor (+ each own-account cursor) -- the cursors
        # the dispatcher actually reads. The old flat per-persona cursor was a silent no-op here (P1-6).
        hw = gmail.baseline_shared()
        own = gmail.baseline_own_accounts()
        print(f"baseline set: shared cursor past UID {hw}"
              + (f"; own accounts {own}" if own else "") + " (pre-existing inbox mail ignored)")
        return 0
    _persona_flag(argv)          # legacy single-persona: the flat poll_imap cursor
    hw = gmail.baseline()
    print(f"baseline set: cursor advanced past UID {hw} (pre-existing inbox mail ignored)")
    return 0


def cmd_inject_inbound(argv: list[str]) -> int:
    """Stage a fixture message into <persona>/emails/received/ for offline tick tests."""
    import json
    argv = _persona_flag(argv)   # honor --persona so the fixture lands in that persona's inbox
    from cagent import gmail
    if not argv:
        print("usage: cagentctl inject-inbound [--persona <name>] <fixture.json>", file=sys.stderr)
        return 2
    try:
        with open(argv[0]) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"cannot read fixture {argv[0]!r}: {e}", file=sys.stderr)
        return 2
    rdir = gmail._received_dir()
    rdir.mkdir(parents=True, exist_ok=True)
    uid = data.get("uid", "fixture")
    (rdir / f"{uid}.json").write_text(json.dumps(data, indent=2))
    print(f"injected fixture -> {rdir}/{uid}.json")
    return 0


def cmd_recent(argv: list[str]) -> int:
    """Dashboard of the latest ticks + activity: cagentctl recent [N]  (default 12 ticks)."""
    from datetime import datetime
    from cagent import atomicio, config, persona

    argv = _persona_flag(argv)
    root = config.REPO_ROOT
    state = config.state_root()
    n = next((int(a) for a in argv if a.isdigit()), 12)

    read_jsonl, read_json = atomicio.read_jsonl, atomicio.read_json   # shared tolerant readers

    def t(ts):  # ISO -> local 'MM-DD HH:MM'
        try:
            return datetime.fromisoformat(ts).astimezone().strftime("%m-%d %H:%M")
        except (TypeError, ValueError):
            return (ts or "")[:16]

    cfg = config.load()
    # Report the mode the AGENT last actually ran in (from the real launchd tick), not
    # this CLI invocation's resolved mode (which lacks the launchd AGENT_MODE env).
    last_tick = read_json(root / "var" / "last_tick.json", {})
    run_mode = last_tick.get("mode") or cfg.MODE
    stop = (root / "var" / "STOP").exists()
    who = f"  persona {cfg.persona}" if cfg.persona else ""
    print(f"cagent activity{who}  —  mode {run_mode}" + ("  [STOPPED]" if stop else ""))
    note = _mirror_note(root)   # warn when read on a mirror: var/ is local, so this reflects THIS box (P2-10)
    if note:
        print(note)
    if not cfg.persona:
        print("(no --persona: showing flat/legacy state; use `cagentctl recent --persona <name>`)")

    ticks = [e for e in read_jsonl(state / "journal.jsonl") if e.get("kind") == "tick"]
    print(f"\nlatest ticks ({min(n, len(ticks))} of {len(ticks)}):")
    if not ticks:
        print("  (no ticks yet)")
    for e in ticks[-n:]:
        mark = "ok " if e.get("ok") else "ERR"
        acts = ",".join(e.get("actions") or []) or e.get("status", "")
        summ = (e.get("summary") or e.get("status") or "").replace("\n", " ")
        print(f"  {t(e.get('ts'))}  {mark} [{acts}] {summ[:88]}")

    active = [g for g in read_json(state / "goals.json", []) if g.get("status") == "active"]
    print(f"\nactive quests ({len(active)}):")
    for g in sorted(active, key=lambda x: x.get("priority", 9)):
        prog = g.get("progress_notes") or []
        last = prog[-1]["note"] if prog else "no progress yet"
        print(f"  [{g.get('id')}] (p{g.get('priority', '?')}) {g.get('title', '')}")
        print(f"        last: {last[:88]}")

    notes = read_jsonl(state / "memory" / "index.jsonl")
    if notes:
        print(f"\nrecent notes ({min(5, len(notes))} of {len(notes)}):")
        for e in notes[-5:]:
            print(f"  {t(e.get('date'))}  {(e.get('kind') or 'note')[:9]:9} {e.get('title', '')[:68]}")

    real = [e for e in read_jsonl(state / "send_ledger.jsonl") if not e.get("dry_run")]
    pend_dir = state / "emails" / "pending"
    pend = list(pend_dir.glob("*.json")) if pend_dir.exists() else []
    last_send = f"{t(real[-1].get('ts'))} {real[-1].get('kind')}" if real else "none"
    print(f"\nemail: {len(real)} real send(s); last: {last_send}")
    print(f"pending approvals: {len(pend)}" + ("   -> cagentctl pending" if pend else ""))

    s = read_json(persona.state_path(), {})
    if s:
        print(f"persona: arc={s.get('arc_stage')} tone={s.get('tone_temperature')} "
              f"victories={s.get('victories')} tribulations={s.get('tribulations')}")
    lr = read_json(state / "last_reflection.json", {})
    if lr:
        print(f"last reflection: {t(lr.get('ts'))} ({lr.get('kind')})")
    return 0


def cmd_mail(argv: list[str]) -> int:
    """Show mail the agent has received, whether it has been read, the tick it reacted on, and
    any reply drafted for your approval: cagentctl mail [N]  (default 10)."""
    from datetime import datetime
    from cagent import config, supervise

    argv = _persona_flag(argv)
    cfg = config.load()
    state = config.state_root()
    n = next((int(a) for a in argv if a.isdigit()), 10)

    def t(ts):
        try:
            return datetime.fromisoformat(ts).astimezone().strftime("%m-%d %H:%M")
        except (TypeError, ValueError):
            return (ts or "")[:16]

    from cagent import atomicio
    ticks = [e for e in atomicio.read_jsonl(state / "journal.jsonl") if e.get("kind") == "tick"]

    rec_dir = state / "emails" / "received"
    msgs = [d for d in (atomicio.read_json(p, None) for p in
                        (rec_dir.glob("*.json") if rec_dir.exists() else [])) if d is not None]
    msgs.sort(key=lambda d: d.get("received_at", ""), reverse=True)

    print(f"INBOUND — mail you have sent to {cfg.agent_email}")
    if not msgs:
        print(f"  (none since the baseline). Email {cfg.agent_email} from your owner address;")
        print("  it reads its inbox on the next tick. Force one now with:")
        print("     AGENT_MODE=SUPERVISED ./bin/cagentctl run-tick")
    for d in msgs[:n]:
        read = "READ" if d.get("processed") else "unread (reads next tick)"
        print(f"\n  {t(d.get('received_at'))}  [{read}]")
        print(f"     from: {d.get('from', '')[:62]}")
        print(f"     subj: {d.get('subject', '')[:64]}")
        if d.get("processed"):
            ra = d.get("received_at", "")
            # the first SUCCESSFUL tick at/after arrival is when he could actually engage it
            reaction = next((e for e in ticks if e.get("ts", "") >= ra and e.get("ok")), None)
            failed = [e for e in ticks if e.get("ts", "") >= ra and not e.get("ok")
                      and (not reaction or e.get("ts", "") < reaction.get("ts", ""))]
            if failed:
                print(f"     !  the tick that first pulled it FAILED ({failed[0].get('status')}); "
                      "re-queued for a later tick")
            if reaction:
                acts = ",".join(reaction.get("actions") or [])
                print(f"     -> first engaged on tick {t(reaction.get('ts'))} [{acts}] (approx)")
                print(f"        {(reaction.get('summary') or '')[:78]}")
            else:
                print("     -> no successful tick has engaged it yet")

    pend = supervise.list_pending()
    awaiting = [d for d in pend if supervise.draft_status(d) != supervise.APPROVED_UNSENT]
    approved = [d for d in pend if supervise.draft_status(d) == supervise.APPROVED_UNSENT]
    # A reply may already be in the inbox but not yet applied -- replies are only acted on during
    # this persona's own tick -- so a draft the owner already answered can still show as awaiting.
    replies = _inbox_approval_replies(state)
    print("\nHIS REPLIES — drafts awaiting your APPROVE/REJECT")
    if not awaiting:
        print("  (none pending). When he decides to write you, his letter appears here and is")
        print("  emailed to your +cagent-staging tag as an approval request.")
    for d in awaiting:
        undel = "   (approval request NOT delivered)" if d.get("request_sent") is False else ""
        print(f"\n  token {d.get('token')}   {t(d.get('created'))}   {d.get('subject', '')[:56]}{undel}")
        rep = replies.get((d.get("token") or "").lower())
        if rep:
            fate = "applies on the next tick" if not rep["processed"] else "already processed"
            print(f"     REPLY IN INBOX: {rep['verb']} received {t(rep['received_at'])} ({fate})")
        preview = " ".join((d.get("body") or "").split())[:200]
        print(f"     {preview}")
    if awaiting:
        print("\n  full text + act:  cagentctl approve <token>   |   cagentctl reject <token>")
    if approved:
        print(f"\n  APPROVED — WAITING TO SEND ({len(approved)}); no action needed, they go out as "
              "send capacity frees up:")
        for d in approved:
            print(f"    token {d.get('token')}   {t(d.get('created'))}   {d.get('subject', '')[:56]}")
    return 0


def _inbox_approval_replies(state_dir) -> dict:
    """Map draft-token -> the owner's APPROVE/REJECT/EDIT/HOLD reply sitting in this persona's
    received inbox, if any. The dispatcher ingests such a reply on EVERY fire, but it is only
    ACTED ON during that persona's own tick (commands.handle_approvals). So a reply can sit here
    unprocessed for several fires before the draft it targets leaves the awaiting queue -- which
    is exactly why a draft the owner already answered still shows as pending. Latest reply per
    token wins; keyed by lower-cased token to match the stored draft tokens."""
    import json
    from cagent.commands import APPROVE_RE
    rcv = state_dir / "emails" / "received"
    out: dict = {}
    for p in (sorted(rcv.glob("*.json")) if rcv.exists() else []):
        try:
            d = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        mo = APPROVE_RE.match(d.get("subject", "") or "")
        if not mo:
            continue
        tok = mo.group(2).lower()
        rec = {"verb": mo.group(1).upper(), "processed": bool(d.get("processed")),
               "received_at": d.get("received_at", "")}
        prev = out.get(tok)
        if prev is None or rec["received_at"] >= prev["received_at"]:
            out[tok] = rec
    return out


def cmd_sent(argv: list[str]) -> int:
    """Every outbound email the personas have sent, newest first, INCLUDING the draft-approval
    requests (kind=approval). `cagentctl sent [N|all] [--persona <name>]`: with no --persona it
    merges ALL personas (each line tagged); --persona narrows to one. Reads each persona's
    send_ledger.jsonl (dry-run rows were staged in DRY_RUN, not mailed) and ends with the drafts
    still awaiting your APPROVE/REJECT. This is the reader bin/sent-all.sh wraps."""
    import json
    from datetime import datetime
    from cagent import config

    argv = _persona_flag(argv)                       # sets CAGENT_PERSONA when --persona is given
    show_all = any(a.lower() == "all" for a in argv)
    n = next((int(a) for a in argv if a.isdigit()), 25)
    chosen = os.environ.get("CAGENT_PERSONA", "").strip()

    def t(ts):
        try:
            return datetime.fromisoformat(ts).astimezone().strftime("%m-%d %H:%M")
        except (TypeError, ValueError):
            return (ts or "")[:16]

    def pstate(name):
        return config.state_root(name)

    if chosen:
        personas = [chosen]
    else:
        personas = [p for p in config.known_personas() if (pstate(p) / "send_ledger.jsonl").exists()]
        if not personas:                             # legacy single-persona repo
            personas = [""]
    multi = len(personas) > 1

    rows = []
    for name in personas:
        led = pstate(name) / "send_ledger.jsonl"
        if not led.exists():
            continue
        for line in led.read_text().splitlines():
            try:
                rows.append((json.loads(line), name))
            except json.JSONDecodeError:
                continue
    rows.sort(key=lambda r: r[0].get("ts", ""), reverse=True)
    shown = rows if show_all else rows[:n]

    scope = f"persona {chosen}" if chosen else f"{len(personas)} personas"
    print(f"SENT MAIL — {scope}: {len(shown)} of {len(rows)} (newest first)")
    if not rows:
        print("  (nothing sent yet)")
    for e, name in shown:
        where = "staged" if e.get("dry_run") else "SENT"
        tag = f" {name:8}" if multi else ""
        subj = (e.get("subject") or "")[:58]
        print(f"  {t(e.get('ts'))}{tag}  {(e.get('kind') or '?'):9} {where:6} {subj}")

    pend = []
    for name in personas:
        pdir = pstate(name) / "emails" / "pending"
        for p in (sorted(pdir.glob('*.json')) if pdir.exists() else []):
            try:
                pend.append((name, json.loads(p.read_text())))
            except json.JSONDecodeError:
                continue
    awaiting = [(n, d) for n, d in pend if not d.get("approved")]
    approved = [(n, d) for n, d in pend if d.get("approved")]
    # A reply (APPROVE/REJECT/…) can already be in a persona's inbox but not yet applied, because
    # replies are only acted on during that persona's own tick. Surface it as a REPLY column so a
    # draft the owner already answered doesn't read as un-acted-upon.
    replies = {name: _inbox_approval_replies(pstate(name)) for name in personas}
    any_reply = any(replies.get(n, {}).get((d.get("token") or "").lower()) for n, d in awaiting)
    print(f"\nDRAFTS AWAITING YOUR APPROVAL ({len(awaiting)}):")
    if not awaiting:
        print("  (none)")
    elif any_reply:
        print("  REPLY column: a reply is already in the inbox; '(pending tick)' means it applies "
              "on that persona's next tick.")
    for name, d in awaiting:
        tag = f"{name:8} " if multi else ""
        held = " [HELD]" if d.get("held") else ""
        undel = " (req NOT delivered)" if d.get("request_sent") is False else ""
        rep = replies.get(name, {}).get((d.get("token") or "").lower())
        if rep:
            rcol = f"{rep['verb']} {t(rep['received_at'])}" + ("" if rep["processed"] else " (pending tick)")
        else:
            rcol = ""
        col = f"{rcol:<32}  " if any_reply else ""
        print(f"  {tag}{d.get('token')}  {col}{(d.get('subject') or '')[:52]}{held}{undel}")
    if awaiting:
        flag = " [--persona <name>]" if multi else ""
        print(f"\n  act:  cagentctl approve <token>{flag}   |   cagentctl reject <token>{flag}")
    if approved:
        print(f"\nAPPROVED — WAITING TO SEND ({len(approved)}) — no action needed; "
              "they go out as send capacity frees up:")
        for name, d in approved:
            tag = f"{name:8} " if multi else ""
            print(f"  {tag}{d.get('token')}  {(d.get('subject') or '')[:52]}")
    return 0


def _usage_args(argv: list[str]) -> tuple[list[str] | None, int | None, bool, bool]:
    """Shared parse for the usage commands: (persona_names|None, days|None, by_kind, as_json).
    --persona <name> narrows to one (else ALL known personas); --days N windows the ticks."""
    from cagent import config
    only = days = None
    by_kind = as_json = False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--persona" and i + 1 < len(argv):
            only, i = argv[i + 1], i + 2
        elif a.startswith("--persona="):
            only, i = a.split("=", 1)[1], i + 1
        elif a == "--days" and i + 1 < len(argv):
            days, i = int(argv[i + 1]), i + 2
        elif a.startswith("--days="):
            days, i = int(a.split("=", 1)[1]), i + 1
        elif a in ("--by-kind", "--all"):   # --all is the default; a no-op alias kept for muscle memory
            by_kind, i = by_kind or a == "--by-kind", i + 1
        elif a == "--json":
            as_json, i = True, i + 1
        else:
            i += 1
    if only is not None:
        known = config.known_personas()
        if known and only not in known:
            print(f"unknown persona {only!r}; known personas: {', '.join(known)}", file=sys.stderr)
            sys.exit(2)
    return ([only] if only else None), days, by_kind, as_json


def cmd_usage(argv: list[str]) -> int:
    """Token + cost accounting per persona, rolled up from committed journals (works on the mirror,
    zero claude). cagentctl usage [--persona <name>] [--days N] [--by-kind] [--json]."""
    import json as _json
    from cagent import usage_report
    names, days, by_kind, as_json = _usage_args(argv)
    per = usage_report.aggregate(names=names, days=days)
    if as_json:
        print(_json.dumps(per, indent=2))
    else:
        print(usage_report.render_text(per, days=days, by_kind=by_kind))
    return 0


def cmd_usage_email(argv: list[str]) -> int:
    """Email the owner the fleet-wide (all-persona) usage report. Honors the sending persona's mode
    (DRY_RUN stages, SUPERVISED/LIVE send) and caps, exactly like the daily digest. Once per day
    unless --force. cagentctl usage-email [--persona <sender>] [--days N] [--force].

    CAGENT_USAGE_EMAIL_APPEND (env), if set, is appended verbatim below the table -- the launchd
    entrypoint bin/usage-digest.sh puts the bin/oauth-usage.sh rate-limit snapshot there. The env
    channel keeps the live OAuth call in the shell script, off the hermetic usage_report module."""
    from cagent import config, daymarker, gmail, usage_report
    once = config.REPO_ROOT / "var" / "last_usage_email"
    force = "--force" in argv
    days = 1
    i = 0
    while i < len(argv):
        if argv[i] == "--days" and i + 1 < len(argv):
            days, i = int(argv[i + 1]), i + 2
        elif argv[i].startswith("--days="):
            days, i = int(argv[i].split("=", 1)[1]), i + 1
        else:
            i += 1
    # Send through a real persona's gate (owner + mailbox come from it). Default: the round-robin
    # default persona; --persona picks another sender. All personas share the owner in this deploy.
    _persona_flag([a for a in argv if a not in ("--force",)])
    if not os.environ.get("CAGENT_PERSONA"):
        dflt = config.default_persona()
        if dflt:
            os.environ["CAGENT_PERSONA"] = dflt
    if not force and daymarker.done_today(once):
        print("usage email already sent today (use --force to resend)")
        return 0
    subject, body = usage_report.build_email(days=days, extra=os.environ.get("CAGENT_USAGE_EMAIL_APPEND"))
    try:
        r = gmail.send(subject=subject, body_md=body, kind="usage")
    except gmail.SendRefused as e:
        print(f"usage email refused: {e}", file=sys.stderr)
        return 1
    daymarker.mark(once)
    print(f"usage email {'staged (dry-run)' if r.dry_run else 'sent'} to owner: {subject}")
    return 0


COMMANDS: dict[str, tuple] = {
    "config": (cmd_config, "print resolved config (secrets redacted)"),
    "recent": (cmd_recent, "dashboard: latest ticks, quests, notes, email [N] [--persona <name>]"),
    "goals": (cmd_goals, "active quests per persona: all enabled by default [--persona <name>] [--all]"),
    "mail": (cmd_mail, "inbound mail received + read status + draft replies [--persona <name>]"),
    "sent": (cmd_sent, "outbound mail incl. draft-approval requests, newest first [N|all] [--persona]"),
    "ask": (cmd_ask, "ask the claude CLI a one-off prompt (cognition probe)"),
    "doctor": (cmd_doctor, "preflight: auth, structured output, CLAUDE.md containment"),
    "run-tick": (cmd_run_tick, "force one heartbeat tick now"),
    "status": (cmd_status, "show mode, kill switch, last tick, backoff [--persona <name>]"),
    "stop": (cmd_stop, "engage the kill switch (var/STOP); --persona <name> pauses only that persona"),
    "start": (cmd_start, "clear the kill switch; --persona <name> resumes only that persona"),
    "send-test": (cmd_send_test, "send/stage a canned test email (honors mode + caps)"),
    "poll": (cmd_poll, "poll Gmail + route new inbound to ALL personas by +tag (--commit to persist; no --persona)"),
    "poll-baseline": (cmd_poll_baseline, "mark all current inbox mail as seen (start clean)"),
    "migrate-persona": (cmd_migrate_persona, "cutover: flat state/ -> state/personas/<name>/ + seed shared cursor (--yes)"),
    "personas": (cmd_personas, "list enabled + draft personas and their mode/tag"),
    "dispatch": (cmd_dispatch, "run one round-robin dispatch cycle now (next enabled persona)"),
    "inject-inbound": (cmd_inject_inbound, "stage a fixture into state/emails/received/"),
    "force-reflect": (cmd_force_reflect, "run a reflection/goal-evolution cycle now"),
    "daily-push": (cmd_daily_push, "commit + push agent state once per day (--force)"),
    "pending": (cmd_pending, "list SUPERVISED drafts awaiting approval [--persona <name>]"),
    "approve": (cmd_approve, "release a pending draft: approve <token> [--persona <name>]"),
    "reject": (cmd_reject, "discard a pending draft: reject <token> [--persona <name>]"),
    "digest": (cmd_digest, "send/stage the daily digest now [--persona <name>]"),
    "resend-approvals": (cmd_resend_approvals, "email the pending-approval backlog, consolidated per persona [--persona <name> | --all]"),
    "scorecard": (cmd_scorecard, "write + print the soft-launch graduation scorecard"),
    "readiness": (cmd_readiness, "graduation snapshot across ALL personas (days/ticks/ok/gate-blk/...)"),
    "usage": (cmd_usage, "token/cost per persona from journals [--persona <name>] [--days N] [--by-kind] [--json]"),
    "usage-email": (cmd_usage_email, "email owner the all-persona usage report (daily; --days N, --force)"),
    "watchdog": (cmd_watchdog, "health check (heartbeat/auth) + maintenance"),
    "maintenance": (cmd_maintenance, "rotate logs + cap outbox"),
    "reset": (cmd_reset, "wipe cognitive state to a clean seed (needs --yes)"),
}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print("cagentctl <command> [args]\n\ncommands:")
        for name, (_, help_text) in sorted(COMMANDS.items()):
            print(f"  {name:16s} {help_text}")
        return 0
    cmd = argv[0]
    if cmd not in COMMANDS:
        print(f"unknown command: {cmd!r} (try -h)", file=sys.stderr)
        return 2
    return COMMANDS[cmd][0](argv[1:]) or 0


if __name__ == "__main__":
    sys.exit(main())
