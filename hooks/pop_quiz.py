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
  - "update" (CLI only): self-update — download the latest hook from GitHub and
    overwrite this script in place (keeps its installed filename; backs up .bak).

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
  POP_QUIZ_REPO              owner/repo checked for updates
                             (default jay739/claude-pop-quiz)
  POP_QUIZ_BRANCH            branch checked for updates           (default main)
  POP_QUIZ_NO_UPDATE_CHECK   set to any value to disable the daily online
                             version check entirely               (default off)

Updates: once a day (on UserPromptSubmit) the hook quietly checks GitHub for a
newer __version__ and, if found, appends a one-line upgrade nudge to its next
injected message. The check is throttled, time-boxed, and fully offline-safe —
any failure is swallowed so the hook never breaks. Run "update" to self-upgrade.

Portable: state AND the default journal live under <claude-dir>/state/, so
copying ~/.claude/ to another machine just works — no absolute paths baked in.
Defers are tracked GLOBALLY (across chats), so a new chat can't dodge the limit.

Per-project topics: place a .pop-quiz-topics file in the project root (one topic
per line, lines starting with # are comments). The hook walks up from cwd to ~
and injects any found topics into the quiz directive for targeted questions.

Spaced repetition: the grading journal is parsed back on each fire — topics the
user previously scored partial/missed on are fed into the next quiz so weak
areas resurface. The same parse powers a lifetime-accuracy line in "status".

Robustness: harness-injected prompts (background/terminal task completions,
[SYSTEM NOTIFICATION] wrappers) pass straight through so they can't be misread
as a quiz answer and silently eat a pending check. Runs without fcntl too
(degraded, lock-free) so native Windows no longer fails silently.

License: MIT.
"""

import contextlib
import json
import os
import random
import re
import sys
import time

try:
    import fcntl  # Unix only; absent on native Windows.
except ImportError:
    fcntl = None  # Degraded mode: best-effort, no advisory lock (see _exclusive_lock).

__version__ = "0.4.0"

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # e.g. ~/.claude
STATE_DIR = os.path.join(BASE, "state")
STATE_FILE = os.path.join(STATE_DIR, "pop_quiz_state.json")
LOCK_FILE = STATE_FILE + ".lock"
PRUNE_AFTER = 30 * 24 * 3600  # forget sessions untouched for 30 days
GLOBAL_KEY = "_global"  # holds the cross-chat defer counter + lock flag
UPDATE_INTERVAL = 24 * 3600  # check GitHub for a newer version at most once/day


def _int_env(name, default):
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


MIN_GAP = _int_env("POP_QUIZ_MIN", 40)
MAX_GAP = _int_env("POP_QUIZ_MAX", 45)
NUM_Q = _int_env("POP_QUIZ_QUESTIONS", 5)
DEFER_LIMIT = _int_env("POP_QUIZ_DEFER_LIMIT", 0)  # 0 = soft mode, never freezes
JOURNAL_MAX = _int_env("POP_QUIZ_JOURNAL_MAX_ENTRIES", 0)  # 0 = unlimited

if MIN_GAP > MAX_GAP:
    MIN_GAP, MAX_GAP = MAX_GAP, MIN_GAP

FORMAT = (os.environ.get("POP_QUIZ_FORMAT") or "essay").lower()
if FORMAT not in ("essay", "mcq", "mixed"):
    FORMAT = "essay"

# Portable default: under <claude-dir>/state/ so it travels with ~/.claude.
JOURNAL = os.environ.get("POP_QUIZ_JOURNAL") or os.path.join(
    STATE_DIR, "learning_journal.md"
)


# --- answer classification ---------------------------------------------------

# "1C 2A 3D 4B 5A" / "1) c, 2) a ..." → an MCQ answer.
_MCQ_PAIR = re.compile(r"\b(\d+)\s*[)\.:\-]?\s*([A-Da-d])\b")

_DEFER_WORDS = {
    "defer",
    "defer quiz",
    "skip",
    "skip quiz",
    "quiz later",
    "later",
    "not now",
    "skip the quiz",
}

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


def is_explicit_defer(msg):
    return (msg or "").strip().lower().rstrip(".!").strip() in _DEFER_WORDS


# Harness-injected prompts (background/terminal task completions, system
# notifications) arrive on UserPromptSubmit but are NOT the human typing. They
# must never be read as a quiz answer/defer or counted toward the cadence.
_SYNTHETIC_MARKERS = ("<task-notification", "</task-notification>", "<task-id")


def _is_synthetic_prompt(msg):
    t = (msg or "").lstrip()
    if t.startswith("[SYSTEM NOTIFICATION"):
        return True
    low = t.lower()
    return any(mark in low for mark in _SYNTHETIC_MARKERS)


def classify(msg):
    """Was the user's message an answer to the pending quiz, or a defer?

    A quiz is already pending whenever this runs, so the bar to count as an
    ANSWER is deliberately low: anything that is not an explicit defer word or a
    clear new-work instruction is treated as an attempt. A concise but correct
    reply ("it memoizes with functools.lru_cache") must NOT be punished as a
    skip just because it is short — the old length/period heuristic did exactly
    that and silently consumed the quiz.
    """
    t = (msg or "").strip()
    if not t:
        return "defer"
    if is_explicit_defer(msg):
        return "defer"
    if looks_like_mcq_answer(msg):
        return "answer"
    if _ACTION_START.match(t):
        return "defer"  # they moved on to new work → soft defer
    return "answer"


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
                    return [
                        ln.strip() for ln in f if ln.strip() and not ln.startswith("#")
                    ]
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


# --- learning-journal parsing (accuracy + spaced repetition) -----------------

# Per-question entry headers written by grade_directive, e.g.
#   "### ✅ Q1 · HTTP cache headers"   /   "### 🟡 Q2 · Index trade-offs"
# This is the single source of truth for both the score tally and which topics
# the user got wrong, so the grading directive is pinned to emit this exact shape.
_Q_HEADER = re.compile(
    r"^#{2,4}\s*([✅\U0001F7E1❌])\s*Q\d+\s*[·:\-]?\s*(.*)$",
    re.MULTILINE,
)
_VERDICT_GOOD = "✅"  # ✅ correct
_VERDICT_PART = "\U0001f7e1"  # 🟡 partial
_VERDICT_MISS = "❌"  # ❌ missed


def _read_journal(max_bytes=0):
    try:
        with open(JOURNAL, encoding="utf-8") as f:
            return f.read(max_bytes) if max_bytes else f.read()
    except Exception:
        return ""


def _journal_accuracy():
    """Tally graded verdicts across the whole journal. None if nothing graded."""
    counts = {_VERDICT_GOOD: 0, _VERDICT_PART: 0, _VERDICT_MISS: 0}
    for m in _Q_HEADER.finditer(_read_journal()):
        counts[m.group(1)] += 1
    total = sum(counts.values())
    if not total:
        return None
    score = counts[_VERDICT_GOOD] + 0.5 * counts[_VERDICT_PART]
    return {
        "correct": counts[_VERDICT_GOOD],
        "partial": counts[_VERDICT_PART],
        "missed": counts[_VERDICT_MISS],
        "total": total,
        "pct": round(100 * score / total),
    }


def _recent_weak_topics(limit=5):
    """Most recent topics the user scored partial/missed on (newest-first file)."""
    topics, seen = [], set()
    for m in _Q_HEADER.finditer(_read_journal(max_bytes=65536)):
        if m.group(1) in (_VERDICT_PART, _VERDICT_MISS):
            topic = m.group(2).strip().strip("`").strip()
            key = topic.lower()
            if topic and key not in seen:
                seen.add(key)
                topics.append(topic)
                if len(topics) >= limit:
                    break
    return topics


# --- directives Claude receives ----------------------------------------------

# Anti-gaming guidance: without this the model reliably makes the correct MCQ
# option the longest / most-qualified one, so "always pick the longest" wins.
MCQ_FAIRNESS = (
    "Keep the options fair and ungameable: make all four roughly the SAME length "
    "and equally specific (NEVER let the correct one be the longest, the most "
    "detailed, or the most hedged — that is a dead giveaway), vary which letter "
    "is correct across the questions instead of clustering on one, and write "
    "distractors that encode real misconceptions rather than obviously-wrong "
    "filler. "
)


def _format_clause():
    if FORMAT == "mcq":
        return (
            f"Present all {NUM_Q} questions as MULTIPLE CHOICE: 4 options "
            "A-D each, one correct. " + MCQ_FAIRNESS + "The user answers "
            'in ONE line of letters (e.g. "1C 2A 3D 4B 5A") — seconds, no '
            "essays. "
        )
    if FORMAT == "mixed":
        return (
            f"Ask the first half of the {NUM_Q} questions as short free "
            "response and the rest as MULTIPLE CHOICE (A-D, one line of "
            "letters). " + MCQ_FAIRNESS
        )
    return (
        "Make the user answer in their own words. If they are short on time, "
        "offer the quick version: the SAME questions as MULTIPLE CHOICE "
        "(A-D, one-line letters). " + MCQ_FAIRNESS
    )


def quiz_directive(count, defers, sess):
    pressure = ""
    if DEFER_LIMIT:
        left = max(0, DEFER_LIMIT - defers)
        pressure = (
            f"The user may DEFER (say 'defer' / 'skip quiz' or just keep "
            f"working), but defers are tracked across all chats: {defers} "
            f"used, {left} left before ALL tool use freezes until they "
            f"take a quiz. Mention this if they try to skip. "
        )
    tools_hint = _tools_summary(sess)
    topics = _project_topics()
    topic_hint = (
        (f"Project-specific topics to prioritise: " f"{', '.join(topics[:10])}. ")
        if topics
        else ""
    )
    weak = _recent_weak_topics()
    weak_hint = (
        (
            f"Spaced repetition: the user previously scored partial/missed on "
            f"{', '.join(weak)} — work AT LEAST ONE question in to revisit a weak "
            f"area, phrased differently than before. "
        )
        if weak
        else ""
    )
    return (
        f"MANDATORY {NUM_Q}-QUESTION LEARNING CHECK (auto-fired after {count} "
        "actions — your messages plus Claude's tool/file/agent calls — in this "
        "chat). Before doing anything else this turn, PAUSE normal work and quiz "
        f"the user: ask EXACTLY {NUM_Q} focused questions on the TECHNICAL "
        "SUBSTANCE of THIS chat — (a) jargon/terminology used, (b) the scripts or "
        "code written/changed and WHY they work that way, (c) the project files "
        "touched and what each does. Pick the meatiest concepts, NOT conversational "
        f"trivia. {tools_hint}{topic_hint}{weak_hint}"
        + _format_clause()
        + pressure
        + "Do NOT grade yet — just present the questions and wait for their reply. "
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
        "Grade each question now: brief spoken feedback, correct any mistakes, "
        "give a verdict (correct / partial / missed) and 1-2 study links per "
        "question, and flag real gaps. THEN append the results to the learning "
        f"journal at {JOURNAL} (create it with a short header + a summary table if "
        "missing). Add a section dated today, NEWEST ENTRY FIRST, with a one-line "
        "topic summary, and render EACH question in this EXACT shape so the entry "
        "is self-contained when re-read months later:\n"
        "  ### <✅ correct | 🟡 partial | ❌ missed> Q<n> · <short topic title>\n"
        "  > <the FULL question text, verbatim — include any code snippet or the "
        "A-D options exactly as shown to the user>\n"
        "  - **Context:** <one line naming the file, function, command, or concept "
        "from THIS session the question was about, so the entry stands alone>\n"
        "  - **You said:** <the user's answer in FULL — do not truncate or "
        "paraphrase away detail>\n"
        "  - **Answer:** <the correct answer, in full>\n"
        "  - 🔗 <1-2 study links>\n"
        "Record everything COMPLETELY — never cut off or abbreviate the question, "
        "the context, the user's answer, or the correct answer; for a revision log "
        f"completeness beats brevity. {trim_note}"
        "Then resume what they were doing."
    )


def locked_directive(defers):
    return (
        f"LEARNING-CHECK LOCK. The user has deferred {defers} times (limit "
        f"{DEFER_LIMIT}); ALL tool use is now FROZEN and stays frozen until they "
        f"answer a quiz. Do NOT attempt the user's task or any tool call. Present "
        f"the {NUM_Q} questions as one-line MULTIPLE CHOICE (A-D). "
        + MCQ_FAIRNESS
        + 'Tell the user plainly: they must reply with their answers (e.g. "1C 2A '
        '3D 4B 5A") to unlock — nothing else will proceed. Be matter-of-fact, not '
        "preachy."
    )


def defer_ack_directive(defers):
    left = max(0, DEFER_LIMIT - defers) if DEFER_LIMIT else None
    if left is None:
        return (
            "The user deferred the learning check. Note it briefly and "
            "continue — but encourage them to take the quick MCQ version "
            "soon."
        )
    return (
        f"The user deferred the learning check ({defers}/{DEFER_LIMIT}; "
        f"{left} left before tool use freezes). Note this in one line so they "
        "feel the ramp, then continue with their task."
    )


# --- state with file locking -------------------------------------------------


@contextlib.contextmanager
def _exclusive_lock():
    """Hold an exclusive flock for the duration of the read-modify-write cycle.

    On platforms without fcntl (native Windows) we degrade to a best-effort,
    lock-free cycle rather than crashing the hook outright — saves stay atomic
    via os.replace, so the only exposure is a rare lost counter increment under
    heavy parallel tool calls.
    """
    os.makedirs(STATE_DIR, exist_ok=True)
    if fcntl is None:
        yield
        return
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


# Set once per UserPromptSubmit when a newer version is detected; _inject appends
# it to whatever directive (quiz / grade / defer / nudge-only) is being sent.
_NUDGE = ""


def _inject(context):
    if _NUDGE:
        context = (context + "\n\n" + _NUDGE) if context else _NUDGE
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": context,
                }
            }
        )
    )


def _deny(reason):
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
        )
    )


# --- update checking ---------------------------------------------------------


def _parse_version(s):
    nums = re.findall(r"\d+", s or "")
    return tuple(int(n) for n in nums[:3]) if nums else (0,)


def _raw_url():
    repo = os.environ.get("POP_QUIZ_REPO") or "jay739/claude-pop-quiz"
    branch = os.environ.get("POP_QUIZ_BRANCH") or "main"
    return f"https://raw.githubusercontent.com/{repo}/{branch}/hooks/pop_quiz.py"


def _fetch_remote(timeout, max_bytes=0):
    """Fetch the canonical pop_quiz.py from GitHub. Returns bytes, or None on any
    failure (offline, timeout, 404) — callers must treat None as 'unknown'."""
    import urllib.request

    try:
        with urllib.request.urlopen(_raw_url(), timeout=timeout) as r:
            return r.read(max_bytes) if max_bytes else r.read()
    except Exception:
        return None


def _remote_version(timeout=2):
    head = _fetch_remote(timeout, max_bytes=4096)
    if not head:
        return None
    m = re.search(rb'__version__\s*=\s*["\']([^"\']+)["\']', head)
    return m.group(1).decode() if m else None


def _update_precheck(now):
    """OUTSIDE the lock: decide whether a daily check is due (by peeking state)
    and, if so, do the network fetch here so we never hold the flock during I/O.
    Returns (latest_version_or_None, did_fetch)."""
    if os.environ.get("POP_QUIZ_NO_UPDATE_CHECK"):
        return None, False
    g = load().get(GLOBAL_KEY, {})
    if now - g.get("update_checked_ts", 0) < UPDATE_INTERVAL:
        return g.get("update_latest"), False
    return _remote_version(), True


def _apply_update_check(g, latest, did_fetch, now):
    """INSIDE the lock: record the check result into global state and return a
    one-line nudge if a newer version was newly detected (once per new version)."""
    if did_fetch:
        g["update_checked_ts"] = now
        if latest:
            g["update_latest"] = latest
    latest = latest or g.get("update_latest")
    if latest and _parse_version(latest) > _parse_version(__version__):
        if g.get("update_notified") != latest:
            g["update_notified"] = latest
            script = os.path.abspath(__file__)
            return (
                f"[pop-quiz] A newer version is available: v{latest} "
                f"(installed v{__version__}). Tell the user, in one line, to "
                f"upgrade by running: python3 {script} update"
            )
    return ""


def cmd_update():
    target = os.path.abspath(__file__)
    latest = _remote_version(timeout=5)
    if latest and _parse_version(latest) <= _parse_version(__version__):
        print(f"Already up to date (v{__version__}).")
        return
    data = _fetch_remote(timeout=10)
    if not data:
        print(
            "Update failed: could not reach GitHub. Check your connection "
            "(or POP_QUIZ_REPO / POP_QUIZ_BRANCH)."
        )
        return
    # Sanity-check the payload before trusting it over our running script: it
    # must look like a real, reasonably-sized pop_quiz.py, not an error page.
    markers = (b"__version__", b"def main", b"def quiz_directive", b"def classify")
    if len(data) < 4096 or any(m not in data for m in markers):
        print("Update aborted: the downloaded file does not look like pop_quiz.py.")
        return
    backup = target + ".bak"
    try:
        if os.path.exists(target):
            with open(target, "rb") as src, open(backup, "wb") as dst:
                dst.write(src.read())
        tmp = target + ".new"
        with open(tmp, "wb") as f:
            f.write(data)
        os.chmod(tmp, 0o755)
        os.replace(tmp, target)  # atomic; safe to overwrite the running script
    except Exception as e:
        print(f"Update failed while writing {target}: {e}")
        return
    print(f"Updated {os.path.basename(target)}: v{__version__} -> v{latest or '?'}")
    print(f"Backup of the previous version: {backup}")
    print("Restart Claude Code (or run /hooks) to load the new version.")


# --- status command ----------------------------------------------------------


def cmd_status():
    state = load()
    g = state.get(GLOBAL_KEY, {})
    print("=== pop-quiz status ===")
    print(f"Version    : {__version__}")
    print(f"Script     : {os.path.abspath(__file__)}")
    print(f"State file : {STATE_FILE}")
    print(f"Journal    : {JOURNAL}")
    print(
        f"Format     : {FORMAT}   threshold: {MIN_GAP}–{MAX_GAP} actions   "
        f"questions: {NUM_Q}   max-entries: {JOURNAL_MAX or 'unlimited'}"
    )
    defer_str = (
        f"{g.get('defers', 0)}/{DEFER_LIMIT}"
        if DEFER_LIMIT
        else f"{g.get('defers', 0)} (soft mode — no freeze)"
    )
    print(f"Defers     : {defer_str}")
    print(f"Locked     : {bool(g.get('locked', False))}")
    # Live update check (explicit user action — network is fine here), with the
    # cached value as fallback when offline.
    latest = (
        None
        if os.environ.get("POP_QUIZ_NO_UPDATE_CHECK")
        else _remote_version(timeout=3)
    ) or g.get("update_latest")
    if latest and _parse_version(latest) > _parse_version(__version__):
        print(
            f"Update     : v{latest} available — run "
            f"`python3 {os.path.abspath(__file__)} update`"
        )
    elif latest:
        print(f"Update     : up to date (latest v{latest})")
    else:
        print("Update     : check skipped/unavailable")
    stats = g.get("stats", {})
    taken = stats.get("quizzes_taken", 0)
    if taken:
        answered = stats.get("quizzes_answered", 0)
        deferred = stats.get("quizzes_deferred", 0)
        print(f"Quizzes    : {taken} fired · {answered} answered · {deferred} deferred")
    else:
        print("Quizzes    : none yet this install")
    acc = _journal_accuracy()
    if acc:
        print(
            f"Accuracy   : {acc['pct']}%   ({acc['correct']} ✅ · {acc['partial']} 🟡 "
            f"· {acc['missed']} ❌ over {acc['total']} graded questions)"
        )
    sessions = {
        k: v for k, v in state.items() if k != GLOBAL_KEY and isinstance(v, dict)
    }
    if sessions:
        now = time.time()
        print(f"\nActive sessions ({len(sessions)}):")
        for sid, s in sorted(sessions.items(), key=lambda x: -x[1].get("ts", 0)):
            age_m = int((now - s.get("ts", now)) / 60)
            seen = s.get("tools_seen", {})
            tools_hint = (
                f"  tools=[{', '.join(sorted(seen.keys())[:4])}]" if seen else ""
            )
            print(
                f"  {sid[:20]}  count={s.get('count', 0)}/{s.get('threshold', '?')}  "
                f"pending={s.get('pending', False)}{tools_hint}  ({age_m}m ago)"
            )
    else:
        print("\nNo active sessions.")


# --- main --------------------------------------------------------------------


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "prompt"

    if mode == "status":
        cmd_status()
        return
    if mode == "update":
        cmd_update()
        return

    payload = {}
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        pass
    sid = str(payload.get("session_id") or "default")
    msg = payload.get("prompt") or ""

    # A harness-injected event (background/terminal task completion, system
    # notification) is NOT the user typing: passing it through keeps a pending
    # quiz pending instead of silently consuming it as a non-answer, and keeps it
    # from counting toward the cadence. Tool mode is real activity, so only guard
    # prompt mode.
    if mode == "prompt" and _is_synthetic_prompt(msg):
        sys.exit(0)

    # Daily update check — do the network fetch OUTSIDE the lock so we never
    # hold the flock during I/O (prompt mode only; tool calls stay fast/silent).
    now = time.time()
    upd_latest = upd_did_fetch = None
    if mode == "prompt":
        upd_latest, upd_did_fetch = _update_precheck(now)

    with _exclusive_lock():
        state = load()
        state = {
            k: v
            for k, v in state.items()
            if k == GLOBAL_KEY
            or (isinstance(v, dict) and now - v.get("ts", now) < PRUNE_AFTER)
        }

        g = state.setdefault(GLOBAL_KEY, {"defers": 0, "locked": False, "stats": {}})
        g.setdefault("stats", {})
        g["ts"] = now
        sess = state.setdefault(sid, {"count": 0, "threshold": 0})
        sess["ts"] = now
        locked = bool(DEFER_LIMIT) and bool(g.get("locked"))

        # Record the (already-fetched) update result and arm the nudge for any
        # message _inject sends this turn.
        if mode == "prompt":
            global _NUDGE
            _NUDGE = _apply_update_check(g, upd_latest, upd_did_fetch, now)

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
                _deny(
                    "Learning-check lock: defer limit reached. Answer the pending "
                    'quiz (one line, e.g. "1C 2A 3D 4B 5A") to unlock.'
                )
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
                secs = int(now - sess["issued"]) if sess.get("issued") else None
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
                secs = int(now - sess["issued"]) if sess.get("issued") else None
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
            # No quiz this turn — but still surface a one-time update nudge.
            if _NUDGE:
                _inject("")
        sys.exit(0)


if __name__ == "__main__":
    main()
