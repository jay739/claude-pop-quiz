#!/usr/bin/env python3
"""claude-pop-quiz: a mandatory, automatic learning-check hook for Claude Code.

Fires in EVERY chat in EVERY project. Counts ALL activity PER chat (keyed by
session_id from the hook payload): the user's messages AND Claude's tool/file/
agent calls. Wired to two events with a mode argument:

  - "prompt" (UserPromptSubmit): increment the counter, and if this chat crossed
    its randomized threshold, inject a directive telling Claude to PAUSE and quiz
    the user. Also resolves a pending quiz (was the user's message an answer or a
    defer?) and enforces the defer limit.
  - "tool"   (PreToolUse, all tools): increment the counter SILENTLY, collect the
    tool name and any file paths for context enrichment, and if the learning check
    is LOCKED (defer limit hit), DENY the tool call so no work proceeds until the
    user takes the quiz.
  - "status" (CLI only): pretty-print current state, counters, and stats.

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
  POP_QUIZ_MIN               lower bound of the random threshold  (default 40)
  POP_QUIZ_MAX               upper bound of the random threshold  (default 45)
  POP_QUIZ_QUESTIONS         number of questions to ask           (default 5)
  POP_QUIZ_FORMAT            essay | mcq | mixed                  (default essay)
  POP_QUIZ_DEFER_LIMIT       consecutive defers before tools FREEZE; 0 = never
                             freeze (soft mode)                   (default 0)
  POP_QUIZ_JOURNAL           journal markdown path
                             (default <claude-dir>/state/learning_journal.md)
  POP_QUIZ_JOURNAL_MAX_ENTRIES  keep at most this many dated entries in the
                             journal; 0 = unlimited               (default 0)

Portable: state AND the default journal live under <claude-dir>/state/, so
copying ~/.claude/ to another machine just works — no absolute paths baked in.
Defers are tracked GLOBALLY (across chats), so a new chat can't dodge the limit.

Per-project topics: place a .pop-quiz-topics file in the project root (one topic
per line, lines starting with # are comments). The hook walks up from cwd to ~
and injects any found topics into the quiz directive for targeted questions.

License: MIT.
"""
import contextlib
import fcntl
import json
import os
import random
import re
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # e.g. ~/.claude
STATE_DIR = os.path.join(BASE, "state")
STATE_FILE = os.path.join(STATE_DIR, "pop_quiz_state.json")
LOCK_FILE = STATE_FILE + ".lock"
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
DEFER_LIMIT = _int_env("POP_QUIZ_DEFER_LIMIT", 0)   # 0 = soft mode, never freezes
JOURNAL_MAX = _int_env("POP_QUIZ_JOURNAL_MAX_ENTRIES", 0)  # 0 = unlimited

if MIN_GAP > MAX_GAP:
    MIN_GAP, MAX_GAP = MAX_GAP, MIN_GAP

FORMAT = (os.environ.get("POP_QUIZ_FORMAT") or "essay").lower()
if FORMAT not in ("essay", "mcq", "mixed"):
    FORMAT = "essay"

# Portable default: under <claude-dir>/state/ so it travels with ~/.claude.
JOURNAL = os.environ.get("POP_QUIZ_JOURNAL") \
    or os.path.join(STATE_DIR, "learning_journal.md")


# --- answer classification ---------------------------------------------------

# "1C 2A 3D 4B 5A" / "1) c, 2) a ..." → an MCQ answer.
_MCQ_PAIR = re.compile(r"\b(\d+)\s*[)\.:\-]?\s*([A-Da-d])\b")

_DEFER_WORDS = {"defer", "defer quiz", "skip", "skip quiz", "quiz later",
                "later", "not now", "skip the quiz"}

# Messages that start with these words are work instructions, not quiz answers,
# even if they happen to be long or contain periods.
_ACTION_START = re.compile(
    r"^(run|let'?s|let me|okay|ok|can you|could you|please|go ahead|now|next|"
    r"start|begin|continue|add|create|update|fix|make|change|refactor|"
    r"write|build|deploy|install|remove|delete|check|show|tell|explain|"
    r"what|how|why|when|where|yes|no|yep|nope|sure|alright|great|thanks|"
    r"thank|i want|i need|i'?d like|also|and|but|so|actually|hmm|hm|"
    r"sounds|looks|seems|that'?s|this|those|these)\b",
    re.IGNORECASE,
)


