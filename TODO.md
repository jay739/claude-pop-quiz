# claude-pop-quiz — TODO / backlog

## Fixed

### Background/terminal result silently skips a pending quiz (v0.4.0)

Resolved: `_is_synthetic_prompt()` now detects harness-injected events
(`<task-notification>`, `[SYSTEM NOTIFICATION ...]`) and `main()` passes them
through in prompt mode, so a pending quiz stays pending and no defer is counted.
Covered by `tests/test_pop_quiz.py::EndToEndCycleTests`.

## Backlog (original report kept for reference)

### Background/terminal result silently skips a pending quiz (reported 2026-06-08)

When the model is waiting on a terminal/background task and the completion event
arrives **while a quiz is pending**, the `UserPromptSubmit` hook treats that
system `<task-notification>` (and the `[SYSTEM NOTIFICATION - NOT USER INPUT]`
wrapper) as if it were the user's answer. It then fires the "user just answered"
grading directive with no real answer text, so the quiz is consumed/skipped
silently. (It does NOT increment the defer counter — verified `defers_used` stayed
0 — but the questions are lost and never graded.)

**Fix idea:** in `hooks/pop_quiz.py`, before classifying a prompt as answer/defer,
detect synthetic/non-user prompts and PASS THROUGH (leave the quiz pending, don't
grade, don't count a defer). Markers to match:

- prompt body contains `<task-notification>` / `<task-id>` / `</task-notification>`
- prompt starts with `[SYSTEM NOTIFICATION - NOT USER INPUT]`
- (generally) UserPromptSubmit payloads that are harness-injected events rather
  than typed user text.

Then the quiz stays pending until the human actually replies, and a follow-up
real answer gets graded normally.
