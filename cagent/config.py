"""Single config loader: secrets from ~/.config/cagent/.env + tunables from config.toml.

AGENT_MODE env var overrides [agent].mode. Refuses to start in LIVE without the
Gmail app password. No other module parses the .env or config.toml.
"""
from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

ENV_PATH = Path(os.path.expanduser("~/.config/cagent/.env"))
REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_TOML = REPO_ROOT / "config.toml"

VALID_MODES = ("DRY_RUN", "SUPERVISED", "LIVE")
# One filename-safe id shape, shared by persona names AND mailbox/owner overlay refs (the id IS the
# .env-<id> suffix, e.g. "mailbox-1" -> ~/.config/cagent/.env-mailbox-1). PERSONA_RE / REF_RE are kept
# as named aliases so callers read intently, but they are the same pattern -- no divergence risk.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
PERSONA_RE = _NAME_RE
REF_RE = _NAME_RE


def state_root(persona: str | None = None) -> Path:
    """Root for this run's mutable state. With CAGENT_PERSONA set (or an explicit `persona` arg),
    state is namespaced under state/personas/<persona>/ (multi-persona layout); otherwise the legacy
    flat state/ directory (single-persona, byte-for-byte unchanged behavior). The env var is read at
    call time so each per-persona subprocess resolves its own namespace. The explicit `persona` arg
    lets one process (e.g. the watchdog) resolve another persona's namespace without mutating the
    environment."""
    persona = (persona or os.environ.get("CAGENT_PERSONA", "")).strip()
    if not persona:
        return REPO_ROOT / "state"
    if not PERSONA_RE.match(persona):
        raise ValueError(f"invalid persona {persona!r}; expected [a-z0-9_-], leading alnum")
    return REPO_ROOT / "state" / "personas" / persona


def shared_root() -> Path:
    """State shared across all personas: the one mailbox cursor, the sent-index for reply
    routing, and (Phase 5) the global send ledger. Not per-persona."""
    return REPO_ROOT / "state" / "shared"


def personas_state_root() -> Path:
    """The CONTAINER holding every persona's state namespace (state/personas/). This is the parent
    of each state_root(name); callers that iterate/glob all personas (watchdog prune, daily-push
    aggregation) resolve it here rather than re-spelling the literal so the layout has one owner."""
    return REPO_ROOT / "state" / "personas"


def _parse_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        v = v.strip()
        # Strip an inline `# comment` so a value copied verbatim from .env.example (which ships
        # `KEY=value  # note` lines) is not corrupted by the trailing note. An inline comment must be
        # preceded by whitespace (standard .env convention); a value that is ALL comment (a blank
        # template line) becomes empty; a quoted value is left verbatim (quote chars kept as data and
        # any trailing comment NOT stripped -- no cagent config uses the quoted form, template values
        # are all bare). Gmail app passwords are alnum groups separated by spaces, so ` #` never
        # occurs inside one.
        if v[:1] in ("'", '"'):
            pass
        elif v.startswith("#"):
            v = ""
        else:
            hidx = v.find(" #")
            if hidx != -1:
                v = v[:hidx].strip()
        env[k.strip()] = v
    return env


def _env_for(persona: str | None, mailbox: str = "", owner: str = "") -> dict[str, str]:
    """Resolve secrets by layering overlays onto the global ~/.config/cagent/.env, most-specific
    wins, in this order:

        .env  <  .env-<mailbox>  <  .env-<owner>  <  .env-<persona>

    MAILBOX and OWNER are two INDEPENDENT axes (a persona = one sending account x one owner),
    each selected by a non-secret id in personas/<name>/config.toml: the mailbox overlay holds the
    sending account + app password + IMAP/SMTP; the owner overlay holds the recipient address,
    display name, staging recipient, and command token. The optional per-persona overlay stays as
    a final escape hatch. Their key-sets are disjoint by convention, so mailbox-vs-owner order
    never actually collides. Every overlay lives beside the gitignored global .env, never in the
    repo, so neither the app password nor an owner's address is ever committed."""
    env = _parse_env(ENV_PATH)
    for ref in (mailbox, owner):
        if ref:
            p = ENV_PATH.parent / f".env-{ref}"
            if p.exists():
                env.update(_parse_env(p))
    if persona:
        overlay = ENV_PATH.parent / f".env-{persona}"
        if overlay.exists():
            env.update(_parse_env(overlay))
    return env


