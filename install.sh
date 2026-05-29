#!/usr/bin/env bash
# claude-pop-quiz installer.
# Copies the hook into ~/.claude/hooks/ and merges the hook config into
# ~/.claude/settings.json (preserving everything already there).
# Idempotent: safe to re-run. Requires python3; uses it for the JSON merge.
set -euo pipefail

CLAUDE_DIR="${CLAUDE_DIR:-$HOME/.claude}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOOK_SRC="$SRC_DIR/hooks/pop_quiz.py"
HOOK_DST="$CLAUDE_DIR/hooks/pop_quiz.py"
SETTINGS="$CLAUDE_DIR/settings.json"

mkdir -p "$CLAUDE_DIR/hooks"
cp "$HOOK_SRC" "$HOOK_DST"
chmod +x "$HOOK_DST"
echo "Installed hook -> $HOOK_DST"

python3 - "$SETTINGS" <<'PY'
import json, os, sys
path = sys.argv[1]
try:
    with open(path) as f:
        cfg = json.load(f)
except Exception:
    cfg = {}

hooks = cfg.setdefault("hooks", {})

def ensure(event, mode):
    cmd = f"python3 ~/.claude/hooks/pop_quiz.py {mode} 2>/dev/null || true"
    arr = hooks.setdefault(event, [])
    # don't add a duplicate pop_quiz hook if one is already present
    for group in arr:
        for h in group.get("hooks", []):
            if "pop_quiz.py" in h.get("command", ""):
                h["command"] = cmd  # refresh in case the path/flags changed
                return
    entry = {"type": "command", "command": cmd}
    if event == "UserPromptSubmit":
        entry["statusMessage"] = "pop-quiz cadence..."
    arr.append({"hooks": [entry]})

ensure("UserPromptSubmit", "prompt")
ensure("PreToolUse", "tool")

os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")
print(f"Merged hook config -> {path}")
PY

echo
echo "Done. Open /hooks in Claude Code once (or restart) to load the new config."
echo "New chats pick it up automatically. Default cadence is every 40-45 actions."
echo "Tune it with env vars, e.g. for longer sessions:"
echo "  POP_QUIZ_MIN=90 POP_QUIZ_MAX=110 POP_QUIZ_QUESTIONS=5"
