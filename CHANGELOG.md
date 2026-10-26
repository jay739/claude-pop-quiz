# Changelog

All notable changes to **claude-pop-quiz** are documented here.
This project adheres to [Semantic Versioning](https://semver.org/).

## [0.5.2] — The learning loop actually learns

Every fix here addresses the same underlying flaw: this hook was built so that
all of its failures are silent. The wired command discards stderr, the journal
is written by a cooperative model rather than by code, and nothing ever checked
its own output. Three features had been dead or degraded for 37 journal
sessions with no symptom.

- **Journal parsing recovered 88% of the history it was discarding.** The
  grading directive's template reads `<✅ correct | 🟡 partial | ❌ missed>
Q<n>`, and the grader renders that as `### ✅ correct Q1 · topic`, emoji
  **and** word. The parser allowed no slot for that word, so on a real 283 KB
  journal it matched **22 of 185** graded questions. Lifetime accuracy,
  adaptive difficulty, the Leitner boxes and the `review` drill were all
  reading 12% of the data. Measured accuracy was 45% when the truth was 61%,
  which is the difference between the hook feeding you deliberately
  foundational questions and treating you as competent. The header pattern now
  tolerates the verdict word, `⚠️` as a fourth verdict, h2 to h6, bold
  headings and looser separators, and the directive pins the shape explicitly.
- **Spaced repetition can promote a topic at all now.** `quiz_directive` asked
  the model to reuse an existing topic title, but `grade_directive`, which
  actually writes the headers, was handed no list of existing titles. So the
  grader coined a fresh name per question: 173 topics across 185 questions,
  only 4 ever asked twice, no box ever promoted, `mastered` permanently 0.
  Grading now receives the tracked titles as a controlled vocabulary with an
  explicit instruction to reuse them character for character, and topic keys
  are normalised so near-identical titles share one box (a real journal had
  split one topic across two boxes over a single stray backtick).
- **Leitner boxes have a clock.** "Due" was `box <= 1` and nothing else, so a
  topic answered correctly twice retired forever. Entries are now attributed to
  their dated section and fall due again after a per-box interval (7 / 21 / 60
  days), so mastery decays instead of being permanent.
- **A correct answer can no longer freeze your tools.** The defer classifier
  treated a long list of opening words as "new work", including `this`, `so`,
  `yes`, `that's` and the bare modals, which are ordinary ways to begin an
  essay answer. With `POP_QUIZ_DEFER_LIMIT` set, answering correctly could
  freeze every chat. The work-instruction list is now deliberately narrow, with
  questions and bare acknowledgements handled separately, because a false defer
  costs a freeze while a false answer costs one graded reply.
- **New `doctor` command.** Reports journal parse coverage, unreadable
  headings, topic-reuse rate, duplicate hook wiring, the update channel and the
  last swallowed traceback. Exits non-zero so it can run from CI or a cron.
  This is the systemic fix: the bugs above were all invisible, and coverage
  would have been a number on a screen on day one.
- **Crashes are recorded instead of vanishing.** Any unhandled exception is
  appended to `state/pop_quiz_errors.log` and surfaced by `status` and
  `doctor`. The hook still exits 0 so it can never break your turn.
- **Updates track tagged releases, not a branch tip** (`POP_QUIZ_CHANNEL`,
  default `release`). With auto-update on, tracking a branch meant any merge
  self-applied to every machine within a day. The download is also
  **byte-compiled before it replaces the running script**, so a syntactically
  broken release cannot silently take the quiz down everywhere.
- **`JOURNAL_MAX_ENTRIES` works.** It told the model to trim `## 📅 ...`
  headers; real journals contain `## <date> · <topic>` and zero 📅, so the cap
  silently trimmed nothing.
- **The installer no longer double-wires a renamed install.** It matched the
  literal string `pop_quiz.py`, so an install renamed to e.g.
  `grill_reminder.py` (which the README blesses and `update` preserves) was
  invisible: re-running appended a _second_ hook, both ran, and the action
  counter double-counted. `install.sh` now finds an existing install under any
  name, upgrades it in place, and carries forward every knob already baked into
  the command. `uninstall.sh` had the same blindness and used to report success
  while leaving the hooks wired.
- **CI** gained a lint job (`black`, plus shell syntax checks) and a
  format-conformance step that runs `doctor` against `JOURNAL.example.md`, so
  the documented journal shape can never drift from the parser again. Actions
  bumped to `checkout@v7` / `setup-python@v7`, off Node 20.
- Tests: 23 to 57, with each new case verified to fail against 0.5.1.
- New env var: `POP_QUIZ_CHANNEL`.

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