def _toml(path: Path) -> dict:
    return tomllib.loads(path.read_text()) if path.exists() else {}


def enabled_personas() -> list[str]:
    """Ordered list of personas the dispatcher round-robins. Empty in legacy single-persona mode."""
    return [str(p) for p in _toml(CONFIG_TOML).get("personas", {}).get("enabled", [])]


def default_persona() -> str:
    """Persona that receives untagged inbound mail (Phase 4 routing). Empty if unset."""
    return str(_toml(CONFIG_TOML).get("personas", {}).get("default", ""))


def known_personas() -> list[str]:
    """Every persona that EXISTS on disk (a directory under personas/), enabled or draft. This is
    the set a --persona flag may legitimately name; empty in a legacy single-persona repo with no
    personas/ dir. Sorted for stable listing."""
    pdir = REPO_ROOT / "personas"
    if not pdir.exists():
        return []
    return sorted(p.name for p in pdir.iterdir() if p.is_dir())


def _persona_toml(persona: str) -> dict:
    return _toml(REPO_ROOT / "personas" / persona / "config.toml") if persona else {}


@dataclass(frozen=True)
class Config:
    # --- secrets / connection (.env) ---
    agent_email: str
    from_name: str
    gmail_app_password: str
    imap_host: str
    imap_port: int
    smtp_host: str
    smtp_port: int
    owner_email: str
    owner_name: str
    staging_recipient: str
    command_token: str
    claude_bin: str
    # --- tunables (config.toml) ---
    MODE: str
    model_tick: str
    model_reflect: str
    emails_per_day: int
    emails_per_week: int
    research_per_tick: int
    research_per_day: int
    max_backlog_drafts: int         # outbound backpressure: stop admitting new drafts/research once this
                                    # many live drafts already wait in pending/ (approved-but-unsent + awaiting)
    global_emails_per_day: int      # GLOBAL ceiling across all personas (anti-flood)
    global_emails_per_week: int
    reflect_light_hours: int
    reflect_deep_hours: int
    context_byte_cap: int
    memory_notes: int
    tick_timeout_s: int
    ack_commands: bool      # email a one-line "applied: !PAUSE" / "refused: ..." confirmation back
    command_footer: bool    # append the "steer me by email" command menu (mailto links) to every send
    repo_root: Path
    # --- identity (per-persona, multi-persona layout) ---
    persona: str            # "" in legacy single-persona mode
    plus_tag: str           # mailbox +tag for routing; "" in legacy
    mailbox: str            # non-secret id of the .env-<id> account overlay; "" = global account
    owner: str              # non-secret id of the .env-<id> owner overlay; "" = global owner

    @property
    def recipient(self) -> str:
        """Where outbound mail actually goes: owner in LIVE, staging otherwise."""
        return self.owner_email if self.MODE == "LIVE" else self.staging_recipient

    def redacted(self) -> dict:
        d = {k: getattr(self, k) for k in (
            "agent_email", "from_name", "imap_host", "imap_port", "smtp_host",
            "smtp_port", "owner_email", "owner_name", "staging_recipient", "claude_bin", "MODE",
            "model_tick", "model_reflect", "emails_per_day",
            "persona", "plus_tag", "mailbox", "owner",
        )}
        d["gmail_app_password"] = f"<set:{len(self.gmail_app_password)}>" if self.gmail_app_password else "<missing>"
        d["command_token"] = "<set>" if self.command_token else "<unset>"
        return d


