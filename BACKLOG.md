# Backlog

The next work on this project: features to add and bugs to fix. Each item
records where the change goes and what counts as done, so it can be started
straight away without working the problem out from scratch again.

Priority is **impact if left undone**, not effort. Items move to GitHub Issues
only when they need outside discussion; this file is the working queue.

Status: `open` · `in progress` · `blocked` · `done` (drop done items once the
release that carried them ships).

---

## P1 — correctness and trust

### B-1 · Verify the quiz was actually presented before charging a defer

**Status:** open · **Size:** M · **Touches:** `hooks/pop_quiz.py` (`main`, `classify`)

Today `UserPromptSubmit` sets `pending = True` and assumes the model obeyed the
directive. If the model ignores it and the user's next message is ordinary work,
`classify()` reads that as a defer and the user is charged for the _model's_
failure. With `POP_QUIZ_DEFER_LIMIT` set, enough of those freeze every chat.

`transcript_path` is in every hook payload and is still completely unused. Read
the last assistant turn from that JSONL and only mark the quiz pending if it
actually contains the questions.

**Done when:** a defer is only counted when the previous assistant turn
contained a presented quiz; a test drives the "model ignored the directive" path
and asserts no defer is charged; a malformed or missing transcript falls back to
current behaviour rather than leaving a quiz pending forever.

**Risk:** the failure mode is a quiz that never resolves, which is worse than the
bug. Guard the fallback first, then the detection.

### B-2 · Consolidate incident-shaped topic titles

**Status:** open · **Size:** M · **Touches:** `JOURNAL.md` (data), `hooks/pop_quiz.py` (`_topic_key`)

168 of 176 tracked topics were journaled before the controlled vocabulary
existed, and most name an incident rather than a concept: "Cause of the JSON
parse error", "Why osascript hung over SSH but Swift/CGWindowList didn't". Those
can never legitimately recur, so they sit at box 0 or 1 permanently and
`mastered` stays at 0 regardless of how well the vocabulary works from now on.

Nothing is broken by this (coverage is 100%, accuracy is correct), so it is not
urgent. But the scheduler's whole value depends on topics recurring.

**Done when:** a one-off script proposes concept-name merges for near-duplicate
and incident-shaped titles, the user approves the mapping, the journal is
rewritten in place with a backup, and `doctor` shows a materially higher
repeated-topic count. Keep the mapping file so the rename is auditable.

**Note:** verified working on 2026-10-26. "Cause of the JSON parse error" was
re-asked under its exact title and promoted box 0 → 1, so the mechanism is
sound; this item is only about the historical backlog of bad names.

---

## P2 — the self-check should catch more

### B-3 · `doctor --json` and a scheduled run

**Status:** open · **Size:** S · **Touches:** `hooks/pop_quiz.py` (`cmd_doctor`)

`doctor` exits non-zero and prints for humans, which is why CI can gate on it.
Nothing consumes it on a schedule, so a coverage regression on a live install
still waits for someone to run it by hand.

**Done when:** `doctor --json` emits the same findings as a machine-readable
object; a cron or `SessionStart` entry runs it and surfaces only problems.

### B-4 · Resolve the journal summary-table instruction

**Status:** open · **Size:** S · **Touches:** `hooks/pop_quiz.py` (`grade_directive`)

`grade_directive` tells the model to create the journal "with a short header + a
summary table if missing", and `JOURNAL_MAX` trimming tells it to "update the
summary table at the top to match". No journal has ever contained one and
nothing parses one. So the instruction is either dead weight in every grading
prompt or a feature that was never finished.

**Done when:** either the table is specified precisely and parsed (so it can be
verified), or both mentions are removed. Decide, do not leave it ambiguous.

### B-5 · Test the lock-free degraded path

**Status:** open · **Size:** S · **Touches:** `tests/test_pop_quiz.py`

`_exclusive_lock()` falls back to a no-op context manager when `fcntl` is
missing, which is the real path on native Windows. CI runs on Windows, but no
test forces that branch, so the degraded mode is only covered incidentally.

**Done when:** a test patches `fcntl` to `None` and asserts the read-modify-write
cycle still completes and `os.replace` still produces a valid state file.

---

## P3 — reach and polish

### B-6 · `SessionStart` nudge for due topics

**Status:** open · **Size:** S · **Touches:** `hooks/pop_quiz.py`, `install.sh`

`review` is a good offline drill that nobody remembers to run. A `SessionStart`
hook printing "N topics due for review" would surface it for free.

**Done when:** a third hook event is optional, off by default, and installable
from `install.sh`; it prints one line and never blocks the session.

**Depends on:** fits naturally with B-7's refactor, so consider doing it there.

### B-7 · v0.6.0 cross-platform epic

**Status:** open · **Size:** L · **Touches:** new `core/` + adapters

Take the quiz beyond Claude Code: an MCP server for Cursor, Copilot agent,
Antigravity and Claude Desktop (pull-based, no hard freeze), per-platform rules
generators (`.cursor/rules`, `.github/copilot-instructions.md`, `AGENTS.md`), and
Cursor native hooks. Needs a core/adapter split separating the platform-agnostic
quiz logic from the Claude Code hook.

**Verify first:** each platform's current extension API before writing an
adapter. Honest constraint: the mandatory cadence and the tool freeze only work
on hosts with a real hook system, so others get honour-system fidelity.

### B-8 · CI `push` trigger misses non-`feature/**` branches

**Status:** open · **Size:** XS · **Touches:** `.github/workflows/ci.yml`

`on.push.branches` is `[main, "feature/**"]`, so a `fix/**` branch gets CI only
from the `pull_request` event. That worked for #12, but the inconsistency will
bite a branch that is pushed without a PR.

**Done when:** the push trigger covers the branch prefixes actually in use, or
the project standardises on one prefix and the README says which.

### B-9 · Tag and release v0.1.0

**Status:** open · **Size:** XS · **Touches:** repo metadata

Releases exist for v0.2.0 through v0.5.3. The changelog documents a `0.1.0`
initial release that has no tag, so the release history has one gap at the
bottom.

**Done when:** the initial-release commit is identified and tagged, or the
changelog notes that 0.1.0 predates tagging and the gap is deliberate.
