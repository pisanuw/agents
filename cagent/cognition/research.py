"""The bounded web-research sub-call: the ONLY place tools are enabled (read-only
WebSearch/WebFetch). It cannot send, write files, or run shell. Fetched pages are
untrusted; the constitution + researcher prompt instruct the model to treat them as data.
Per-day cap is enforced via a small ledger.
"""
from __future__ import annotations

import json

from cagent import atomicio, clock, config, persona
from cagent.cognition import invoke, parse

SCHEMA = config.REPO_ROOT / "prompts" / "schemas" / "research_output.json"
RESEARCHER = config.REPO_ROOT / "persona" / "researcher.md"


def _ledger():
    return config.state_root() / "research_ledger.jsonl"


def _no_research():
    return config.state_root() / "no_research.flag"     # !NO-RESEARCH: disable web research


def _today_count() -> int:
    today = clock.today()
    # one malformed line must not abort the count -- atomicio.read_jsonl skips undecodable lines,
    # so the daily research cap stays enforceable even if the ledger has a torn write.
    return sum(1 for row in atomicio.read_jsonl(_ledger()) if row.get("date") == today)


def _record(query: str, n_findings: int) -> None:
    _ledger().parent.mkdir(parents=True, exist_ok=True)
    with open(_ledger(), "a") as f:
        f.write(json.dumps({"date": clock.today(), "ts": clock.iso(),
                            "query": query[:200], "findings": n_findings}) + "\n")


def run(query: str, timeout_s: int = 240) -> dict | None:
    """Returns research_output dict, or None on cap/error/rate-limit.
    Returns None (not a stub) for cap/!NO-RESEARCH so callers can distinguish "nothing ran"
    from a real sub-call result: a stub zero-findings dict would trigger memory writes and
    goal-progress logs for work that never happened (L2)."""
    cfg = config.load()
    if _no_research().exists():       # owner disabled the web-research sub-call via !NO-RESEARCH
        return None
    if _today_count() >= cfg.research_per_day:
        return None
    sys_prompt = persona.constitution() + "\n\n" + (RESEARCHER.read_text() if RESEARCHER.exists() else "")
    env = invoke.run_claude(
        f"Research query: {query}\nReturn only the requested JSON.",
        model=cfg.model_tick,
        tools="WebSearch WebFetch",
        append_system_prompt=sys_prompt,
        schema_path=str(SCHEMA),
        timeout_s=timeout_s,
        extra=["--permission-mode", "bypassPermissions"],
        label="research",
    )
    r = parse.parse(env)
    # Count every real sub-call attempt against the daily cap, regardless of parse result (L3):
    # a persistently-failing web call was previously uncounted and fired every tick forever.
    _record(query, len(r.structured.get("findings", [])) if r.status == "OK" and isinstance(r.structured, dict) else 0)
    if r.status == "OK" and isinstance(r.structured, dict):
        return r.structured
    if r.rate_limited:
        from cagent.cognition import backoff as _backoff
        _backoff.record_failure(r.status, r.http)
    return None
