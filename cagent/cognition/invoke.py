"""The cognition harness: invoke the local `claude` CLI headless and capture the
JSON envelope. Tools default OFF (`--tools ""`) so the model returns a decision and
cannot loop, read files, or touch secrets. Timeout kills the whole process group.

All flags live in build_argv so a CLI rename is a one-line fix.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path

from cagent import clock, config
from cagent.cognition import meter

# Stripped from the child environment so even a tool-enabled sub-call cannot read them.
SCRUB_ENV = ("GMAIL_APP_PASSWORD", "ANTHROPIC_API_KEY", "AWS_SECRET_ACCESS_KEY", "COMMAND_TOKEN")
# ...and, defensively, ANY var whose NAME looks like a credential. The explicit list above is a
# denylist that only covers today's known secrets; a new one exported into the launchd/shell
# environment (GITHUB_TOKEN, AWS_SESSION_TOKEN, a per-overlay password, ...) would otherwise be
# inherited by every claude subprocess -- including the tools-ON research sub-call. A name-pattern
# scrub catches those without an allowlist that could starve the subscription-authed CLI of the
# broad environment it needs (claude auths via ~/.claude, not an env token, so scrubbing
# TOKEN/AUTH-named vars is safe).
_SECRET_NAME_RE = re.compile(
    r"(PASSWORD|PASSWD|SECRET|TOKEN|API[_-]?KEY|PRIVATE[_-]?KEY|CREDENTIAL|ACCESS[_-]?KEY|"
    r"CLIENT[_-]?SECRET|AUTH[_-]?TOKEN|SESSION[_-]?TOKEN)", re.IGNORECASE)


@dataclass
class RawEnvelope:
    stdout: str
    stderr: str
    code: int
    timed_out: bool = False


def _child_env() -> dict:
    env = dict(os.environ)
    for k in list(env):
        if k in SCRUB_ENV or _SECRET_NAME_RE.search(k):
            env.pop(k, None)
    return env


def build_argv(cfg, *, model: str, tools: str, schema_path: str | None = None,
               append_system_prompt: str | None = None,
               extra: list[str] | None = None) -> list[str]:
    argv = [cfg.claude_bin, "-p", "--output-format", "json", "--model", model]
    argv += ["--tools", tools]  # "" = all tools off; "WebSearch WebFetch" = only those
    if append_system_prompt:
        # Append our constitution/persona. NOTE: this does NOT prevent the owner's global
        # ~/.claude/CLAUDE.md from loading (subscription auth requires the default config
        # dir, and --bare disables keychain auth). Containment instead comes from: tools
        # OFF, a strict --json-schema decision, and the constitution's explicit override
        # clause telling the model those global instructions are not its. See docs/ARCHITECTURE.md.
        argv += ["--append-system-prompt", append_system_prompt]
    if schema_path:
        argv += ["--json-schema", Path(schema_path).read_text()]
    argv += ["--no-session-persistence"]
    if extra:
        argv += extra
    return argv


def _meter_call(label: str, model: str, stdout: str, ms: int | None, timed_out: bool = False) -> None:
    """Record this call's token/cost into the per-tick meter. invoke is the single chokepoint every
    claude subprocess funnels through, so metering here counts EVERY call (cognition, gate-check,
    research, reflection) with no per-caller bookkeeping. Best-effort: a non-JSON/empty stdout still
    records a zero row so `calls` reflects the true number of invocations. A timed-out call is a
    zero row too, but flagged so the report can surface it (its usage never arrived). Never raises."""
    usage, cost = None, None
    try:
        if stdout.strip():
            e = json.loads(stdout)
            if isinstance(e, dict):
                usage, cost = meter.usage_fields(e), e.get("total_cost_usd")
    except (json.JSONDecodeError, ValueError):
        pass
    meter.record(label, model, usage, cost, ms, timed_out=timed_out)


def run_claude(prompt: str, *, model: str | None = None, tools: str = "",
               schema_path: str | None = None, append_system_prompt: str | None = None,
               timeout_s: int | None = None, extra: list[str] | None = None,
               label: str = "?") -> RawEnvelope:
    cfg = config.load()
    model = model or cfg.model_tick
    timeout_s = timeout_s or cfg.tick_timeout_s
    argv = build_argv(cfg, model=model, tools=tools, schema_path=schema_path,
                      append_system_prompt=append_system_prompt, extra=extra)
    started = clock.now()
    proc = subprocess.Popen(
        argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=_child_env(), start_new_session=True,
    )
    try:
        out, err = proc.communicate(input=prompt, timeout=timeout_s)
        env = RawEnvelope(stdout=out, stderr=err, code=proc.returncode)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        out, err = proc.communicate()
        env = RawEnvelope(stdout=out or "", stderr=(err or "") + "\n[timeout: killed process group]",
                          code=124, timed_out=True)
    _meter_call(label, model, env.stdout, int((clock.now() - started).total_seconds() * 1000),
                timed_out=env.timed_out)
    return env
