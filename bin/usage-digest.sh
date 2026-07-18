#!/usr/bin/env bash
# launchd entrypoint for the once-daily fleet usage email: sums every persona's token/cost from
# the committed journals and mails the owner one report. Idempotent (cagentctl usage-email has its
# own once-per-day guard, var/last_usage_email), so a duplicate launchd fire is a no-op.
# Sends through the default persona's gate (owner + mailbox come from it); honors mode + caps.
# Run this on the LIVE host, not the mirror: the mirror has no sending mailbox configured.
# Shared launchd prelude (PATH, ROOT, cd, venv PY + fail-loud). Sourcing it -- rather than the
# verbatim copy this script used to carry -- keeps the usagemail timer in lockstep with the other
# launchd entrypoints (dailypush/dispatcher/heartbeat/watchdog) whenever the prelude changes.
. "$(dirname "${BASH_SOURCE[0]}")/_launchd_env.sh"

# Account-level Anthropic rate-limit snapshot (the OAuth usage endpoint the subscription reports).
# Best-effort: an expired token / missing curl|jq must NOT block the usage email, so a failure
# degrades to a short note (oauth-usage.sh already prints the reason on stderr, folded in via 2>&1).
oauth=$("$ROOT/bin/oauth-usage.sh" 2>&1) || oauth="(oauth-usage unavailable: ${oauth:-unknown error})"
block=$'=== Anthropic account rate-limit windows (subscription powering the tick) ===\n'"$oauth"
printf '%s\n' "$block"                       # -> StandardOutPath (kept in logs/usagemail.out.log)
export CAGENT_USAGE_EMAIL_APPEND="$block"    # -> appended to the usage email body by cmd_usage_email
exec "$PY" -m cagent.cli usage-email "$@"
