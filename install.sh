#!/usr/bin/env bash
# claude-pop-quiz installer.
# Copies the hook into ~/.claude/hooks/ and merges the hook config into
# ~/.claude/settings.json (preserving everything already there).
# Idempotent: safe to re-run. Requires python3; uses it for the JSON merge.
#
# A re-run UPGRADES AN EXISTING INSTALL IN PLACE, including one whose script was
# renamed (the README blesses renaming, and `update` keeps the installed
# filename). Matching only the literal string "pop_quiz.py" meant a renamed
# install was invisible here: the merge found no existing hook and APPENDED a
# second one, so both ran, the action counter double-counted, and quizzes fired
# at half the configured cadence.
set -euo pipefail

CLAUDE_DIR="${CLAUDE_DIR:-$HOME/.claude}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOOK_SRC="$SRC_DIR/hooks/pop_quiz.py"
HOOK_DST="$CLAUDE_DIR/hooks/pop_quiz.py"
SETTINGS="$CLAUDE_DIR/settings.json"

mkdir -p "$CLAUDE_DIR/hooks"

# Find an existing pop-quiz hook (under any script name) so we upgrade THAT
# file and keep its name, rather than orphaning it next to a new copy.
EXISTING="$(
    python3 - "$SETTINGS" <<'PY'
import json, os, re, sys

try:
    with open(sys.argv[1]) as f:
        cfg = json.load(f)
except Exception:
    sys.exit(0)

MARKERS = ("pop_quiz", "grill_reminder", "POP_QUIZ_")
for groups in (cfg.get("hooks") or {}).values():
    for group in groups or []:
        for h in (group or {}).get("hooks", []):
            cmd = (h or {}).get("command", "")
            if not any(m in cmd for m in MARKERS):
                continue
            m = re.search(r"(\S+\.py)\s+(?:prompt|tool)\b", cmd)
            if not m:
                continue
            path = os.path.expanduser(os.path.expandvars(m.group(1)))
            if os.path.isfile(path):
                print(path)
                sys.exit(0)
PY
)"

if [ -n "$EXISTING" ] && [ "$EXISTING" != "$HOOK_DST" ]; then
    HOOK_DST="$EXISTING"
    echo "Found an existing install at $HOOK_DST — upgrading it in place."
fi

cp "$HOOK_SRC" "$HOOK_DST"
chmod +x "$HOOK_DST"
echo "Installed hook -> $HOOK_DST"

# Optional config, baked into the hook commands. Pre-set via env, or answer the
# prompts when run interactively. Anything already baked into the existing
# command is carried forward by the merge step, so a re-run never silently drops
# a knob you set by hand.
#
# Only knobs the caller ACTUALLY set are exported. Defaulting them here with
# ${VAR:-essay} would make "unset" indistinguishable from "explicitly essay",
# and the merge step would then overwrite a carried-forward setting with a
# default the caller never asked for.
if [ -t 0 ]; then
    read -r -p "Quiz format essay|mcq|mixed [keep current]: " _f || true
    [ -n "${_f:-}" ] && POP_QUIZ_FORMAT="$_f"
    read -r -p "Defer limit before tools freeze, 0=off [keep current]: " _d || true
    [ -n "${_d:-}" ] && POP_QUIZ_DEFER_LIMIT="$_d"
    read -r -p "Max journal entries to keep, 0=unlimited [keep current]: " _m || true
    [ -n "${_m:-}" ] && POP_QUIZ_JOURNAL_MAX_ENTRIES="$_m"
fi
for _v in POP_QUIZ_FORMAT POP_QUIZ_DEFER_LIMIT POP_QUIZ_JOURNAL_MAX_ENTRIES; do
    [ -n "${!_v:-}" ] && export "$_v"
done

python3 - "$SETTINGS" "$HOOK_DST" "$HOME" <<'PY'
import json, os, re, sys

path, hook_dst, home = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    with open(path) as f:
        cfg = json.load(f)
except Exception:
    cfg = {}

hooks = cfg.setdefault("hooks", {})
MARKERS = ("pop_quiz", "grill_reminder", "POP_QUIZ_")
_ENV_PAIR = re.compile(r"\b(POP_QUIZ_[A-Z_]+)=(\"[^\"]*\"|\S+)")


