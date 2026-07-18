# Shared launchd prelude: PATH, ROOT, venv python. Source this at the top of each
# launchd entrypoint with:  . "$(dirname "$0")/_launchd_env.sh"
# No shebang — this file is sourced, not executed directly.
set -uo pipefail
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$HOME/.pyenv/shims:${PATH:-}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1
PY="$ROOT/.venv/bin/python"
if [ ! -x "$PY" ]; then   # fail loud: bare python3 lacks jsonschema, so this would crash cryptically
  echo "${0##*/}: venv python missing ($PY). Run: python3 -m venv .venv && ./.venv/bin/pip install -e '.[dev]'" >&2
  exit 1
fi
