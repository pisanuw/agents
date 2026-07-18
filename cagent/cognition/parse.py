"""Parse the claude JSON envelope into a typed result. We trust `structured_output`
(validated by the CLI against our --json-schema), never the free-text `result`.
Rate-limit / auth-lapse are detected so the heartbeat can back off instead of spinning.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from cagent.cognition import meter

RATE_LIMIT_RE = re.compile(
    r"rate.?limit|usage.?limit|quota|reached your limit|too many requests|try again (later|in)|overloaded",
    re.I,
)
AUTH_RE = re.compile(
    r"not logged in|please run /login|/login|authentication|unauthorized|invalid api key|session expired",
    re.I,
)

@dataclass
class ParseResult:
    status: str            # OK | FLAG_ERROR | EMPTY_OUTPUT | BAD_JSON | API_ERROR | RATE_LIMIT | AUTH_ERROR | NO_STRUCTURED_OUTPUT | TIMEOUT
    text: str = ""         # free-text result (human log only)
    structured: Any = None
    http: Any = None
    detail: str = ""
    cost_usd: float | None = None
    num_turns: int | None = None
    usage: dict | None = None    # normalized token counts (input/output/cache_*), None if absent

    @property
    def ok(self) -> bool:
        return self.status == "OK"

    @property
    def rate_limited(self) -> bool:
        return self.status in ("RATE_LIMIT", "AUTH_ERROR")


def parse(env) -> ParseResult:
    if getattr(env, "timed_out", False):
        return ParseResult("TIMEOUT", detail=env.stderr[:500])

    if not env.stdout.strip():
        low = env.stderr.lower()
        if "unknown option" in low or "unknown argument" in low or "unknown command" in low:
            return ParseResult("FLAG_ERROR", detail=env.stderr.strip()[:500])
        if AUTH_RE.search(env.stderr):
            return ParseResult("AUTH_ERROR", detail=env.stderr.strip()[:500])
        if RATE_LIMIT_RE.search(env.stderr):
            return ParseResult("RATE_LIMIT", detail=env.stderr.strip()[:500])
        return ParseResult("EMPTY_OUTPUT", detail=env.stderr.strip()[:500])

    try:
        e = json.loads(env.stdout)
    except json.JSONDecodeError:
        return ParseResult("BAD_JSON", detail=env.stdout[:500])

    if not isinstance(e, dict):
        return ParseResult("BAD_JSON", detail=str(e)[:500])

    text = e.get("result", "") or ""
    cost = e.get("total_cost_usd")
    turns = e.get("num_turns")
    usage = meter.usage_fields(e)

    if e.get("is_error"):
        blob = f"{text} {env.stderr}"
        http = e.get("api_error_status")
        if http == 429 or RATE_LIMIT_RE.search(blob):
            return ParseResult("RATE_LIMIT", text=text, http=http, detail=blob[:500])
        if AUTH_RE.search(blob):
            return ParseResult("AUTH_ERROR", text=text, http=http, detail=blob[:500])
        return ParseResult("API_ERROR", text=text, http=http, detail=blob[:500])

    so = e.get("structured_output")
    if so is None:
        return ParseResult("NO_STRUCTURED_OUTPUT", text=text, cost_usd=cost, num_turns=turns, usage=usage)
    return ParseResult("OK", text=text, structured=so, cost_usd=cost, num_turns=turns, usage=usage)
