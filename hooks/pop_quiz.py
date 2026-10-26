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
  - "status" (CLI only): pretty-print current state, counters, stats, accuracy,
    and per-topic mastery.
  - "review" (CLI only): offline flashcard drill of the topics the spaced-
    repetition system says are due — pure read of the journal, no model/network.
  - "doctor" (CLI only): self-check. Reports journal parse COVERAGE (records
    found vs parsed), unrecognised headers, topic-vocabulary health, duplicate
    hook wiring, and the last swallowed error. Exits non-zero if anything is
    wrong, so it can be run from CI or a cron. Run this first when the hook
    seems to have stopped working.
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
  POP_QUIZ_AUTO_UPDATE       set to any value to AUTO-APPLY a newer version on
                             the daily check (else just nudge)    (default off)
  POP_QUIZ_CHANNEL           "release" tracks the latest TAGGED release (safe:
                             an un-released commit on main can never auto-apply
                             itself); "branch" tracks POP_QUIZ_BRANCH's tip
                             (bleeding edge)                  (default release)

Updates: once a day (on UserPromptSubmit) the hook quietly checks GitHub for a
newer __version__. By default it appends a one-line upgrade nudge; with
POP_QUIZ_AUTO_UPDATE set it instead self-applies (download, back up to .bak,
atomic in-place replace) and announces it. Either way the check is throttled,
time-boxed, and offline-safe, and updates touch ONLY the script file, so the
defer counter, lock, stats, and journal survive untouched. Run "update" to
self-upgrade manually at any time.

Portable: state AND the default journal live under <claude-dir>/state/, so
copying ~/.claude/ to another machine just works — no absolute paths baked in.
Defers are tracked GLOBALLY (across chats), so a new chat can't dodge the limit.

Per-project topics: place a .pop-quiz-topics file in the project root (one topic
per line, lines starting with # are comments). The hook walks up from cwd to ~
and injects any found topics into the quiz directive for targeted questions.

Adaptive + spaced repetition: the grading journal is parsed back on each fire.
Each topic's verdict history folds into a Leitner box (✅ promotes, ❌ resets,
🟡 holds); the least-mastered topics are fed into the next quiz so weak areas
resurface, mastered ones retire. Recent accuracy also tunes question difficulty
(harder when strong, foundational when struggling), and a correct-answer streak
is surfaced to motivate. The same parse powers the accuracy + mastery lines in
"status" and the offline "review" drill.

Robustness: harness-injected prompts (background/terminal task completions,
[SYSTEM NOTIFICATION] wrappers) pass straight through so they can't be misread
as a quiz answer and silently eat a pending check. Runs without fcntl too
(degraded, lock-free) so native Windows no longer fails silently.

Fails LOUDLY, eventually: the wired hook command ends in "2>/dev/null || true"
so a crash can never break the user's turn, which also means a crash is
invisible. Every unexpected exception is therefore appended to
<claude-dir>/state/pop_quiz_errors.log and surfaced by "status" and "doctor".
If the quiz ever seems to have stopped firing, run "doctor" first.

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

__version__ = "0.5.2"

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # e.g. ~/.claude
STATE_DIR = os.path.join(BASE, "state")
STATE_FILE = os.path.join(STATE_DIR, "pop_quiz_state.json")
LOCK_FILE = STATE_FILE + ".lock"
# Where swallowed tracebacks go. The wired command discards stderr, so without
# this a crash is completely silent (see the module docstring).
ERROR_LOG = os.path.join(STATE_DIR, "pop_quiz_errors.log")
ERROR_LOG_MAX = 64 * 1024  # keep only the newest 64 KiB of tracebacks
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

# A message that is NOTHING BUT an acknowledgement is not an attempt. The same
# word followed by real content ("Yes, the lock is advisory only") IS an answer,
# which is why this matches the WHOLE message rather than just its first word.
_BARE_ACK = {
    "yes",
    "yeah",
    "yep",
    "yup",
    "no",
    "nope",
    "nah",
    "ok",
    "okay",
    "k",
    "sure",
    "alright",
    "right",
    "great",
    "nice",
    "cool",
    "thanks",
    "thank you",
    "ty",
    "got it",
    "understood",
    "fine",
    "hmm",
    "hm",
    "idk",
    "dunno",
    "no idea",
}

# Messages that OPEN like a new work instruction rather than an answer.
#
# Deliberately NARROW, and it is worth knowing why. The costs here are wildly
# asymmetric: a false DEFER increments the global counter and can freeze every
# chat as a punishment for answering correctly, while a false ANSWER merely
# spends one graded reply. So any opener that is also a natural way to begin an
# answer is NOT listed. That rules out "so", "this", "that's", "these", "it",
# "yes/no", "also", "and", "but", "actually", "sounds", "seems", and the bare
# modals ("is", "are", "do", "can", "will"): every one of those used to classify
# a real essay answer as a skip. It also rules out bare tool and subject names
# ("git", "docker", "npm", "ls") since "Docker gives each container its own
# namespace" is an answer, not an instruction.
_ACTION_START = re.compile(
    r"^(run|re-?run|exec|execute|let'?s|let me|can you|could you|would you|"
    r"will you|please|go ahead|carry on|keep going|continue|resume|proceed|"
    r"next up|moving on|add|create|update|fix|make|change|modify|refactor|"
    r"rename|write|build|compile|deploy|install|uninstall|remove|delete|drop|"
    r"commit|push|merge|rebase|revert|checkout|check|verify|test|lint|format|"
    r"show me|tell me|give me|explain|walk me|i want|i need|i'?d like|"
    r"skip (?:it|this|that)|hold on|wait|stop|cancel|abort|nevermind|"
    r"never mind|forget it)\b",
    re.IGNORECASE,
)