def load(persona: str | None = None) -> Config:
    if persona is None:
        persona = os.environ.get("CAGENT_PERSONA", "").strip()
    if persona and not PERSONA_RE.match(persona):
        raise ValueError(f"invalid persona {persona!r}; expected [a-z0-9_-], leading alnum")

    # Read the persona toml FIRST: it names the mailbox/owner overlay ids that select the env files,
    # so the ids must be known before resolving secrets.
    gtoml = _toml(CONFIG_TOML)
    ptoml = _persona_toml(persona)
    pre_agent = ptoml.get("agent", {})
    mailbox = str(pre_agent.get("mailbox", "")).strip()
    owner = str(pre_agent.get("owner", "")).strip()
    for label, ref in (("mailbox", mailbox), ("owner", owner)):
        if ref and not REF_RE.match(ref):
            raise ValueError(f"invalid {label} id {ref!r}; expected [a-z0-9_-], leading alnum")
    env = _env_for(persona, mailbox, owner)   # global .env < mailbox < owner < per-persona overlay

    def section(name: str) -> dict:
        merged = dict(gtoml.get(name, {}))
        merged.update(ptoml.get(name, {}))   # a persona's config.toml overrides the global default
        return merged

    agent = section("agent")
    caps = section("caps")
    refl = section("reflection")
    cog = section("cognition")
    cmds = section("commands")

    mode = os.environ.get("AGENT_MODE", agent.get("mode", "DRY_RUN")).upper()
    # Auto-downgrade tripwire: a LIVE agent that wrote mode_override demotes itself (per-persona).
    # Resolve against the SAME persona being loaded (state_root(persona)), not the ambient
    # CAGENT_PERSONA env, so config.load("x") from the operator/dispatcher shell checks x's own
    # namespace. Fail CLOSED: the file is only ever written to demote, so its mere existence drops
    # LIVE -> SUPERVISED even if its content is torn/empty; a recognized value may demote to DRY_RUN.
    override = state_root(persona) / "mode_override"
    if mode == "LIVE" and override.exists():
        ov = override.read_text().strip().upper()
        mode = ov if ov in ("SUPERVISED", "DRY_RUN") else "SUPERVISED"
    if mode not in VALID_MODES:
        raise ValueError(f"invalid AGENT_MODE {mode!r}; expected one of {VALID_MODES}")

    # Identity: a persona's display_name / plus_tag override the .env defaults.
    from_name = agent.get("display_name") or env.get("AGENT_FROM_NAME", "cagent")
    plus_tag = str(agent.get("plus_tag", persona))

    cfg = Config(
        agent_email=env.get("AGENT_EMAIL", "agent@example.com"),
        from_name=from_name,
        gmail_app_password=env.get("GMAIL_APP_PASSWORD", "").replace(" ", ""),
        imap_host=env.get("IMAP_HOST", "imap.gmail.com"),
        imap_port=int(env.get("IMAP_PORT", "993")),
        smtp_host=env.get("SMTP_HOST", "smtp.gmail.com"),
        smtp_port=int(env.get("SMTP_PORT", "465")),
        # Owner identity is env-ONLY (the owner overlay selected by config.toml [agent].owner), never
        # read from the repo: a structural guarantee that an owner's address is never committed. The
        # send gate (gmail.py) hard-locks each persona to ITS OWN owner/staging, never a free list.
        owner_email=env.get("OWNER_EMAIL", "owner@example.com"),
        owner_name=env.get("OWNER_NAME", ""),
        staging_recipient=env.get("STAGING_RECIPIENT", "owner+cagent-staging@example.com"),
        command_token=env.get("COMMAND_TOKEN", ""),
        claude_bin=env.get("CLAUDE_BIN", "claude"),
        MODE=mode,
        model_tick=agent.get("model_tick", "sonnet"),
        model_reflect=agent.get("model_reflect", "opus"),
        emails_per_day=int(caps.get("emails_per_day", 3)),
        emails_per_week=int(caps.get("emails_per_week", 10)),
        research_per_tick=int(caps.get("research_per_tick", 1)),
        research_per_day=int(caps.get("research_per_day", 6)),
        # Backlog cap defaults to TWICE the daily content-send cap: at that depth the persona has
        # already drafted more than two days of deliverable output, so more is pure queue growth.
        max_backlog_drafts=int(caps.get("max_backlog_drafts", 2 * int(caps.get("emails_per_day", 3)))),
        # global caps come from the GLOBAL toml only, so a persona cannot raise its own ceiling
        global_emails_per_day=int(gtoml.get("caps", {}).get("global_emails_per_day", 4)),
        global_emails_per_week=int(gtoml.get("caps", {}).get("global_emails_per_week", 12)),
        reflect_light_hours=int(refl.get("light_hours", 24)),
        reflect_deep_hours=int(refl.get("deep_hours", 168)),
        context_byte_cap=int(cog.get("context_byte_cap", 49152)),
        memory_notes=int(cog.get("memory_notes", 8)),
        tick_timeout_s=int(cog.get("tick_timeout_s", 180)),
        ack_commands=bool(cmds.get("acknowledge", True)),
        command_footer=bool(cmds.get("footer", True)),
        repo_root=REPO_ROOT,
        persona=persona,
        plus_tag=plus_tag,
        mailbox=mailbox,
        owner=owner,
    )
    if cfg.MODE == "LIVE" and not cfg.gmail_app_password:
        raise RuntimeError("MODE=LIVE but GMAIL_APP_PASSWORD missing in ~/.config/cagent/.env")
    return cfg