def _find_existing():
    """The first pop-quiz hook command already in settings, under any name."""
    for groups in hooks.values():
        for group in groups or []:
            for h in (group or {}).get("hooks", []):
                cmd = (h or {}).get("command", "")
                if any(m in cmd for m in MARKERS):
                    return cmd
    return ""


# Carry forward every knob already baked into the wired command, so a re-run
# preserves flags that were set by hand (POP_QUIZ_AUTO_UPDATE lived only in
# settings.json, and a reinstall used to drop it silently).
carried = {
    k: v.strip('"') for k, v in _ENV_PAIR.findall(_find_existing())
}

# Knobs this run is explicitly setting. Every variable the README documents is
# honoured here, so re-running with e.g. POP_QUIZ_MIN/MAX/QUESTIONS actually
# changes the baked command.
_KNOBS = [
    ("POP_QUIZ_FORMAT", "essay"),
    ("POP_QUIZ_DEFER_LIMIT", "0"),
    ("POP_QUIZ_JOURNAL_MAX_ENTRIES", "0"),
    ("POP_QUIZ_MIN", ""),
    ("POP_QUIZ_MAX", ""),
    ("POP_QUIZ_QUESTIONS", ""),
    ("POP_QUIZ_JOURNAL", ""),
    ("POP_QUIZ_AUTO_UPDATE", ""),
    ("POP_QUIZ_CHANNEL", ""),
    ("POP_QUIZ_REPO", ""),
    ("POP_QUIZ_BRANCH", ""),
    ("POP_QUIZ_NO_UPDATE_CHECK", ""),
]
chosen = dict(carried)
for name, default in _KNOBS:
    val = os.environ.get(name) or ""
    if not val:
        continue  # not set this run: keep whatever the install already had
    if val == default and name not in chosen:
        continue  # a fresh install at the default needs no explicit override
    chosen[name] = val

script = hook_dst
if script.startswith(home + os.sep):
    script = "~" + script[len(home) :]

prefix = "".join(
    (f'{k}="{v}" ' if " " in v else f"{k}={v} ") for k, v in sorted(chosen.items())
)


def ensure(event, mode):
    # Both commands get the same config: the tool-mode hook needs
    # POP_QUIZ_DEFER_LIMIT to know whether to freeze, not just the prompt hook.
    cmd = f"{prefix}python3 {script} {mode} 2>/dev/null || true"
    arr = hooks.setdefault(event, [])
    # Refresh an existing pop-quiz hook rather than adding a second one. Matched
    # on any of MARKERS so a renamed script is still recognised as ours.
    for group in arr:
        for h in group.get("hooks", []):
            if any(m in h.get("command", "") for m in MARKERS):
                h["command"] = cmd
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
kept = sorted(k for k in carried if chosen.get(k) == carried[k])
if kept:
    print("Carried forward from the existing install: " + ", ".join(kept))
print(
    "Effective config: "
    + (", ".join(f"{k}={v}" for k, v in sorted(chosen.items())) or "all defaults")
)
PY

echo
echo "Done. Open /hooks in Claude Code once (or restart) to load the new config."
echo "New chats pick it up automatically. Default cadence is every 40-45 actions."
echo "Graded results are journaled to: $CLAUDE_DIR/state/learning_journal.md"
echo "  (override with POP_QUIZ_JOURNAL=/path/to/journal.md)"
echo "Re-run with env to change, e.g.:"
echo "  POP_QUIZ_FORMAT=mcq POP_QUIZ_DEFER_LIMIT=3 ./install.sh          # quick MCQ + freeze after 3 defers"
echo "  POP_QUIZ_MIN=90 POP_QUIZ_MAX=110 POP_QUIZ_QUESTIONS=5            # quiz less often"
echo "  POP_QUIZ_JOURNAL_MAX_ENTRIES=100 ./install.sh                    # keep last 100 sessions"
echo
echo "Check everything is healthy: python3 $HOOK_DST doctor"
echo "To inspect state at any time:  python3 $HOOK_DST status"
echo "To remove: ./uninstall.sh"
