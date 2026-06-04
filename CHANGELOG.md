# Changelog

All notable changes to **claude-pop-quiz** are documented here.
This project adheres to [Semantic Versioning](https://semver.org/).

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

[0.3.0]: https://github.com/jay739/claude-pop-quiz/releases/tag/v0.3.0
[0.2.0]: https://github.com/jay739/claude-pop-quiz/releases/tag/v0.2.0
[0.1.0]: https://github.com/jay739/claude-pop-quiz/releases/tag/v0.1.0