def validate_personas() -> list[tuple[str, bool, str]]:
    """(name, ok, detail) per enabled persona. claude-FREE so it is safe to run in doctor/CI: each
    persona's config loads; any mailbox/owner overlay it names exists and defines its load-bearing
    key (AGENT_EMAIL / OWNER_EMAIL) rather than silently falling back to the global account/owner;
    and the resolved owner address is not the agent's own account (a misconfig that would let the
    agent mail itself). Catches a typo'd mailbox/owner id BEFORE launchd ever runs the persona."""
    out: list[tuple[str, bool, str]] = []
    for name in enabled_personas():
        try:
            c = load(name)
        except Exception as e:                       # noqa: BLE001 - any load failure is a fail row
            out.append((f"persona {name}: config loads", False, str(e)))
            continue
        atoml = _persona_toml(name).get("agent", {})
        for axis, key in (("mailbox", "AGENT_EMAIL"), ("owner", "OWNER_EMAIL")):
            ref = str(atoml.get(axis, "")).strip()
            if not ref:
                continue
            p = ENV_PATH.parent / f".env-{ref}"
            keys = _parse_env(p)
            out.append((f"persona {name}: {axis} '{ref}' file exists", p.exists(), str(p)))
            out.append((f"persona {name}: {axis} '{ref}' defines {key}", key in keys,
                        "else it silently falls back to the global value"))
        out.append((f"persona {name}: owner != agent account",
                    c.owner_email.lower() != c.agent_email.lower(),
                    f"owner={c.owner_email} account={c.agent_email}"))
        if c.MODE == "LIVE":
            out.append((f"persona {name}: LIVE has app password", bool(c.gmail_app_password), ""))
    # The untagged-mail routing target must be a real, enabled persona; a typo'd `default` would
    # silently route owner mail with no plus-tag to a persona that never runs (L7).
    dflt = default_persona()
    if dflt or enabled_personas():
        out.append(("[personas].default is enabled", dflt in enabled_personas(),
                    f"default={dflt!r} not in enabled={enabled_personas()}"))
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(load().redacted(), indent=2, default=str))
