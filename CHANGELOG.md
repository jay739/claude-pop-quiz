# Changelog

All notable changes to **claude-pop-quiz** are documented here.
This project adheres to [Semantic Versioning](https://semver.org/).

## [0.5.1] — Freeze-bypass fix

- **The freeze is a real backstop again.** While locked, only an actual one-line
  MCQ answer (`1C 2A 3D 4B 5A`) unlocks — matching what the locked prompt asks
  for and what the README always documented. The v0.4.0 lenient classifier had
  let any non-action remark (even a question like "who is the founder of git")
  pop the freeze without the user attempting the quiz.
- **Questions are no longer graded as answers.** A message ending in `?`, or
  starting with an interrogative (`who/whom/whose/which/...`), classifies as a
  defer instead of being scored. Fixes genuine follow-up questions being eaten by
  a pending quiz.

## [0.5.0] — Adaptive quizzing, Leitner SRS, and opt-in auto-update

- **Leitner spaced repetition:** each topic's verdict history folds into a box
  (✅ promotes, ❌ resets to 0, 🟡 holds). The least-mastered topics are fed into
  the next quiz; mastered ones retire. Replaces the flat "recent weak topics".
- **Adaptive difficulty:** recent accuracy tunes the questions — foundational
  below 60%, harder above 85%.
- **Gamification:** a correct-answer streak and lifetime accuracy are surfaced in
  the quiz intro and in `status` to motivate.
- **Socratic follow-ups:** on a 🟡/❌ the grader offers one guiding hint before
  the answer instead of just handing it over.
- **`review` command:** `pop_quiz.py review [N]` prints an offline flashcard
  drill of the topics the SRS says are due. No model, no network, pure journal.
- **`status`** now shows a mastery line (topics tracked · due · mastered · streak).
- **Opt-in auto-update:** with `POP_QUIZ_AUTO_UPDATE=1` the daily check self-applies
  a newer version (download, back up to `.bak`, atomic in-place replace) and
  announces it; otherwise it keeps nudging. Updates touch only the script, so the
  defer counter, lock, stats, and journal survive — locked in by a regression test.
- New env var: `POP_QUIZ_AUTO_UPDATE`.

## [0.4.0] — Learning loop: spaced repetition, fairer quizzes, fewer false skips

- **Spaced repetition:** each quiz parses the journal and pulls topics you last
  scored partial/missed on, then works at least one back in, rephrased.
- **Real accuracy in `status`:** lifetime ✅/🟡/❌ tally and a score percent,
  read straight from the journal's graded verdicts.
- **Richer journal entries:** every question is now logged with a **Context**
  line (the file/function/command it came from) and the question, your answer,
  and the correct answer recorded in **full** — no more truncated entries.
- **Fairer MCQs:** options are constrained to equal length and specificity with
  the correct letter varied, so "always pick the longest option" no longer wins.
- **Bug — synthetic prompts no longer eat a pending quiz:** background/terminal
  task completions and `[SYSTEM NOTIFICATION]` events pass through instead of
  being misread as your answer and silently consuming the check.
- **Bug — concise correct answers count:** a short, right reply ("it memoizes
  with `lru_cache`") is graded, not scored as a skip toward the freeze limit.
- **Bug — installer honours every knob:** re-running with `POP_QUIZ_MIN`/`MAX`/
  `QUESTIONS`/`JOURNAL` now bakes them into the command (they were silently
  dropped, contradicting the docs).
- **Windows:** runs without `fcntl` in a degraded lock-free mode instead of
  failing silently.
- **Tests + CI:** a stdlib `unittest` suite and a GitHub Actions matrix
  (Ubuntu + Windows, Python 3.8/3.12).

## [0.3.0] — Self-update checker

- **Self-update:** the hook checks GitHub once a day (throttled, time-boxed,
  offline-safe) for a newer `__version__` and appends a one-line upgrade nudge —
  shown once per version, never on tool calls.
- **`update` subcommand:** `pop_quiz.py update` downloads the latest hook, backs
  the current one up to `.bak`, and overwrites in place (keeps renamed filenames).
- **`status`** now reports installed version, script path, and live update status.
- New env vars: `POP_QUIZ_REPO`, `POP_QUIZ_BRANCH`, `POP_QUIZ_NO_UPDATE_CHECK`.

## [0.2.0] — Fixes, tool context & tooling

- **Race-condition fix:** the load/modify/save cycle is now guarded by
  `fcntl.flock`, so parallel `PreToolUse` calls can't clobber the counter.
- **Smarter answer detection:** work instructions ("run…", "let's…", "can you…")
  are no longer misread as quiz answers and silently skipped.
- **MCQ single-question fix:** a `POP_QUIZ_QUESTIONS=1` quiz can now be answered.
- **Tool context:** `PreToolUse` records tool names + files touched, so quizzes
  ask about the actual files you changed.
- **Per-project topics:** a `.pop-quiz-topics` file targets questions per repo.
- **`status` subcommand**, **`POP_QUIZ_JOURNAL_MAX_ENTRIES`** journal cap, and
  **`uninstall.sh`**.

## [0.1.0] — Initial release

- Mandatory, automatic learning-check hook firing every ~40–45 actions per chat.
- Essay / MCQ / mixed formats, defer limit with a tool-use freeze, and a graded
  markdown learning journal.

[0.5.1]: https://github.com/jay739/claude-pop-quiz/releases/tag/v0.5.1
[0.5.0]: https://github.com/jay739/claude-pop-quiz/releases/tag/v0.5.0
[0.4.0]: https://github.com/jay739/claude-pop-quiz/releases/tag/v0.4.0
[0.3.0]: https://github.com/jay739/claude-pop-quiz/releases/tag/v0.3.0
[0.2.0]: https://github.com/jay739/claude-pop-quiz/releases/tag/v0.2.0
[0.1.0]: https://github.com/jay739/claude-pop-quiz/releases/tag/v0.1.0
