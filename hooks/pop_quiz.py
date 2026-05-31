#!/usr/bin/env python3
"""claude-pop-quiz: a mandatory, automatic learning-check hook for Claude Code.

Fires in EVERY chat in EVERY project. Counts ALL activity PER chat (keyed by
session_id from the hook payload): both the user's messages AND Claude's
tool/file/agent calls. Wired to two events with a mode argument:

  - "prompt" (UserPromptSubmit): increment the counter, and if this chat has
    crossed its randomized threshold, inject a directive telling Claude to PAUSE
    and give the user a short pop quiz, then reset the counter.
  - "tool"   (PreToolUse, all tools): increment the counter SILENTLY and never
    inject — so the quiz only ever surfaces at the START of a user message and
    never interrupts Claude mid-task.

Why count tool calls too? An agentic chat can do a lot of work across very few
typed messages. Counting only messages would let that work go unexamined.

After grading, Claude appends the results (each question, your answer, the
correct answer, a verdict, and study links) to a markdown learning journal —
a running revision log you can re-read or push to git.

Configuration (environment variables, all optional):
  POP_QUIZ_MIN        lower bound of the random threshold (default 40)
  POP_QUIZ_MAX        upper bound of the random threshold (default 45)
  POP_QUIZ_QUESTIONS  number of questions to ask           (default 5)
  POP_QUIZ_JOURNAL    path to the journal markdown file
                      (default <claude-dir>/state/learning_journal.md)

State is stored next to this script under <claude-dir>/state/, so the whole
~/.claude/ folder can be copied to another machine and it just works.

License: MIT.
"""
import json
import os
import random
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # e.g. ~/.claude
STATE_DIR = os.path.join(BASE, "state")
STATE_FILE = os.path.join(STATE_DIR, "pop_quiz_state.json")
PRUNE_AFTER = 30 * 24 * 3600  # forget sessions untouched for 30 days


def _int_env(name, default):
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


MIN_GAP = _int_env("POP_QUIZ_MIN", 40)
MAX_GAP = _int_env("POP_QUIZ_MAX", 45)
NUM_Q = _int_env("POP_QUIZ_QUESTIONS", 5)
if MIN_GAP > MAX_GAP:
    MIN_GAP, MAX_GAP = MAX_GAP, MIN_GAP

JOURNAL = os.environ.get("POP_QUIZ_JOURNAL") \
    or os.path.join(STATE_DIR, "learning_journal.md")


def directive(count):
    return (
        f"MANDATORY {NUM_Q}-QUESTION LEARNING CHECK (auto-fired after {count} "
        "actions — your messages plus Claude's tool/file/agent calls — in this "
        "chat). Before doing anything else this turn, PAUSE normal work and give "
        f"the user a short test: ask EXACTLY {NUM_Q} focused questions on the "
        "TECHNICAL SUBSTANCE of THIS chat — specifically (a) the jargon/terminology "
        "used, (b) the scripts or code written/changed and WHY they work that way, "
        "and (c) the project files touched and what each does. Aim the questions at "
        "the meatiest concepts that appeared, NOT at conversational trivia (don't "
        "quiz on what was asked or in what order). Make the user answer in their "
        "own words — do not answer for them. After they respond, give brief "
        "feedback, correct mistakes, and flag real gaps to study. Then resume what "
        "they were doing. Tell the user this check fired automatically and is a "
        "mandatory part of their learning loop. FINALLY, after grading, append the "
        f"results to the learning journal at {JOURNAL} (create it with a short "
        "header if it does not exist): add a section dated today with a one-line "
        "topic summary, and for EACH question a bullet containing the question, the "
        "user's answer in their own words (brief), the correct answer, a verdict "
        "(correct / partial / missed), and 1-2 study links on the topic. Newest "
        "entry first. Keep it concise — it is the user's personal revision log."
    )


def load():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save(state):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_FILE)  # atomic write


def main():
    # mode: "prompt" (UserPromptSubmit) may inject; "tool" (PreToolUse) only counts
    mode = sys.argv[1] if len(sys.argv) > 1 else "prompt"

    payload = {}
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        pass
    sid = str(payload.get("session_id") or "default")

    state = load()
    now = time.time()
    # prune stale sessions so the state file stays small
    state = {k: v for k, v in state.items()
             if isinstance(v, dict) and now - v.get("ts", now) < PRUNE_AFTER}

    entry = state.get(sid, {})
    count = int(entry.get("count", 0))
    threshold = int(entry.get("threshold", 0))
    if not (MIN_GAP <= threshold <= MAX_GAP):
        threshold = random.randint(MIN_GAP, MAX_GAP)
    count += 1

    # Only fire at a message boundary, so the quiz never interrupts mid-task.
    if mode == "prompt" and count >= threshold:
        state[sid] = {"count": 0, "threshold": random.randint(MIN_GAP, MAX_GAP), "ts": now}
        save(state)
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": directive(count),
            }
        }))
    else:
        state[sid] = {"count": count, "threshold": threshold, "ts": now}
        save(state)
    sys.exit(0)


if __name__ == "__main__":
    main()
