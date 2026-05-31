#!/usr/bin/env python3
"""claude-pop-quiz: a mandatory, automatic learning-check hook for Claude Code.

Fires in EVERY chat in EVERY project. Counts ALL activity PER chat (keyed by
session_id from the hook payload): the user's messages AND Claude's tool/file/
agent calls. Wired to two events with a mode argument:

  - "prompt" (UserPromptSubmit): increment the counter, and if this chat crossed
    its randomized threshold, inject a directive telling Claude to PAUSE and quiz
    the user. Also resolves a pending quiz (was the user's message an answer or a
    defer?) and enforces the defer limit.
  - "tool"   (PreToolUse, all tools): increment the counter SILENTLY; and if the
    learning check is LOCKED (defer limit hit), DENY the tool call so no work
    proceeds until the user takes the quiz.

Why count tool calls too? An agentic chat can do a lot across few typed
messages; counting only messages would let that work go unexamined.

After grading, Claude appends the results (question, your answer, the correct
answer, a verdict, study links, and how long you took) to a markdown learning
journal — a running revision log you can re-read or push to git.

Enforcement model (honest about its limits): the hook can check an answer's
SHAPE (the one-line "1C 2A 3D" pattern), not its correctness — only the model
grades understanding. And since it's your own settings + script, you can always
disable it. It's a discipline gate, not a vault. The hard part it DOES do well:
once you've deferred POP_QUIZ_DEFER_LIMIT times, tool use is frozen until you
submit a quiz answer.

Configuration (environment variables, all optional):
  POP_QUIZ_MIN          lower bound of the random threshold  (default 40)
  POP_QUIZ_MAX          upper bound of the random threshold  (default 45)
  POP_QUIZ_QUESTIONS    number of questions to ask           (default 5)
  POP_QUIZ_FORMAT       essay | mcq | mixed                  (default essay)
  POP_QUIZ_DEFER_LIMIT  consecutive defers before tools FREEZE; 0 = never
                        freeze (soft mode)                   (default 0)
  POP_QUIZ_JOURNAL      journal markdown path
                        (default <claude-dir>/state/learning_journal.md)

Portable: state AND the default journal live under <claude-dir>/state/, so
copying ~/.claude/ to another machine just works — no absolute paths baked in.
Defers are tracked GLOBALLY (across chats), so a new chat can't dodge the limit.
License: MIT.
"""
import json
import os
import random
import re
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # e.g. ~/.claude
STATE_DIR = os.path.join(BASE, "state")
STATE_FILE = os.path.join(STATE_DIR, "pop_quiz_state.json")
PRUNE_AFTER = 30 * 24 * 3600  # forget sessions untouched for 30 days
GLOBAL_KEY = "_global"        # holds the cross-chat defer counter + lock flag


def _int_env(name, default):
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


MIN_GAP = _int_env("POP_QUIZ_MIN", 40)
MAX_GAP = _int_env("POP_QUIZ_MAX", 45)
NUM_Q = _int_env("POP_QUIZ_QUESTIONS", 5)
DEFER_LIMIT = _int_env("POP_QUIZ_DEFER_LIMIT", 0)  # 0 = soft mode, never freezes
if MIN_GAP > MAX_GAP:
    MIN_GAP, MAX_GAP = MAX_GAP, MIN_GAP

FORMAT = (os.environ.get("POP_QUIZ_FORMAT") or "essay").lower()
if FORMAT not in ("essay", "mcq", "mixed"):
    FORMAT = "essay"

# Portable default: under <claude-dir>/state/ so it travels with ~/.claude.
JOURNAL = os.environ.get("POP_QUIZ_JOURNAL") \
    or os.path.join(STATE_DIR, "learning_journal.md")

# --- classifying the user's reply to a pending quiz --------------------------
# "1C 2A 3D 4B 5A" / "1) c, 2) a ..." → an MCQ answer. We require a majority of
# question-letter pairs so ordinary prose never trips it.
_MCQ_PAIR = re.compile(r"\b(\d+)\s*[)\.:\-]?\s*([A-Da-d])\b")
_DEFER_WORDS = {"defer", "defer quiz", "skip", "skip quiz", "quiz later",
                "later", "not now", "skip the quiz"}