# Messages that OPEN like a genuine follow-up question. Kept separate from
# _ACTION_START so the two can be tuned independently: this one exists for the
# v0.5.1 regression where "who is the founder of git" (no question mark) was
# graded as a quiz answer. Limited to the wh-words, because every broader modal
# is also an answer opener.
_QUESTION_START = re.compile(
    r"^(what|what'?s|how|how'?s|why|when|where|who|whom|whose|which|"
    r"is there|are there|any (?:idea|chance|thoughts))\b",
    re.IGNORECASE,
)


def looks_like_mcq_answer(msg):
    # Require a majority of question-number/letter pairs so ordinary prose
    # never trips it. Use max(1, ...) so a single-question quiz (NUM_Q=1)
    # can still be satisfied — the original max(2, ...) made it impossible.
    need = max(1, NUM_Q // 2 + 1)
    return len(_MCQ_PAIR.findall(msg or "")) >= need


def _bare_phrase(msg):
    """The whole message reduced to a bare lowercase phrase, or "" if it has
    real content. Used to spot one-word acknowledgements and defer words."""
    t = (msg or "").strip().lower().strip(".!,;:…").strip()
    return t if len(t) <= 24 else ""


def is_explicit_defer(msg):
    return _bare_phrase(msg) in _DEFER_WORDS


def is_bare_ack(msg):
    return _bare_phrase(msg) in _BARE_ACK


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
    ANSWER is deliberately low: anything that is not an explicit defer, a bare
    acknowledgement, a question, or a clear new-work instruction is treated as
    an attempt. Two regressions are pinned down here, both of which silently
    consumed a quiz or charged an undeserved defer:

      - a concise but correct reply ("it memoizes with functools.lru_cache")
        must not be punished for being short (the old length/period heuristic),
      - an essay answer that merely OPENS with a common word ("So the flock is
        advisory...", "This script uses os.replace...", "Yes, it is advisory
        only") must not be read as new work. See _ACTION_START on why that list
        is narrow.
    """
    t = (msg or "").strip()
    if not t:
        return "defer"
    if is_explicit_defer(msg) or is_bare_ack(msg):
        return "defer"
    if looks_like_mcq_answer(msg):
        return "answer"
    if t.endswith("?"):
        return "defer"  # a question is a question, not a quiz answer
    if _QUESTION_START.match(t):
        return "defer"  # an unpunctuated question is still a question
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
# This is the single source of truth for the score tally, the spaced-repetition
# boxes and the review drill, so it has to match what the MODEL actually writes,
# not only what the directive asks for.
#
# It is deliberately TOLERANT, because being strict here failed silently and
# expensively: the directive's template reads "<✅ correct | 🟡 partial | ❌
# missed> Q<n>", the model overwhelmingly renders that as "### ✅ correct Q1 ·
# topic" (emoji AND word), and the original pattern allowed no slot for that
# word. On a real 283 KB journal it matched 22 of 185 graded questions, so
# accuracy, adaptive difficulty, the Leitner boxes and the review drill were all
# reading 12% of the data with nothing to indicate a problem. Hence: an optional
# verdict word, looser separators, h2-h6, and `doctor` to report the coverage
# so any future drift shows up as a number instead of rotting quietly.
#
# Separators are [ \t] rather than \s so a greedy match can never swallow the
# newline and capture the following line as the topic title.
_SEP = r"[ \t!.,:;*_·–—-]*"
_Q_HEADER = re.compile(
    r"^#{2,6}[ \t*_]*"
    r"([✅❌\U0001F7E1⚠])️?"  # the verdict emoji
    r"[ \t]*(?:correct(?:ly)?|right|partial(?:ly)?(?:[ \t]+correct)?|"
    r"half(?:[ \t]+right)?|missed?|wrong|incorrect|unanswered|skipped|"
    r"no[ \t]+answer)?"  # the optional verdict WORD
    + _SEP
    + r"Q[ \t]*\d+\b"  # Q<n>
    + _SEP
    + r"(.*)$",  # the topic title
    re.MULTILINE | re.IGNORECASE,
)
# Dated section headers, e.g. "## 2026-01-15 · topic" (the 📅 is historical and
# optional). Entries are attributed to the nearest one above them, which is what
# gives the Leitner scheduler a clock.
_DATE_HEADER = re.compile(
    r"^#{2,3}[ \t*_]*(?:📅[ \t]*)?(\d{4})-(\d{2})-(\d{2})\b",
    re.MULTILINE,
)
_VERDICT_GOOD = "✅"  # ✅ correct
_VERDICT_PART = "\U0001f7e1"  # 🟡 partial
_VERDICT_MISS = "❌"  # ❌ missed


def _norm_verdict(raw):
    """Fold a matched verdict emoji onto one of the three canonical ones. The
    model occasionally invents a fourth (⚠️ unanswered); anything that is not a
    pass or a partial counts as missed."""
    ch = (raw or "").replace("️", "")[:1]
    if ch == _VERDICT_GOOD:
        return _VERDICT_GOOD
    if ch == _VERDICT_PART:
        return _VERDICT_PART
    return _VERDICT_MISS


def _topic_key(title):
    """Normalise a topic title into a stable spaced-repetition key.

    The box history is keyed on this, so near-identical titles MUST collapse or
    a topic can never be promoted. A real journal logged the same question as
    "`localhost` inside a reverse-proxy container" and "localhost inside a
    reverse-proxy container"; .strip("`") only trimmed the ends, the keys
    differed, and a correct second answer opened a brand-new box instead of
    promoting the old one.
    """
    t = re.sub(r"[`*_\"'()\[\]]", "", (title or "").lower())
    return " ".join(re.sub(r"[^a-z0-9]+", " ", t).split())


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
        counts[_norm_verdict(m.group(1))] += 1
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


_ANSWER_LINE = re.compile(r"\*\*Answer:\*\*\s*(.+)")

# Leitner box thresholds: <= DUE means resurface soon; >= MASTERED means retire.
SRS_DUE = 1
SRS_MASTERED = 3
SRS_BOX_MAX = 4
# Days before a topic in each box falls due again. Without this a box is a
# one-way ratchet: answer something right twice and it retires permanently, so
# "mastered" quietly means "never asked again" rather than "known". Index is the
# box number; anything at or below SRS_DUE is due immediately.
SRS_INTERVALS = (0, 0, 7, 21, 60)


def _today_ord():
    import datetime

    return datetime.date.today().toordinal()


def _date_ord(y, m, d):
    import datetime

    try:
        return datetime.date(int(y), int(m), int(d)).toordinal()
    except ValueError:
        return None


def _journal_entries(text=None):
    """Parse the journal into per-question dicts, NEWEST FIRST. Each is
    {verdict, title, key, date, question, answer}. The block between two ###
    headers holds the quoted question and the **Answer:** bullet, and `date` is
    the dated section the entry sits under (an ordinal, or None)."""
    text = _read_journal() if text is None else text
    # Section dates in document order, so each question can binary-search the
    # nearest header above it.
    sections = []
    for d in _DATE_HEADER.finditer(text):
        ordinal = _date_ord(d.group(1), d.group(2), d.group(3))
        if ordinal:
            sections.append((d.start(), ordinal))
    starts = [s[0] for s in sections]

    def _date_for(pos):
        import bisect

        i = bisect.bisect_right(starts, pos) - 1
        return sections[i][1] if i >= 0 else None

    matches = list(_Q_HEADER.finditer(text))
    entries = []
    for i, m in enumerate(matches):
        block = text[
            m.end() : (matches[i + 1].start() if i + 1 < len(matches) else len(text))
        ]
        question = " ".join(
            ln.lstrip(">").strip()
            for ln in block.splitlines()
            if ln.lstrip().startswith(">")
        ).strip()
        am = _ANSWER_LINE.search(block)
        title = m.group(2).strip().strip("`").strip()
        # An odd number of backticks means one was orphaned by the strip above
        # ("`localhost` inside ..." -> "localhost` inside ..."); drop them all so
        # the displayed title and the vocabulary stay clean.
        if title.count("`") % 2:
            title = title.replace("`", "").strip()
        entries.append(
            {
                "verdict": _norm_verdict(m.group(1)),
                "title": title,
                "key": _topic_key(title),
                "date": _date_for(m.start()),
                "question": question,
                "answer": am.group(1).strip() if am else "",
            }
        )
    return entries


def _topic_boxes(entries=None):
    """Fold each topic's verdict history (oldest -> newest) into a Leitner box:
    ✅ promotes (+1, capped), ❌ resets to 0, 🟡 holds. Keyed by _topic_key so
    near-identical titles share one box.
    Returns {key: {title, box, seen, last_verdict, last_date}}."""
    entries = _journal_entries() if entries is None else entries
    boxes = {}
    for e in reversed(entries):  # oldest first so promotions accumulate
        title, key = e["title"], e.get("key") or _topic_key(e["title"])
        if not key:
            continue
        b = boxes.setdefault(
            key,
            {
                "title": title,
                "box": 1,
                "seen": 0,
                "last_verdict": "",
                "last_date": None,
            },
        )
        b["title"], b["seen"], b["last_verdict"] = title, b["seen"] + 1, e["verdict"]
        if e.get("date"):
            b["last_date"] = e["date"]
        if e["verdict"] == _VERDICT_GOOD:
            b["box"] = min(SRS_BOX_MAX, b["box"] + 1)
        elif e["verdict"] == _VERDICT_MISS:
            b["box"] = 0
    return boxes


def _days_overdue(b, today=None):
    """How far past its review interval a topic is. Boxes at or below SRS_DUE
    are always due. A topic with no parseable date is treated as due, since the
    safe failure is asking again rather than retiring something unlearned."""
    if b["box"] <= SRS_DUE:
        return 10**6  # always due, and sorts ahead of merely-overdue topics
    interval = SRS_INTERVALS[min(b["box"], len(SRS_INTERVALS) - 1)]
    if not b.get("last_date"):
        return 0
    return ((today or _today_ord()) - b["last_date"]) - interval


def _srs_due_topics(limit=5, boxes=None, today=None):
    """Topics needing review: least-mastered first (box <= SRS_DUE), then the
    ones whose box interval has elapsed, most-seen breaking ties."""
    boxes = _topic_boxes() if boxes is None else boxes
    today = today or _today_ord()
    scored = [(b, _days_overdue(b, today)) for b in boxes.values()]
    due = sorted(
        ((b, od) for b, od in scored if od >= 0),
        key=lambda p: (p[0]["box"], -p[1], -p[0]["seen"]),
    )
    return [b["title"] for b, _ in due[:limit]]


def _mastered_topics(boxes=None, today=None):
    """Topics in a high box that are NOT yet due for a refresher."""
    boxes = _topic_boxes() if boxes is None else boxes
    today = today or _today_ord()
    return [
        b["title"]
        for b in boxes.values()
        if b["box"] >= SRS_MASTERED and _days_overdue(b, today) < 0
    ]


def _topic_vocabulary(limit=30, boxes=None, today=None):
    """The existing topic titles worth offering the grader as a controlled
    vocabulary, most reusable first.

    Without this the SRS cannot function at all. quiz_directive asks the model
    to reuse a topic title, but grade_directive is what actually WRITES the
    headers, and it used to be handed no list of existing titles. So the grader
    coined a fresh name for every question: a real journal held 173 topics
    across 185 questions, only 4 of them seen more than once, which means no box
    could ever be promoted and "mastered" was permanently 0.

    Ordering matters as much as inclusion. Ranking purely by review urgency puts
    every once-missed one-off at the top, and real journals are full of titles
    like "Cause of the JSON parse error" that name an incident rather than a
    concept and can never legitimately recur. Those crowd out the reusable
    titles and, worse, model the wrong naming style. So this leads with the
    topics the current round was told to ask about, then the ones that have
    demonstrably recurred, then the rest by urgency.
    """
    boxes = _topic_boxes() if boxes is None else boxes
    today = today or _today_ord()
    asked_now = _srs_due_topics(limit=8, boxes=boxes, today=today)
    repeated = sorted(
        (b for b in boxes.values() if b["seen"] > 1),
        key=lambda b: (-b["seen"], b["box"]),
    )
    rest = sorted(
        boxes.values(), key=lambda b: (-_days_overdue(b, today), b["box"], -b["seen"])
    )

    out, seen = [], set()
    for title in asked_now + [b["title"] for b in repeated + rest]:
        key = _topic_key(title)
        if title and key not in seen:
            seen.add(key)
            out.append(title)
        if len(out) >= limit:
            break
    return out


def _current_streak(entries=None):
    """Consecutive most-recent ✅ questions (newest first)."""
    entries = _journal_entries() if entries is None else entries
    n = 0
    for e in entries:
        if e["verdict"] == _VERDICT_GOOD:
            n += 1
        else:
            break
    return n


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

    # Adaptive + spaced-repetition signals, all derived from the graded journal.
    entries = _journal_entries()
    boxes = _topic_boxes(entries)
    acc = _journal_accuracy()
    due = _srs_due_topics(boxes=boxes)
    mastered = _mastered_topics(boxes)
    streak = _current_streak(entries)

    diff_hint = ""
    if acc:
        if acc["pct"] < 60:
            diff_hint = (
                f"The user's recent accuracy is {acc['pct']}% (struggling): favour "
                "FOUNDATIONAL questions that rebuild core understanding, kept clear "
                "and not tricky. "
            )
        elif acc["pct"] > 85:
            diff_hint = (
                f"The user's recent accuracy is {acc['pct']}% (strong): make the "
                "questions HARDER — edge cases, trade-offs, 'why not the "
                "alternative', and internals. "
            )
        else:
            diff_hint = f"The user's recent accuracy is {acc['pct']}%. "

    srs_hint = ""
    if due:
        srs_hint = (
            f"Spaced repetition (Leitner): the user is weakest on {', '.join(due)} "
            "— work AT LEAST ONE of these in, rephrasing the question but reusing "
            "that topic title EXACTLY, character for character, when you later "
            "journal it: the title is the key the user's history is stored under, "
            "so rewording it resets their progress on that topic. "
        )
    if mastered:
        srs_hint += (
            f"They have largely mastered {', '.join(mastered[:6])}; only revisit "
            "those occasionally. "
        )

    motiv = ""
    bits = []
    if streak >= 3:
        bits.append(f"a {streak}-question correct streak")
    if acc and acc["total"] >= 3:
        bits.append(f"{acc['pct']}% lifetime accuracy")
    if bits:
        motiv = (
            "Open by briefly noting their progress (" + ", ".join(bits) + ") to "
            "motivate, then begin. "
        )

    return (
        f"MANDATORY {NUM_Q}-QUESTION LEARNING CHECK (auto-fired after {count} "
        "actions — your messages plus Claude's tool/file/agent calls — in this "
        "chat). Before doing anything else this turn, PAUSE normal work and quiz "
        f"the user: ask EXACTLY {NUM_Q} focused questions on the TECHNICAL "
        "SUBSTANCE of THIS chat — (a) jargon/terminology used, (b) the scripts or "
        "code written/changed and WHY they work that way, (c) the project files "
        "touched and what each does. Pick the meatiest concepts, NOT conversational "
        f"trivia. {motiv}{tools_hint}{topic_hint}{srs_hint}{diff_hint}"
        + _format_clause()
        + pressure
        + "Do NOT grade yet — just present the questions and wait for their reply. "
        "Tell the user this check fired automatically and is a mandatory part of "
        "their learning loop."
    )


def grade_directive(unlocked, seconds, vocab=None):
    took = f"They answered in about {seconds}s. " if seconds is not None else ""
    unlock_note = "Tool use is now UNLOCKED. " if unlocked else ""
    trim_note = ""
    if JOURNAL_MAX:
        trim_note = (
            f"After appending, trim the journal so it contains at most "
            f"{JOURNAL_MAX} dated sections (the '## <date> · <topic>' headers), "
            "removing the oldest ones from the bottom. Update the summary table "
            "at the top to match. "
        )
    # The controlled vocabulary. Topic titles are the PRIMARY KEY of the spaced-
    # repetition system, so a title that drifts by one word opens a new box and
    # loses the history. See _topic_vocabulary.
    vocab = _topic_vocabulary() if vocab is None else vocab
    vocab_note = ""
    if vocab:
        vocab_note = (
            "TOPIC TITLES ARE A CONTROLLED VOCABULARY. These titles are already "
            "tracked in the journal: "
            + "; ".join(vocab)
            + ". If a question covers one of them, reuse that title EXACTLY, "
            "character for character, even if you would phrase it differently "
            "— it is the key the spaced-repetition boxes are stored under, so a "
            "reworded title silently loses the user's whole history on that "
            "topic and resets their progress. Coin a new title ONLY for a "
            "genuinely new topic, and when you do, NAME THE CONCEPT, NOT THIS "
            "SESSION: a title has to be reusable the next time the same idea "
            "comes up in a different project. Good: 'Advisory file locks', "
            "'Atomic file replacement', 'Container network namespaces'. Bad: "
            "'Cause of the JSON parse error', 'Why osascript hung over SSH' — "
            "those name an incident that can never recur, so the user can never "
            "be shown to have learned them. "
        )
    return (
        f"The user just answered the pending learning check. {took}{unlock_note}"
        "Grade each question now: brief spoken feedback, correct any mistakes, "
        "give a verdict (correct / partial / missed) and 1-2 study links per "
        "question, and flag real gaps. For any 🟡 PARTIAL or ❌ MISSED question, "
        "first offer ONE short hint phrased as a guiding question (a Socratic "
        "nudge, not the solution) before revealing the full answer — keep it to a "
        "single nudge, do not wait for a reply or stall the turn. THEN append the "
        "results to the learning "
        f"journal at {JOURNAL} (create it with a short header + a summary table if "
        "missing). Add a section dated today headed exactly "
        "'## <YYYY-MM-DD> · <one-line topic summary>', NEWEST SECTION FIRST, and "
        "render EACH question in this EXACT shape so the entry is self-contained "
        "when re-read months later:\n"
        "  ### <emoji> Q<n> · <short topic title>\n"
        "    where <emoji> is ✅ for correct, 🟡 for partial, or ❌ for missed, "
        "written as JUST the emoji with NO word after it, then a space, then "
        "Q<n>. Do not write '### ✅ correct Q1'; write '### ✅ Q1'. The journal "
        "is parsed back to drive spaced repetition, and the verdict word in that "
        "position is the single most common cause of an entry being skipped.\n"
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
        f"completeness beats brevity. {vocab_note}{trim_note}"
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


# --- error logging -----------------------------------------------------------


def _log_error(mode):
    """Append the current traceback to ERROR_LOG, trimming it to the newest
    ERROR_LOG_MAX bytes. Must never raise: it runs from the top-level except."""
    import traceback

    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        block = f"--- {stamp} v{__version__} mode={mode}\n{traceback.format_exc()}\n"
        old = ""
        if os.path.exists(ERROR_LOG) and os.path.getsize(ERROR_LOG) > ERROR_LOG_MAX:
            with open(ERROR_LOG, encoding="utf-8", errors="replace") as f:
                old = f.read()[-ERROR_LOG_MAX // 2 :]
            with open(ERROR_LOG, "w", encoding="utf-8") as f:
                f.write(old)
        with open(ERROR_LOG, "a", encoding="utf-8") as f:
            f.write(block)
    except Exception:
        pass


def _last_error():
    """(timestamp_line, traceback_first_line) of the newest logged error, or
    None. Surfaced by status and doctor so a swallowed crash is visible."""
    try:
        with open(ERROR_LOG, encoding="utf-8", errors="replace") as f:
            blocks = f.read().split("--- ")
    except Exception:
        return None
    for block in reversed([b for b in blocks if b.strip()]):
        lines = [ln for ln in block.splitlines() if ln.strip()]
        if lines:
            return lines[0], (lines[-1] if len(lines) > 1 else "")
    return None


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


def _repo():
    return os.environ.get("POP_QUIZ_REPO") or "jay739/claude-pop-quiz"


# Resolved once per process: the git ref updates are pulled from, plus a note
# for `status`/`doctor` saying which channel actually won.
_REF_CACHE = {}


def _resolve_ref():
    """Which ref to pull the hook from.

    Default is the latest TAGGED RELEASE, not a branch tip. With
    POP_QUIZ_AUTO_UPDATE set, tracking a branch means every commit merged to it
    self-applies to every machine within 24h, so a single bad merge silently
    disables the hook everywhere (the wired command discards stderr). A release
    tag makes publishing an update a deliberate act.

    Falls back to the branch when the repo has no releases yet, so an install
    never gets stranded with no update path; `status` and `doctor` report which
    channel was used so the fallback is never invisible.
    """
    if "ref" in _REF_CACHE:
        return _REF_CACHE["ref"]
    branch = os.environ.get("POP_QUIZ_BRANCH") or "main"
    channel = (os.environ.get("POP_QUIZ_CHANNEL") or "release").lower()
    ref, note = branch, f"branch {branch}"
    if channel != "branch":
        tag = _latest_release_tag()
        if tag:
            ref, note = tag, f"release {tag}"
        else:
            note = f"branch {branch} (no release found)"
    _REF_CACHE["ref"], _REF_CACHE["note"] = ref, note
    return ref


def _ref_note():
    _resolve_ref()
    return _REF_CACHE.get("note", "")


def _latest_release_tag(timeout=3):
    """The newest release tag via the GitHub API, or None on any failure."""
    import urllib.request

    url = f"https://api.github.com/repos/{_repo()}/releases/latest"
    try:
        req = urllib.request.Request(
            url, headers={"Accept": "application/vnd.github+json"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return (json.loads(r.read(65536)) or {}).get("tag_name") or None
    except Exception:
        return None


def _raw_url():
    return (
        f"https://raw.githubusercontent.com/{_repo()}/{_resolve_ref()}"
        "/hooks/pop_quiz.py"
    )


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


def _do_update(fetch_timeout=10):
    """Download the latest hook and replace THIS script in place, keeping its
    filename and backing the current one up to .bak. Returns
    (applied: bool, latest: str|None, message: str).

    Touches ONLY the script file — never the state file — so the defer counter,
    lock flag, stats, and journal all survive an update untouched. Shared by the
    manual `update` command and the opt-in auto-updater.
    """
    target = os.path.abspath(__file__)
    latest = _remote_version(timeout=min(5, fetch_timeout))
    if latest and _parse_version(latest) <= _parse_version(__version__):
        return False, latest, f"Already up to date (v{__version__})."
    data = _fetch_remote(timeout=fetch_timeout)
    if not data:
        return (
            False,
            latest,
            (
                "Update failed: could not reach GitHub "
                "(check your connection, or POP_QUIZ_REPO / POP_QUIZ_BRANCH)."
            ),
        )
    # Sanity-check the payload before trusting it over our running script: it
    # must look like a real, reasonably-sized pop_quiz.py, not an error page.
    markers = (b"__version__", b"def main", b"def quiz_directive", b"def classify")
    if len(data) < 4096 or any(m not in data for m in markers):
        return (
            False,
            latest,
            ("Update aborted: the downloaded file does not look like pop_quiz.py."),
        )
    # And it must actually COMPILE. The wired hook command swallows stderr, so a
    # syntactically broken update would disable the quiz on every machine with
    # no symptom other than quizzes quietly never firing again. Cheap insurance.
    try:
        compile(data, target, "exec")
    except SyntaxError as e:
        return (
            False,
            latest,
            f"Update aborted: the downloaded file does not compile ({e}).",
        )
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
        return False, latest, f"Update failed while writing {target}: {e}"
    return (
        True,
        latest,
        (
            f"Updated {os.path.basename(target)}: v{__version__} -> v{latest or '?'} "
            f"(backup: {backup})"
        ),
    )


def _apply_update_check(g, latest, did_fetch, now, auto_applied=None):
    """INSIDE the lock: record the check result into global state and return a
    one-line nudge. If `auto_applied` is set (the opt-in auto-updater already
    swapped the file OUTSIDE the lock), announce that instead of nagging."""
    if did_fetch:
        g["update_checked_ts"] = now
        if latest:
            g["update_latest"] = latest
    latest = latest or g.get("update_latest")
    if auto_applied:
        g["update_notified"] = auto_applied
        g["update_applied"] = auto_applied
        return (
            f"[pop-quiz] Auto-updated to v{auto_applied} (was v{__version__}). "
            "Tell the user in one line to restart Claude Code or run /hooks to "
            "load it; their defer count and journal are unchanged."
        )
    if latest and _parse_version(latest) > _parse_version(__version__):
        if g.get("update_notified") != latest:
            g["update_notified"] = latest
            script = os.path.abspath(__file__)
            hint = (
                "set POP_QUIZ_AUTO_UPDATE=1 to apply future updates automatically"
                if not os.environ.get("POP_QUIZ_AUTO_UPDATE")
                else "it will auto-apply on the next check"
            )
            return (
                f"[pop-quiz] A newer version is available: v{latest} "
                f"(installed v{__version__}). Tell the user, in one line, to "
                f"upgrade by running `python3 {script} update` ({hint})."
            )
    return ""


def cmd_update():
    applied, latest, msg = _do_update()
    print(msg)
    if applied:
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
    channel = (
        f" [{_ref_note()}]" if not os.environ.get("POP_QUIZ_NO_UPDATE_CHECK") else ""
    )
    if latest and _parse_version(latest) > _parse_version(__version__):
        print(
            f"Update     : v{latest} available{channel} — run "
            f"`python3 {os.path.abspath(__file__)} update`"
        )
    elif latest:
        print(f"Update     : up to date (latest v{latest}){channel}")
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
    boxes = _topic_boxes()
    if boxes:
        due = len(_srs_due_topics(limit=10**6, boxes=boxes))
        mastered = len(_mastered_topics(boxes))
        print(
            f"Mastery    : {len(boxes)} topics tracked · {due} due · "
            f"{mastered} mastered · streak {_current_streak()} ✅   "
            "(see `review`)"
        )
    err = _last_error()
    if err:
        print(f"Last error : {err[0]} — see {ERROR_LOG} (run `doctor`)")
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


# --- doctor command ----------------------------------------------------------

# Any heading carrying a verdict emoji. Deliberately looser than _Q_HEADER so
# doctor can report the DIFFERENCE between the two: headings that are obviously
# graded questions but that the strict parser rejects. That number is the one
# that would have caught this project's worst bug on day one.
_LOOSE_Q_HEADER = re.compile(
    r"^#{2,6}[^\n]*?[✅❌\U0001F7E1⚠][^\n]*$",
    re.MULTILINE,
)
# One graded question writes exactly one of each of these bullets, so counting
# them is a parser-independent ground truth for "how many records does this
# journal hold" — which is what makes a coverage percentage possible at all.
_ANSWER_BULLET = re.compile(r"^\s*[-*]\s*\*\*Answer:\*\*", re.MULTILINE)
_SAID_BULLET = re.compile(r"^\s*[-*]\s*\*\*You said:\*\*", re.MULTILINE)


def _wired_hooks():
    """Every pop-quiz hook command found in <claude-dir>/settings.json, as
    {event: [command, ...]}. Empty when there is no settings file to read."""
    found = {}
    for name in ("settings.json", "settings.local.json"):
        path = os.path.join(BASE, name)
        try:
            with open(path, encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            continue
        for event, groups in (cfg.get("hooks") or {}).items():
            for group in groups or []:
                for h in (group or {}).get("hooks", []):
                    cmd = (h or {}).get("command", "")
                    if (
                        "pop_quiz" in cmd
                        or "grill_reminder" in cmd
                        or "POP_QUIZ_" in cmd
                    ):
                        found.setdefault(event, []).append(cmd)
    return found


def cmd_doctor():
    """Self-check. Prints what is healthy, what is broken, and exits non-zero if
    anything needs attention, so it can run from CI or a cron.

    This exists because every failure in this hook is silent by construction:
    the wired command ends in "2>/dev/null || true", the journal is written by a
    cooperative model rather than by code, and the parse that drives spaced
    repetition can degrade to near-zero coverage without a single symptom. It
    did exactly that for 37 sessions. Every check below is a thing that failed
    quietly and should have been a number on a screen.
    """
    problems, warnings = [], []
    print("=== pop-quiz doctor ===")
    print(f"Version    : {__version__}")
    print(f"Script     : {os.path.abspath(__file__)}")

    # --- journal parse coverage ---
    text = _read_journal()
    print(f"\nJournal    : {JOURNAL}")
    if not text:
        warnings.append(
            "The journal is empty or unreadable, so there is no history to "
            "learn from yet. Normal on a fresh install."
        )
        print("  (empty or unreadable)")
    else:
        records = max(
            len(_ANSWER_BULLET.findall(text)), len(_SAID_BULLET.findall(text))
        )
        entries = _journal_entries(text)
        strict_spans = {m.start() for m in _Q_HEADER.finditer(text)}
        unparsed = [
            m.group(0).strip()
            for m in _LOOSE_Q_HEADER.finditer(text)
            if m.start() not in strict_spans
        ]
        dated = len(_DATE_HEADER.findall(text))
        size_kb = len(text) // 1024
        print(f"  size            : {size_kb} KiB · {dated} dated sections")
        print(f"  graded records  : {records} (counted from the entry bullets)")
        print(f"  parsed by hook  : {len(entries)}")
        if records:
            pct = round(100 * len(entries) / records)
            print(f"  COVERAGE        : {pct}%")
            if pct < 95:
                problems.append(
                    f"Only {pct}% of the journal parses ({len(entries)} of "
                    f"{records} records). Everything downstream — accuracy, "
                    "adaptive difficulty, the Leitner boxes, the review drill — "
                    "is reading that fraction of your history. The usual cause "
                    "is the grader drifting off the '### <emoji> Q<n> · <title>' "
                    "header shape."
                )
        if unparsed:
            print(f"  UNRECOGNISED    : {len(unparsed)} heading(s), e.g.")
            for h in unparsed[:3]:
                print(f"      {h[:88]}")
            problems.append(
                f"{len(unparsed)} heading(s) carry a verdict emoji but do not "
                "match the question-header shape, so those questions are "
                "invisible to the scheduler."
            )
        if entries and not any(e.get("date") for e in entries):
            warnings.append(
                "No entry could be dated (no '## YYYY-MM-DD' section headers), "
                "so review intervals fall back to 'always due' and no topic can "
                "rest."
            )

    # --- scheduler health ---
    boxes = _topic_boxes()
    if boxes:
        repeated = sum(1 for b in boxes.values() if b["seen"] > 1)
        acc = _journal_accuracy()
        print(f"\nScheduler  : {len(boxes)} topics tracked")
        print(
            f"  repeated        : {repeated} seen more than once "
            f"({round(100 * repeated / len(boxes))}%)"
        )
        print(
            f"  due            : {len(_srs_due_topics(limit=10**6, boxes=boxes))}"
            f" · mastered: {len(_mastered_topics(boxes))}"
            f" · streak: {_current_streak()} ✅"
        )
        if acc:
            print(f"  accuracy        : {acc['pct']}% over {acc['total']} questions")
        # Measured over RECENT entries, not the whole journal: a history written
        # before the controlled vocabulary existed will always show a low
        # lifetime repeat ratio, and a check that can never go green is a check
        # people learn to ignore. What matters is whether the grader is reusing
        # titles NOW, so count recent questions whose topic already existed.
        recent = _journal_entries()[:40]
        earlier = {e["key"] for e in _journal_entries()[len(recent) :]}
        reused = sum(1 for e in recent if e["key"] in earlier)
        if len(recent) >= 20 and earlier:
            print(f"  title reuse     : {reused} of the last {len(recent)} questions")
            if reused == 0:
                problems.append(
                    f"None of the last {len(recent)} questions reused an "
                    "existing topic title, so no Leitner box can ever be "
                    "promoted and nothing is ever mastered. The grader is "
                    "coining a new title per question instead of reusing the "
                    "controlled vocabulary it is given."
                )

    # --- wiring ---
    wired = _wired_hooks()
    print("\nWiring     :", f"{sum(len(v) for v in wired.values())} hook command(s)")
    mine = os.path.basename(os.path.abspath(__file__))
    for event in ("UserPromptSubmit", "PreToolUse"):
        cmds = wired.get(event, [])
        print(f"  {event:<18}: {len(cmds)}")
        if len(cmds) > 1:
            problems.append(
                f"{len(cmds)} pop-quiz hooks are wired to {event}. They all "
                "run, so the action counter increments once per hook and quizzes "
                "fire early. Remove the duplicates from settings.json."
            )
        for cmd in cmds:
            if mine not in cmd and "pop_quiz" not in cmd:
                warnings.append(
                    f"The {event} hook runs a differently-named script than this "
                    f"one ({mine}). That is supported, but make sure you are "
                    "running doctor against the script that is actually wired."
                )
    if not wired:
        if os.path.isdir(os.path.join(BASE, ".git")):
            print("  (running from a git checkout, not an install — nothing to wire)")
        else:
            warnings.append(
                "No pop-quiz hook found in settings.json, so nothing is firing. "
                "Run install.sh, then /hooks in Claude Code."
            )

    # --- update channel + swallowed errors ---
    print(f"\nUpdates    : channel {_ref_note() or 'unknown'}")
    err = _last_error()
    if err:
        print(f"Last error : {err[0]}\n             {err[1][:100]}")
        problems.append(
            f"A crash was logged ({err[0]}). The wired command discards stderr, "
            f"so this never surfaced. Full tracebacks: {ERROR_LOG}"
        )
    else:
        print("Last error : none logged")

    print()
    for p in problems:
        print(f"PROBLEM: {p}\n")
    for w in warnings:
        print(f"note: {w}\n")
    if problems:
        print(f"{len(problems)} problem(s) found.")
        return 1
    print("All checks passed." + (f" {len(warnings)} note(s)." if warnings else ""))
    return 0


# --- review command ----------------------------------------------------------


def cmd_review(n=None):
    """Offline flashcard drill of the topics the SRS says are due (box <= SRS_DUE).
    Pure read of the journal — no model, no network. `n` caps how many topics."""
    entries = _journal_entries()
    boxes = _topic_boxes(entries)
    due_keys = {_topic_key(t) for t in _srs_due_topics(limit=10**6, boxes=boxes)}
    drill, seen = [], set()
    for e in entries:  # newest first → most recent phrasing per topic
        key = e.get("key") or _topic_key(e["title"])
        if key in due_keys and key not in seen and e["question"]:
            seen.add(key)
            drill.append(e)
    if n:
        drill = drill[: int(n)]
    if not drill:
        print(
            "Nothing due for review — the journal is empty, or every tracked "
            "topic is already mastered. Keep taking quizzes to build it up."
        )
        return
    print(f"=== pop-quiz review · {len(drill)} topic(s) due ===")
    print("Cover the A: line, recall it, then check.\n")
    for i, e in enumerate(drill, 1):
        box = boxes.get(e.get("key") or _topic_key(e["title"]), {}).get("box", 0)
        print(f"{i}. {e['title']}  (box {box}, last verdict {e['verdict']})")
        print(f"   Q: {e['question']}")
        print(f"   A: {e['answer'] or '(no answer recorded in the journal)'}\n")


# --- main --------------------------------------------------------------------


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "prompt"

    if mode == "status":
        cmd_status()
        return
    if mode == "update":
        cmd_update()
        return
    if mode == "review":
        cmd_review(sys.argv[2] if len(sys.argv) > 2 else None)
        return
    if mode in ("doctor", "check", "selfcheck"):
        sys.exit(cmd_doctor())

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
    auto_applied = None
    if mode == "prompt":
        upd_latest, upd_did_fetch = _update_precheck(now)
        # Opt-in: if POP_QUIZ_AUTO_UPDATE is set and a newer version turned up on
        # this (throttled, once-a-day) check, apply it now — OUTSIDE the lock,
        # since it does network I/O and a file write. Writes only the script, so
        # the defer counter / journal are untouched. Any failure falls back to
        # the manual upgrade nudge below.
        if (
            upd_did_fetch
            and upd_latest
            and _parse_version(upd_latest) > _parse_version(__version__)
            and os.environ.get("POP_QUIZ_AUTO_UPDATE")
        ):
            applied, _lat, _msg = _do_update()
            if applied:
                auto_applied = upd_latest

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
            _NUDGE = _apply_update_check(
                g, upd_latest, upd_did_fetch, now, auto_applied=auto_applied
            )

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

        # 1) Locked: only an actual one-line MCQ answer ("1C 2A 3D ...") unlocks.
        # The freeze is the hard backstop, and the locked prompt explicitly asks
        # for letters — so we gate on the MCQ shape, NOT the lenient classify().
        # Otherwise any stray remark or question ("who founded git") would pop the
        # freeze without the user ever attempting the quiz.
        if locked:
            if looks_like_mcq_answer(msg):
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
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        # The wired hook command ends in "2>/dev/null || true", so an unhandled
        # exception here would vanish without a trace and the quiz would simply
        # stop firing. Record it where `status` and `doctor` can find it, then
        # exit cleanly so the user's turn is never broken by this hook.
        _log_error(sys.argv[1] if len(sys.argv) > 1 else "prompt")
        sys.exit(0)
