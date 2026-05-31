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

# Optional config, baked into the UserPromptSubmit command. Pre-set via env, or
# answer the prompts when run interactively.
POP_QUIZ_FORMAT="${POP_QUIZ_FORMAT:-essay}"
POP_QUIZ_DEFER_LIMIT="${POP_QUIZ_DEFER_LIMIT:-0}"
if [ -t 0 ]; then
  read -r -p "Quiz format essay|mcq|mixed [$POP_QUIZ_FORMAT]: " _f || true
  POP_QUIZ_FORMAT="${_f:-$POP_QUIZ_FORMAT}"
  read -r -p "Defer limit before tools freeze, 0=off [$POP_QUIZ_DEFER_LIMIT]: " _d || true
  POP_QUIZ_DEFER_LIMIT="${_d:-$POP_QUIZ_DEFER_LIMIT}"
fi
export POP_QUIZ_FORMAT POP_QUIZ_DEFER_LIMIT

python3 - "$SETTINGS" <<'PY'
import json, os, sys
path = sys.argv[1]
try:
    with open(path) as f:
        cfg = json.load(f)
except Exception:
    cfg = {}

hooks = cfg.setdefault("hooks", {})

# Build an env prefix for the prompt command from the chosen config.
_fmt = os.environ.get("POP_QUIZ_FORMAT", "essay")
_defer = os.environ.get("POP_QUIZ_DEFER_LIMIT", "0")

def ensure(event, mode):
    # Both commands get the same config: the tool-mode hook needs
    # POP_QUIZ_DEFER_LIMIT to know whether to freeze, not just the prompt hook.
    envs = []
    if _fmt and _fmt != "essay":
        envs.append(f"POP_QUIZ_FORMAT={_fmt}")
    if _defer and _defer != "0":
        envs.append(f"POP_QUIZ_DEFER_LIMIT={_defer}")
    prefix = (" ".join(envs) + " ") if envs else ""
    cmd = f"{prefix}python3 ~/.claude/hooks/pop_quiz.py {mode} 2>/dev/null || true"
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
echo "Graded results are journaled to: $CLAUDE_DIR/state/learning_journal.md"
echo "  (override with POP_QUIZ_JOURNAL=/path/to/journal.md)"
echo "Format: $POP_QUIZ_FORMAT   |   Defer limit (freeze): $POP_QUIZ_DEFER_LIMIT (0=off)"
echo "Re-run with env to change, e.g.:"
echo "  POP_QUIZ_FORMAT=mcq POP_QUIZ_DEFER_LIMIT=3 ./install.sh   # quick MCQ + freeze after 3 defers"
echo "  POP_QUIZ_MIN=90 POP_QUIZ_MAX=110 POP_QUIZ_QUESTIONS=5     # quiz less often"
