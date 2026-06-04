#!/usr/bin/env bash
# claude-pop-quiz uninstaller.
# Removes the hook script and strips the pop_quiz hook entries from
# ~/.claude/settings.json. State and journal are preserved — delete them
# manually if you want a clean slate.
set -euo pipefail

CLAUDE_DIR="${CLAUDE_DIR:-$HOME/.claude}"
HOOK_DST="$CLAUDE_DIR/hooks/pop_quiz.py"
SETTINGS="$CLAUDE_DIR/settings.json"

# Remove the hook script.
if [ -f "$HOOK_DST" ]; then
    rm "$HOOK_DST"
    echo "Removed $HOOK_DST"
else
    echo "Hook not found at $HOOK_DST (already removed?)"
fi

# Strip the two pop_quiz hook entries from settings.json.
if [ -f "$SETTINGS" ]; then
    python3 - "$SETTINGS" <<'PY'
import json, sys
path = sys.argv[1]
try:
    with open(path) as f:
        cfg = json.load(f)
except Exception as e:
    print(f"Could not read {path}: {e}")
    sys.exit(1)

hooks = cfg.get("hooks", {})
changed = False
for event in list(hooks.keys()):
    new_groups = []
    for group in hooks[event]:
        kept = [h for h in group.get("hooks", [])
                if "pop_quiz.py" not in h.get("command", "")]
        if len(kept) != len(group.get("hooks", [])):
            changed = True
        if kept:
            new_groups.append({**group, "hooks": kept})
    hooks[event] = new_groups
    if not hooks[event]:
        del hooks[event]
        changed = True

if changed:
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    print(f"Stripped pop_quiz hooks from {path}")
else:
    print(f"No pop_quiz hooks found in {path} — settings unchanged")
PY
else
    echo "Settings file not found at $SETTINGS — nothing to strip"
fi

echo
echo "Done. Restart Claude Code (or open /hooks) to apply."
echo "State and journal are preserved:"
echo "  $CLAUDE_DIR/state/pop_quiz_state.json"
echo "  $CLAUDE_DIR/state/pop_quiz_state.json.lock"
echo "  $CLAUDE_DIR/state/learning_journal.md  (or wherever POP_QUIZ_JOURNAL points)"
echo "Delete them manually if you want a clean slate."