def looks_like_mcq_answer(msg):
    # Require a majority of question-number/letter pairs so ordinary prose
    # never trips it. Use max(1, ...) so a single-question quiz (NUM_Q=1)
    # can still be satisfied — the original max(2, ...) made it impossible.
    need = max(1, NUM_Q // 2 + 1)
    return len(_MCQ_PAIR.findall(msg or "")) >= need


def looks_like_essay_answer(msg):
    t = (msg or "").strip()
    if not t:
        return False
    # Reject work instructions that are long or punctuated.
    if _ACTION_START.match(t):
        return False
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


# --- per-project topic hints -------------------------------------------------

def _project_topics():
    """Walk up from cwd to ~ looking for .pop-quiz-topics (one topic per line)."""
    d = os.getcwd()
    home = os.path.expanduser("~")
    for _ in range(12):
        path = os.path.join(d, ".pop-quiz-topics")
        if os.path.isfile(path):
            try:
                with open(path) as f:
                    return [ln.strip() for ln in f
                            if ln.strip() and not ln.startswith("#")]
            except Exception:
                return []
        parent = os.path.dirname(d)
        if d == home or parent == d:
            break
        d = parent
    return []


# --- tools-seen summary for context enrichment -------------------------------

def _tools_summary(sess):
    seen = sess.get("tools_seen") or {}
    if not seen:
        return ""
    parts = []
    for tool, files in sorted(seen.items()):
        if files:
            parts.append(f"{tool}({', '.join(files[:5])})")
        else:
            parts.append(tool)
    return "Tools used this session: " + ", ".join(parts) + ". "


# --- directives Claude receives ----------------------------------------------

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


def quiz_directive(count, defers, sess):
    pressure = ""
    if DEFER_LIMIT:
        left = max(0, DEFER_LIMIT - defers)
        pressure = (f"The user may DEFER (say 'defer' / 'skip quiz' or just keep "
                    f"working), but defers are tracked across all chats: {defers} "
                    f"used, {left} left before ALL tool use freezes until they "
                    f"take a quiz. Mention this if they try to skip. ")
    tools_hint = _tools_summary(sess)
    topics = _project_topics()
    topic_hint = (f"Project-specific topics to prioritise: "
                  f"{', '.join(topics[:10])}. ") if topics else ""
    return (
        f"MANDATORY {NUM_Q}-QUESTION LEARNING CHECK (auto-fired after {count} "
        "actions — your messages plus Claude's tool/file/agent calls — in this "
        "chat). Before doing anything else this turn, PAUSE normal work and quiz "
        f"the user: ask EXACTLY {NUM_Q} focused questions on the TECHNICAL "
        "SUBSTANCE of THIS chat — (a) jargon/terminology used, (b) the scripts or "
        "code written/changed and WHY they work that way, (c) the project files "
        "touched and what each does. Pick the meatiest concepts, NOT conversational "
        f"trivia. {tools_hint}{topic_hint}" + _format_clause() + pressure +
        "Do NOT grade yet — just present the questions and wait for their reply. "
        "Tell the user this check fired automatically and is a mandatory part of "
        "their learning loop."
    )


def grade_directive(unlocked, seconds):
    took = f"They answered in about {seconds}s. " if seconds is not None else ""
    unlock_note = "Tool use is now UNLOCKED. " if unlocked else ""
    trim_note = ""
    if JOURNAL_MAX:
        trim_note = (
            f"After appending, trim the journal so it contains at most "
            f"{JOURNAL_MAX} dated sections (## 📅 ... headers), removing the "
            "oldest ones from the bottom. Update the summary table at the top "
            "to match. "
        )
    return (
        f"The user just answered the pending learning check. {took}{unlock_note}"
        "Grade each question now: brief feedback, correct any mistakes, give a "
        "verdict (correct / partial / missed) and 1-2 study links per question, "
        "and flag real gaps. THEN append the results to the learning journal at "
        f"{JOURNAL} (create it with a short header if missing): a section dated "
        "today with a one-line topic summary and, for EACH question, a bullet "
        "with the question, the user's answer in brief, the correct answer, the "
        f"verdict, and the links. Newest entry first. {trim_note}"
        "Keep it concise. Then resume what they were doing."
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


# --- state with file locking -------------------------------------------------

@contextlib.contextmanager
def _exclusive_lock():
    """Hold an exclusive flock for the duration of the read-modify-write cycle."""
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(LOCK_FILE, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        yield


def load():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save(state):
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


# --- status command ----------------------------------------------------------

def cmd_status():
    state = load()
    g = state.get(GLOBAL_KEY, {})
    print("=== pop-quiz status ===")
    print(f"State file : {STATE_FILE}")
    print(f"Journal    : {JOURNAL}")
    print(f"Format     : {FORMAT}   threshold: {MIN_GAP}–{MAX_GAP} actions   "
          f"questions: {NUM_Q}   max-entries: {JOURNAL_MAX or 'unlimited'}")
    defer_str = (f"{g.get('defers', 0)}/{DEFER_LIMIT}" if DEFER_LIMIT
                 else f"{g.get('defers', 0)} (soft mode — no freeze)")
    print(f"Defers     : {defer_str}")
    print(f"Locked     : {bool(g.get('locked', False))}")
    stats = g.get("stats", {})
    taken = stats.get("quizzes_taken", 0)
    if taken:
        answered = stats.get("quizzes_answered", 0)
        deferred = stats.get("quizzes_deferred", 0)
        print(f"Quizzes    : {taken} fired · {answered} answered · {deferred} deferred")
    else:
        print("Quizzes    : none yet this install")
    sessions = {k: v for k, v in state.items()
                if k != GLOBAL_KEY and isinstance(v, dict)}
    if sessions:
        now = time.time()
        print(f"\nActive sessions ({len(sessions)}):")
        for sid, s in sorted(sessions.items(), key=lambda x: -x[1].get("ts", 0)):
            age_m = int((now - s.get("ts", now)) / 60)
            seen = s.get("tools_seen", {})
            tools_hint = (f"  tools=[{', '.join(sorted(seen.keys())[:4])}]"
                          if seen else "")
            print(f"  {sid[:20]}  count={s.get('count', 0)}/{s.get('threshold', '?')}  "
                  f"pending={s.get('pending', False)}{tools_hint}  ({age_m}m ago)")
    else:
        print("\nNo active sessions.")


# --- main --------------------------------------------------------------------

def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "prompt"

    if mode == "status":
        cmd_status()
        return

    payload = {}
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        pass
    sid = str(payload.get("session_id") or "default")
    msg = payload.get("prompt") or ""

    with _exclusive_lock():
        state = load()
        now = time.time()
        state = {k: v for k, v in state.items()
                 if k == GLOBAL_KEY or (isinstance(v, dict)
                                        and now - v.get("ts", now) < PRUNE_AFTER)}

        g = state.setdefault(GLOBAL_KEY, {"defers": 0, "locked": False, "stats": {}})
        g.setdefault("stats", {})
        g["ts"] = now
        sess = state.setdefault(sid, {"count": 0, "threshold": 0})
        sess["ts"] = now
        locked = bool(DEFER_LIMIT) and bool(g.get("locked"))

        # --- PreToolUse: count, collect context, freeze while locked ---------
        if mode == "tool":
            sess["count"] = int(sess.get("count", 0)) + 1

            # Collect tool name + file paths for targeted quiz questions.
            tool_name = payload.get("tool_name") or ""
            if not tool_name:
                tool_name = (payload.get("tool") or {}).get("name", "")
            if tool_name:
                tool_input = payload.get("tool_input") or {}
                files = []
                for key in ("file_path", "path"):
                    val = tool_input.get(key, "")
                    if val and isinstance(val, str):
                        base = os.path.basename(val)
                        if base:
                            files.append(base)
                seen = sess.setdefault("tools_seen", {})
                existing = seen.setdefault(tool_name, [])
                for fname in files:
                    if fname not in existing:
                        existing.append(fname)
                        if len(existing) > 10:
                            existing.pop(0)

            save(state)
            if locked:
                _deny("Learning-check lock: defer limit reached. Answer the pending "
                      "quiz (one line, e.g. \"1C 2A 3D 4B 5A\") to unlock.")
            sys.exit(0)

        # --- UserPromptSubmit ------------------------------------------------
        count = int(sess.get("count", 0)) + 1
        threshold = int(sess.get("threshold", 0))
        if not (MIN_GAP <= threshold <= MAX_GAP):
            threshold = random.randint(MIN_GAP, MAX_GAP)

        stats = g.setdefault("stats", {})

        # 1) Locked: only a quiz answer unlocks.
        if locked:
            if classify(msg) == "answer":
                secs = (int(now - sess["issued"]) if sess.get("issued") else None)
                g["locked"] = False
                g["defers"] = 0
                stats["quizzes_answered"] = stats.get("quizzes_answered", 0) + 1
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
                secs = (int(now - sess["issued"]) if sess.get("issued") else None)
                g["defers"] = 0
                stats["quizzes_answered"] = stats.get("quizzes_answered", 0) + 1
                save(state)
                _inject(grade_directive(unlocked=False, seconds=secs))
            else:
                g["defers"] = int(g.get("defers", 0)) + 1
                stats["quizzes_deferred"] = stats.get("quizzes_deferred", 0) + 1
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
            stats["quizzes_taken"] = stats.get("quizzes_taken", 0) + 1
            save(state)
            _inject(quiz_directive(count, int(g.get("defers", 0)), sess))
        else:
            sess["count"] = count
            sess["threshold"] = threshold
            save(state)
        sys.exit(0)


if __name__ == "__main__":
    main()
