#!/usr/bin/env bash
# Account-level Anthropic usage / rate-limit windows for the Claude subscription that powers the
# tick (the OAuth `usage` endpoint, NOT this repo's per-persona token journals -- for those see
# usage-all.sh). Read-only: one GET, no state, no claude call. The subscription token is never in
# the repo; it is read from the macOS Keychain, falling back to ~/.claude/.credentials.json on
# hosts that store it as a file. The token is only ever handed to curl over a -K config file so it
# does not land in `ps` output or shell history.
#
# NOTE: on a subscription plan this endpoint returns only rounded whole-number percentages; the
# limit_dollars/used_dollars fields are null (nothing is metered in dollars). The percentages ARE
# the most precise figure available here. For exact token/cost numbers use `cagentctl usage`, which
# sums the committed tick journals.
# Usage:
#   bin/oauth-usage.sh                     # compact summary: one row per limit + time-to-reset (default)
#   bin/oauth-usage.sh --json              # full JSON, pretty-printed
#   bin/oauth-usage.sh '.five_hour'        # any jq filter over the response
#   bin/oauth-usage.sh -r '.five_hour.utilization'
set -uo pipefail

for bin in jq curl; do
  command -v "$bin" >/dev/null 2>&1 || { echo "oauth-usage: '$bin' not found on PATH" >&2; exit 1; }
done

# Default to the compact summary. --json (or any jq filter/flag) switches to raw jq passthrough.
mode="summary"
if [ $# -gt 0 ]; then
  case "$1" in
    -s|--summary) shift ;;             # explicit summary (same as no args)
    -j|--json)    mode="raw"; shift ;; # full JSON dump == `jq .`
    *)            mode="raw" ;;        # a jq filter/flag was given -> pass everything through
  esac
fi

# Resolve the OAuth access token: Keychain first (macOS live host), then the credentials file
# (matches the original one-liner and the Linux mirror).
token=""
if command -v security >/dev/null 2>&1; then
  token=$(security find-generic-password -s "Claude Code-credentials" -w 2>/dev/null \
            | jq -r '.claudeAiOauth.accessToken // empty' 2>/dev/null || true)
fi
if [ -z "$token" ] && [ -f "$HOME/.claude/.credentials.json" ]; then
  token=$(jq -r '.claudeAiOauth.accessToken // empty' "$HOME/.claude/.credentials.json" 2>/dev/null || true)
fi
if [ -z "$token" ]; then
  echo "oauth-usage: no Claude OAuth token found (Keychain 'Claude Code-credentials' or ~/.claude/.credentials.json)." >&2
  echo "             Run 'claude' once to authenticate, then retry." >&2
  exit 1
fi

# Pass the bearer through a -K config file (process substitution) so the secret never appears in
# argv/ps. -f makes curl exit non-zero on 4xx/5xx so an expired token is a loud failure rather than
# silent empty output.
resp=$(curl -sS -f https://api.anthropic.com/api/oauth/usage \
         -H "anthropic-beta: oauth-2025-04-20" \
         -K <(printf 'header = "Authorization: Bearer %s"\n' "$token")) || {
  echo "oauth-usage: request failed (token expired? run 'claude' to refresh)." >&2
  exit 1
}

if [ "$mode" = "raw" ]; then
  printf '%s' "$resp" | jq "${@:-.}"
  exit 0
fi

# Compact view: one row per active limit window. jq does the clock math (portable -- no reliance on
# the incompatible BSD/GNU `date` flags). resets_at is always UTC (+00:00), so slicing to the second
# and appending Z parses correctly with fromdateiso8601; `now` is the current epoch.
printf '%s' "$resp" | jq -r '
  def dur($s):
    ($s | if . < 0 then 0 else . end) as $s
    | ($s/86400 | floor) as $d | (($s%86400)/3600 | floor) as $h | (($s%3600)/60 | floor) as $m
    | if $d > 0 then "\($d)d \($h)h" else "\($h)h \($m)m" end;
  "SCOPE\tUSED\tSTATUS\tRESETS-IN\tRESETS-AT",
  (.limits[]
    | ((.resets_at[0:19] + "Z" | fromdateiso8601) - now) as $left
    | "\(.kind)\t\(.percent)%\t\(.severity)\t\(dur($left))\t\(.resets_at[0:16] | sub("T"; " "))")
' | column -t -s $'\t'