def looks_like_mcq_answer(msg):
    need = max(2, NUM_Q // 2 + 1)
    return len(_MCQ_PAIR.findall(msg or "")) >= need


def looks_like_essay_answer(msg):
    t = (msg or "").strip()
    return len(t) >= 80 or t.count(".") >= 2


def is_explicit_defer(msg):
    return (msg or "").strip().lower().rstrip(".!").strip() in _DEFER_WORDS


def classify(msg):
    """Was the user's message an answer to the pending quiz, or a defer?"""
    if is_explicit_defer(msg):
        return "defer"
    if looks_like_mcq_answer(msg):
        return "answer"
    if FORMAT in ("essay", "mixed") and looks_like_essay_answer(msg):
        return "answer"
    return "defer"  # mcq mode, or a short/off-topic message → treat as a defer


# --- directives Claude receives ---------------------------------------------
def _format_clause():
    if FORMAT == "mcq":
        return (f"Present all {NUM_Q} questions as MULTIPLE CHOICE: 4 options "
                "A-D each, one correct, plausible distractors. The user answers "
                "in ONE line of letters (e.g. \"1C 2A 3D 4B 5A\") — seconds, no "
                "essays. ")
    if FORMAT == "mixed":
        return (f"Ask the first half of the {NUM_Q} questions as short free "
                "response and the rest as MULTIPLE CHOICE (A-D, one line of "
                "letters). ")
    return ("Make the user answer in their own words. If they are short on time, "
            "offer the quick version: the SAME questions as MULTIPLE CHOICE "
            "(A-D, one-line letters). ")


def quiz_directive(count, defers):
    pressure = ""
    if DEFER_LIMIT:
        left = max(0, DEFER_LIMIT - defers)
        pressure = (f"The user may DEFER (say 'defer' / 'skip quiz' or just keep "
                    f"working), but defers are tracked across all chats: {defers} "
                    f"used, {left} left before ALL tool use freezes until they "
                    f"take a quiz. Mention this if they try to skip. ")
    return (
        f"MANDATORY {NUM_Q}-QUESTION LEARNING CHECK (auto-fired after {count} "
        "actions — your messages plus Claude's tool/file/agent calls — in this "
        "chat). Before doing anything else this turn, PAUSE normal work and quiz "
        f"the user: ask EXACTLY {NUM_Q} focused questions on the TECHNICAL "
        "SUBSTANCE of THIS chat — (a) jargon/terminology used, (b) the scripts or "
        "code written/changed and WHY they work that way, (c) the project files "
        "touched and what each does. Pick the meatiest concepts, NOT conversational "
        "trivia. " + _format_clause() + pressure +
        "Do NOT grade yet — just present the questions and wait for their reply. "
        "Tell the user this check fired automatically and is a mandatory part of "
        "their learning loop."
    )


def grade_directive(unlocked, seconds):
    took = f"They answered in about {seconds}s. " if seconds is not None else ""
    unlock_note = ("Tool use is now UNLOCKED. " if unlocked else "")
    return (
        f"The user just answered the pending learning check. {took}{unlock_note}"
        "Grade each question now: brief feedback, correct any mistakes, give a "
        "verdict (correct / partial / missed) and 1-2 study links per question, "
        "and flag real gaps. THEN append the results to the learning journal at "
        f"{JOURNAL} (create it with a short header if missing): a section dated "
        "today with a one-line topic summary and, for EACH question, a bullet "
        "with the question, the user's answer in brief, the correct answer, the "
        "verdict, and the links. Newest entry first. Keep it concise. Then resume "
        "what they were doing."
    )


def locked_directive(defers):
    return (
        f"LEARNING-CHECK LOCK. The user has deferred {defers} times (limit "
        f"{DEFER_LIMIT}); ALL tool use is now FROZEN and stays frozen until they "
        f"answer a quiz. Do NOT attempt the user's task or any tool call. Present "
        f"the {NUM_Q} questions as one-line MULTIPLE CHOICE (A-D) and tell the "
        "user plainly: they must reply with their answers (e.g. \"1C 2A 3D 4B "
        "5A\") to unlock — nothing else will proceed. Be matter-of-fact, not "
        "preachy."
    )


def defer_ack_directive(defers):
    left = max(0, DEFER_LIMIT - defers) if DEFER_LIMIT else None
    if left is None:
        return ("The user deferred the learning check. Note it briefly and "
                "continue — but encourage them to take the quick MCQ version "
                "soon.")
    return (f"The user deferred the learning check ({defers}/{DEFER_LIMIT}; "
            f"{left} left before tool use freezes). Note this in one line so they "
            "feel the ramp, then continue with their task.")


# --- state -------------------------------------------------------------------
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


def _inject(context):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit", "additionalContext": context}}))


def _deny(reason):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason}}))


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "prompt"

    payload = {}
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        pass
    sid = str(payload.get("session_id") or "default")
    msg = payload.get("prompt") or ""

    state = load()
    now = time.time()
    state = {k: v for k, v in state.items()
             if k == GLOBAL_KEY or (isinstance(v, dict)
                                    and now - v.get("ts", now) < PRUNE_AFTER)}

    g = state.setdefault(GLOBAL_KEY, {"defers": 0, "locked": False})
    g["ts"] = now
    sess = state.setdefault(sid, {"count": 0, "threshold": 0})
    sess["ts"] = now
    locked = bool(DEFER_LIMIT) and bool(g.get("locked"))

    # --- PreToolUse: count, and freeze tools while locked --------------------
    if mode == "tool":
        sess["count"] = int(sess.get("count", 0)) + 1
        save(state)
        if locked:
            _deny("Learning-check lock: defer limit reached. Answer the pending "
                  "quiz (one line, e.g. \"1C 2A 3D 4B 5A\") to unlock.")
        sys.exit(0)

    # --- UserPromptSubmit ----------------------------------------------------
    count = int(sess.get("count", 0)) + 1
    threshold = int(sess.get("threshold", 0))
    if not (MIN_GAP <= threshold <= MAX_GAP):
        threshold = random.randint(MIN_GAP, MAX_GAP)

    # 1) Locked: only a quiz answer unlocks.
    if locked:
        if classify(msg) == "answer":
            secs = int(now - sess.get("issued", now)) if sess.get("issued") else None
            g["locked"] = False
            g["defers"] = 0
            sess["pending"] = False
            sess["count"] = 0
            sess["threshold"] = random.randint(MIN_GAP, MAX_GAP)
            save(state)
            _inject(grade_directive(unlocked=True, seconds=secs))
        else:
            sess["count"] = count
            save(state)
            _inject(locked_directive(int(g.get("defers", 0))))
        sys.exit(0)

    # 2) A quiz is pending — was this reply an answer or a defer?
    if sess.get("pending"):
        sess["pending"] = False
        sess["count"] = count
        if classify(msg) == "answer":
            secs = int(now - sess.get("issued", now)) if sess.get("issued") else None
            g["defers"] = 0
            save(state)
            _inject(grade_directive(unlocked=False, seconds=secs))
        else:
            g["defers"] = int(g.get("defers", 0)) + 1
            if DEFER_LIMIT and g["defers"] >= DEFER_LIMIT:
                g["locked"] = True
                save(state)
                _inject(locked_directive(g["defers"]))
            else:
                save(state)
                _inject(defer_ack_directive(g["defers"]))
        sys.exit(0)

    # 3) Otherwise: fire a new quiz if this chat crossed its threshold.
    if count >= threshold:
        sess["pending"] = True
        sess["issued"] = now
        sess["count"] = 0
        sess["threshold"] = random.randint(MIN_GAP, MAX_GAP)
        save(state)
        _inject(quiz_directive(count, int(g.get("defers", 0))))
    else:
        sess["count"] = count
        sess["threshold"] = threshold
        save(state)
    sys.exit(0)


if __name__ == "__main__":
    main()
