#!/usr/bin/env bash
# Install quiet-output as a user-level Claude Code hook, so every new session in
# every project has it enabled.
#
#   ./install.sh              install or update
#   ./install.sh --uninstall  remove the hook entry (leaves the script in place)
#   ./install.sh --quiet      no output unless something changed or failed
#
# Also works standalone, with nothing checked out:
#   curl -fsSL <raw-url>/install.sh | bash
#
# Idempotent: re-running updates the existing entry instead of adding a second
# one, and never rewrites the rest of your settings.
set -euo pipefail

# When piped from curl there is no script directory; fall back to downloading.
# BASH_SOURCE is unset in that case, which is fatal under `set -u` on older bash,
# so read it with -u disabled.
set +u
_self="${BASH_SOURCE[0]}"
set -u
SRC_DIR=""
if [ -n "$_self" ] && [ -r "$_self" ]; then
  SRC_DIR="$(cd "$(dirname "$_self")" && pwd)"
fi
QUIET_REPO="${QUIET_REPO:-mendresvon/mendresvon}"
QUIET_REF="${QUIET_REF:-claude/global-command-output-filter-n075u3}"
QUIET_SRC_URL="${QUIET_SRC_URL:-https://raw.githubusercontent.com/$QUIET_REPO/$QUIET_REF/.claude/hooks/quiet-output.py}"
CONFIG_HOME="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
HOOK_DIR="$CONFIG_HOME/hooks"
HOOK="$HOOK_DIR/quiet-output.py"
SETTINGS="$CONFIG_HOME/settings.json"
MODE="install"
QUIET=""

for arg in "$@"; do
  case "$arg" in
    --uninstall) MODE="uninstall" ;;
    --quiet|-q)  QUIET=1 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

say () { [ -n "$QUIET" ] || echo "$@"; }

command -v python3 >/dev/null || { echo "quiet-output needs python3 on PATH" >&2; exit 1; }

if [ "$MODE" = "install" ]; then
  mkdir -p "$HOOK_DIR"
  STAGED=""
  if [ -n "$SRC_DIR" ] && [ -f "$SRC_DIR/quiet-output.py" ]; then
    STAGED="$SRC_DIR/quiet-output.py"
  else
    STAGED="$(mktemp)"; trap 'rm -f "$STAGED"' EXIT
    say "fetching quiet-output.py from $QUIET_SRC_URL"
    if command -v curl >/dev/null; then
      curl -fsSL "$QUIET_SRC_URL" -o "$STAGED"
    elif command -v wget >/dev/null; then
      wget -qO "$STAGED" "$QUIET_SRC_URL"
    else
      echo "need curl or wget to fetch quiet-output.py, or run install.sh from a checkout" >&2
      exit 1
    fi
  fi
  # never install something that is not a working script
  if ! python3 -c "import sys,py_compile; py_compile.compile(sys.argv[1], doraise=True)" "$STAGED" >/dev/null 2>&1; then
    echo "downloaded quiet-output.py is not valid Python — refusing to install" >&2; exit 1
  fi
  if ! cmp -s "$STAGED" "$HOOK" 2>/dev/null; then
    cp "$STAGED" "$HOOK"
    say "installed $HOOK"
  fi
  chmod +x "$HOOK"
fi

python3 - "$SETTINGS" "$MODE" "${QUIET:-}" <<'PY'
import json, os, sys, shutil

settings_path, mode, quiet = sys.argv[1], sys.argv[2], sys.argv[3]
COMMAND = "~/.claude/hooks/quiet-output.py hook"
MARKER = "quiet-output.py"

def say(*a):
    if not quiet:
        print(*a)

data = {}
if os.path.exists(settings_path):
    with open(settings_path) as fh:
        raw = fh.read().strip()
    if raw:
        try:
            data = json.loads(raw)
        except ValueError as exc:
            sys.exit("refusing to touch %s: it is not valid JSON (%s)"
                     % (settings_path, exc))
    if not isinstance(data, dict):
        sys.exit("refusing to touch %s: top level is not an object" % settings_path)

before = json.dumps(data, sort_keys=True)
hooks = data.setdefault("hooks", {})
pre = hooks.setdefault("PreToolUse", [])
if not isinstance(pre, list):
    sys.exit("refusing to touch %s: hooks.PreToolUse is not a list" % settings_path)

entry = {"type": "command", "command": COMMAND, "timeout": 10,
         "statusMessage": "Filtering noisy output…"}

# find any existing quiet-output entry, wherever it was placed
found = False
for group in pre:
    if not isinstance(group, dict):
        continue
    inner = group.get("hooks")
    if not isinstance(inner, list):
        continue
    for i, h in enumerate(list(inner)):
        if isinstance(h, dict) and MARKER in str(h.get("command", "")):
            found = True
            if mode == "uninstall":
                inner.remove(h)
            else:
                inner[i] = entry

if mode == "uninstall":
    # drop any group left with no hooks, and empty containers
    pre[:] = [g for g in pre
              if not (isinstance(g, dict) and g.get("hooks") == [])]
    if not pre:
        hooks.pop("PreToolUse", None)
    if not hooks:
        data.pop("hooks", None)
    say("removed" if found else "nothing to remove")
elif not found:
    # prefer an existing Bash matcher group so we do not fragment the config
    for group in pre:
        if isinstance(group, dict) and group.get("matcher") == "Bash" \
                and isinstance(group.get("hooks"), list):
            group["hooks"].append(entry)
            break
    else:
        pre.append({"matcher": "Bash", "hooks": [entry]})
    data.setdefault("$schema",
                    "https://json.schemastore.org/claude-code-settings.json")

if json.dumps(data, sort_keys=True) == before:
    say("already up to date: %s" % settings_path)
    sys.exit(0)

if os.path.exists(settings_path):
    shutil.copy2(settings_path, settings_path + ".bak")
os.makedirs(os.path.dirname(settings_path), exist_ok=True)
tmp = settings_path + ".tmp"
with open(tmp, "w") as fh:
    json.dump(data, fh, indent=2, ensure_ascii=False)
    fh.write("\n")
os.replace(tmp, settings_path)
say("updated %s" % settings_path)
PY

if [ "$MODE" = "install" ]; then
  say ""
  say "quiet-output is enabled for every new Claude Code session, in every project."
  say "Hooks load at session start, so restart Claude Code (or open /hooks) once."
fi
