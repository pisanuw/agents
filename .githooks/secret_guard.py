#!/usr/bin/env python3
"""Deterministic secret guard for staged changes. The agent runs git non-interactively
where gitleaks hooks can be skipped, so this same logic also runs in-process before the
daily push (cagent/gitpush.py). Exit non-zero blocks the commit."""
import os
import re
import subprocess
import sys
from pathlib import Path

# Env-var ASSIGNMENTS that look like a real .env value. The test suite legitimately contains fake
# ones (a fixture app-password / token value), so main() skips these for files under tests/;
# everywhere else a KEY=VALUE assignment is treated as a leak.
ENV_ASSIGN_PATTERNS = [
    # Any non-blank value, even the grouped display form ("abcd efgh ijkl mnop") whose spaces the old
    # base64 pattern let slip past. Requires the value to START with an alnum (optionally quoted), so
    # the blank template (`GMAIL_APP_PASSWORD=`) and the `=   # 16-char...` doc placeholder are ignored.
    re.compile(r"GMAIL_APP_PASSWORD\s*=\s*[\"']?[A-Za-z0-9]"),
    # COMMAND_TOKEN carries an arbitrary value; catch the env-var assignment form to avoid
    # false-positives on the many legitimate references to the variable name in code/tests/docs.
    re.compile(r"COMMAND_TOKEN\s*=\s*[\"']?[A-Za-z0-9!@#$%^&*_\-]{6,}"),
]
# Raw key MATERIAL: never legitimate in ANY file, tests included. Always scanned. (A live configured
# COMMAND_TOKEN appearing raw is likewise caught everywhere, via _leaked_tokens in main().)
KEY_MATERIAL_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9]{20,}"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),   # JWT
    re.compile(r"\bre_[A-Za-z0-9]{16,}"),                          # resend-style (legacy)
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                          # AWS access key
]
# Full set, in original order. Kept as one list for callers that scan a single string (unit tests).
PATTERNS = ENV_ASSIGN_PATTERNS + KEY_MATERIAL_PATTERNS
# `.env` and any per-persona/local overlay beside it (`.env-mailbox-1`, `.env-scout`, `.env.local`);
# `.env.example` matches too but the EXACT template path is allowed in main() (any other .env.example
# stays blocked). Overlays are gitignored, but this guard is the backstop for a `git add -f` that
# bypasses .gitignore, so it must cover the -/. forms.
BLOCKED_PATH = re.compile(
    r"\.env$|(^|/)\.env[.-]|(^|/)secrets/|credentials|\.key$|\.pem$|\.p12$|\.keyring$")

# Backstop for a token re-leak: a received mail JSON (or any staged file) that still carries a raw
# COMMAND_TOKEN because redaction was called with an empty/mismatched persona token. The earlier
# heuristic ("a command verb followed by an 8+ char alnum run") false-positived on every free-text
# command -- `!GOAL All the collaborators of ...` has ordinary 8-letter English words after the verb,
# and a mistyped token guess in a subject also tripped it, wedging daily-push. A COMMAND_TOKEN is
# indistinguishable from prose by CHARSET, so we match the EXACT configured value(s) instead: precise,
# zero false-positives on prose, and still catches a cross-persona leak (every owner overlay's token
# is in the set). A correctly-sealed command holds the «COMMAND_TOKEN» placeholder, never the raw
# value, so it never matches.
def _configured_tokens() -> set[str]:
    """The live COMMAND_TOKEN value(s), read straight from ~/.config/cagent/.env and its overlays
    (.env-<owner>, .env-<mailbox>, .env-<persona>). Empty when the secrets dir is absent (a dev
    clone / CI): there is no live secret to leak there, and the COMMAND_TOKEN assignment PATTERN
    still guards committed config. CAGENT_CONFIG_DIR overrides the location (tests point it at tmp)."""
    cfgdir = Path(os.environ.get("CAGENT_CONFIG_DIR") or os.path.expanduser("~/.config/cagent"))
    tokens: set[str] = set()
    if not cfgdir.is_dir():
        return tokens
    for env in sorted(cfgdir.glob(".env*")):
        try:
            text = env.read_text()
        except OSError:
            continue
        for line in text.splitlines():
            if line.startswith("COMMAND_TOKEN="):
                v = line.split("=", 1)[1].strip().strip('"').strip("'")
                if v:
                    tokens.add(v)
    return tokens


def _leaked_tokens(content: str, tokens: set[str]) -> set[str]:
    """The configured tokens that appear RAW (un-redacted) in a staged file's content."""
    return {t for t in tokens if t and t in content}


# The exact committed template, whose values are blank by design. Only THIS path is exempt from the
# blocked-path rule; every other *.env.example (e.g. secrets/creds.env.example) is still blocked.
TEMPLATE_PATH = "cagent/.env.example"


def staged_files() -> list[str]:
    r = subprocess.run(
        # --diff-filter=ACMR: include Renamed files (the R). Without R, a rename-plus-edit that
        # introduces a secret would be invisible to both this hook and gitpush._secret_scan. For a
        # rename, --name-only reports the destination path, which is what we want to scan/block.
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        sys.stderr.write(f"secret_guard: git diff-index failed (rc={r.returncode}): {r.stderr.strip()}\n")
        sys.exit(1)
    return [f for f in r.stdout.splitlines() if f.strip()]


def staged_content(path: str) -> str:
    r = subprocess.run(["git", "show", f":{path}"], capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write(f"secret_guard: git show :{path} failed; blocking commit\n")
        sys.exit(1)
    return r.stdout


def main() -> int:
    bad: list[str] = []
    tokens = _configured_tokens()
    for f in staged_files():
        # Only the exact template escapes the blocked-path rule; every other .env* still blocks.
        if BLOCKED_PATH.search(f) and f != TEMPLATE_PATH:
            bad.append(f"blocked path staged: {f}")
            continue
        # NOTE: the template is NOT skipped from the content scan below -- its values are blank so the
        # PATTERNS never fire, but a real password accidentally pasted into it WILL be caught.
        content = staged_content(f)
        # Under tests/, env-var ASSIGNMENTS are fixtures (fake passwords/tokens), so scan only for raw
        # key MATERIAL there; everywhere else scan the full set. Real keys and a live COMMAND_TOKEN
        # (below) are still caught in tests.
        is_test = f.startswith("tests/") or "/tests/" in f
        for pat in (KEY_MATERIAL_PATTERNS if is_test else PATTERNS):
            if pat.search(content):
                bad.append(f"secret pattern in {f}: /{pat.pattern}/")
        # A live COMMAND_TOKEN must never land in git raw -- received mail is the usual vector (sealed
        # with a mismatched persona token), but check every staged file. Exact-value match means a
        # free-text `!GOAL <prose>` never false-positives, unlike the old charset heuristic.
        if _leaked_tokens(content, tokens):
            bad.append(f"received mail JSON has an un-redacted COMMAND_TOKEN: {f}")
    if bad:
        sys.stderr.write("SECRET GUARD BLOCKED:\n  " + "\n  ".join(bad) + "\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
