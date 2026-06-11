<div align="center">

# 🎓 claude-pop-quiz

**A mandatory, automatic learning-check hook for [Claude Code](https://github.com/anthropics/claude-code).**

_Stay fluent in the code your agent wrote — get pop-quizzed on your own sessions, on a cadence you can't skip._

[![version](https://img.shields.io/github/v/tag/jay739/claude-pop-quiz?label=version&sort=semver&color=brightgreen)](https://github.com/jay739/claude-pop-quiz/releases)
[![license](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![python](https://img.shields.io/badge/python-3.6%2B-blue.svg)](https://www.python.org/)
[![changelog](https://img.shields.io/badge/changelog-v0.5.1-orange.svg)](CHANGELOG.md)

[**Install**](#-install) · [**How it works**](#-how-it-works) · [**Configuration**](#-configuration) · [**Updates**](#-staying-up-to-date) · [**Changelog**](CHANGELOG.md)

</div>

---

Agentic coding lets you ship work without reading what the agent wrote. The risk: you end up unable to _explain_ code, decisions, and tooling that are nominally "yours" — in an interview, a review, or just when the agent isn't around. This hook counters that by **pop-quizzing you on your own sessions, on a cadence you can't skip.**

Every ~40–45 _actions_ in a chat (your messages **plus** every tool/file/agent call Claude makes), it pauses and makes you answer 5 questions about the technical substance of that session — the jargon used, the code written and _why_, and the files touched. You answer in your own words; Claude grades you and flags gaps.

After grading, Claude appends the results to a **learning journal** — a markdown revision log of every quiz: each question, your answer, the correct answer, a ✅/🟡/❌ verdict, a one-line **context** note (the file or concept the question came from, so the entry stands alone months later), and links to study. Re-read it later, or point it at a git repo to version your progress. See [`JOURNAL.example.md`](JOURNAL.example.md) for the format.

That journal is also read **back in** to make each round smarter:

- **Leitner spaced repetition.** Every topic's verdict history folds into a box — a ✅ promotes it, a ❌ resets it, a 🟡 holds. The next quiz pulls your least-mastered topics and retires the ones you've nailed, so weak areas keep coming back until they stick.
- **Adaptive difficulty.** Your recent accuracy tunes the questions: foundational when you're below 60%, harder (edge cases, trade-offs, internals) above 85%.
- **Streaks + accuracy.** A correct-answer streak and lifetime accuracy show up in the quiz intro and in `status` to keep you going.
- **Offline `review` drill.** `python3 ~/.claude/hooks/pop_quiz.py review [N]` prints flashcards for the topics that are due — pure read of the journal, no model and no network.

> [!NOTE]
> Your real journal holds your actual answers, so it's **personal and gitignored by
> default**. The repo ships [`JOURNAL.example.md`](JOURNAL.example.md) as a format
> sample only — your `JOURNAL.md` never gets committed.

## 🧠 How it works

It's two hooks pointing at one script, wired in your Claude Code `settings.json`:

| Event                    | Mode     | What it does                                                                                                                    |
| ------------------------ | -------- | ------------------------------------------------------------------------------------------------------------------------------- |
| `PreToolUse` (all tools) | `tool`   | Increments the per-chat counter **silently**, records the tool name and files touched for targeted questions — never interrupts |
| `UserPromptSubmit`       | `prompt` | Increments, and if the chat crossed its threshold, **injects the quiz** and resets                                              |

Design choices worth knowing:

- **Counts all activity, not just messages.** A heavily agentic chat with 3 typed messages but 80 tool calls still gets examined. (`UserPromptSubmit` alone would never fire there.)
- **Fires only at a message boundary.** Tool calls increment but never inject, so the quiz appears at the _start of your next message_ — never mid-task.
- **Per chat.** State is keyed by `session_id`, so concurrent chats each track their own count.
- **Randomized threshold** (default 40–45) so the cadence isn't gameable.
- **Self-contained.** State lives under `<claude-dir>/state/`; copy `~/.claude/` to another machine and it just works.

## 🚀 Install

```bash
git clone https://github.com/<you>/claude-pop-quiz.git
cd claude-pop-quiz
./install.sh
```

`install.sh` copies `hooks/pop_quiz.py` into `~/.claude/hooks/` and merges the hook
config into `~/.claude/settings.json` **without touching your existing settings**
(idempotent — safe to re-run). Then open `/hooks` in Claude Code once, or restart,
to load it. New chats pick it up automatically.

Prefer to wire it by hand? Copy the `hooks` block from
[`examples/settings.json`](examples/settings.json) into your settings.

> [!TIP]
> The hook loads automatically in **new** chats. To activate it in an already-open
> session, run `/hooks` once. Requires only **Python 3.6+** — no pip installs, no
> dependencies. macOS, Linux, and WSL use a real file lock; native Windows runs
> in a degraded lock-free mode (no `fcntl`) rather than failing silently.

### Uninstall

```bash
./uninstall.sh
```

Removes `~/.claude/hooks/pop_quiz.py` and strips the two hook entries from
`~/.claude/settings.json`. State and journal are preserved — delete them manually
if you want a clean slate:

```bash
rm ~/.claude/state/pop_quiz_state.json
rm ~/.claude/state/pop_quiz_state.json.lock
```

### 🔄 Staying up to date

The hook **checks itself for updates** — no need to remember to `git pull`.

> [!NOTE]
> This daily version check is the hook's **only** outbound network request — a
> single `GET` to GitHub for the latest script. It sends nothing about you or your
> code. Turn it off completely with `POP_QUIZ_NO_UPDATE_CHECK=1`.

- Once a day (on a normal `UserPromptSubmit`), it quietly asks GitHub whether a
  newer `__version__` exists. The check is throttled to once per 24h, time-boxed
  to ~2s, and **fully offline-safe** — any failure is swallowed so the hook never
  breaks or slows you down. Tool calls never hit the network.
- When a newer version is found, it appends a **one-line nudge** to its next
  message ("a newer version is available — run `… update`"). You're told **once**
  per new version, not nagged every turn.
- **Hands-off auto-update (opt-in):** set `POP_QUIZ_AUTO_UPDATE=1` and the daily
  check **self-applies** the new version instead of nudging — it downloads, backs
  up to `.bak`, atomically replaces the running script in place, and tells you
  once to reload. It's opt-in by default because auto-running code pulled from the
  network is a (small, it's your own repo) risk worth choosing deliberately.
- To upgrade in place manually any time:

  ```bash
  python3 ~/.claude/hooks/pop_quiz.py update
  ```

  This downloads the latest hook from GitHub, **backs up the current one to
  `.bak`**, and overwrites the running script _keeping its installed filename_.
  Restart Claude Code (or `/hooks`) to load it. **Updates touch only the script
  file**, so your state, journal, and defer count are guaranteed untouched (a
  regression test enforces this).

Check the version and update status any time:

```bash
python3 ~/.claude/hooks/pop_quiz.py status
```

The `update` command writes to **its own path**, so it works no matter what the
installed file is called — if you renamed it (e.g. `grill_reminder.py`), it
upgrades that file in place and keeps the name. _Existing installs predating the
update checker need one manual `update` (or re-run `install.sh`) to gain it;
after that they self-notify._

Forked the repo? Point the checker at your copy with `POP_QUIZ_REPO=you/your-fork`
(and `POP_QUIZ_BRANCH=…`). Want no network at all? Set `POP_QUIZ_NO_UPDATE_CHECK=1`.

### Scope

- Put the hook in **`~/.claude/settings.json`** (user scope) → fires in _every_ project.
- Put it in a project's **`.claude/settings.json`** → fires only in that project.

## 🔧 Configuration

**Everything is optional and tweakable** — the hook ships with sane defaults and
nothing is hard-coded into the script. Every knob is an environment variable:

| Variable                       | Default                                  | What it does                                                                                     |
| ------------------------------ | ---------------------------------------- | ------------------------------------------------------------------------------------------------ |
| `POP_QUIZ_MIN`                 | `40`                                     | Lower bound of the random action threshold (how _often_ it fires)                                |
| `POP_QUIZ_MAX`                 | `45`                                     | Upper bound of the random action threshold                                                       |
| `POP_QUIZ_QUESTIONS`           | `5`                                      | Number of questions per check                                                                    |
| `POP_QUIZ_FORMAT`              | `essay`                                  | `essay`, `mcq`, or `mixed` — see [Quiz format](#-quiz-format--cant-i-just-skip-it)               |
| `POP_QUIZ_DEFER_LIMIT`         | `0`                                      | Consecutive defers (across all chats) before tool use **freezes**; `0` = soft mode, never freeze |
| `POP_QUIZ_JOURNAL`             | `<claude-dir>/state/learning_journal.md` | Where the graded journal is written                                                              |
| `POP_QUIZ_JOURNAL_MAX_ENTRIES` | `0`                                      | Keep at most this many dated entries in the journal; `0` = unlimited                             |
| `POP_QUIZ_REPO`                | `jay739/claude-pop-quiz`                 | `owner/repo` the update checker pulls from (set if you forked)                                   |
| `POP_QUIZ_BRANCH`              | `main`                                   | Branch the update checker pulls from                                                             |
| `POP_QUIZ_NO_UPDATE_CHECK`     | _(unset)_                                | Set to any value to disable the daily online version check entirely                              |
| `POP_QUIZ_AUTO_UPDATE`         | _(unset)_                                | Set to any value to auto-apply a newer version on the daily check (else just nudge)              |

### How to set / change them

These values get **baked into the hook command** in your `~/.claude/settings.json`.
Two ways to change them — both safe to repeat:

1. **Re-run the installer with env vars** (it rewrites the command for you):

   ```bash
   POP_QUIZ_FORMAT=mcq POP_QUIZ_DEFER_LIMIT=3 ./install.sh
   ```

   Run with no env and it also **prompts** you for format, defer limit, and max journal entries.

2. **Edit the command directly** in `~/.claude/settings.json` — prepend the vars
   to _both_ the `UserPromptSubmit` and `PreToolUse` commands (the freeze needs
   `POP_QUIZ_DEFER_LIMIT` on both):
   ```json
   "command": "POP_QUIZ_MIN=90 POP_QUIZ_MAX=110 POP_QUIZ_QUESTIONS=3 POP_QUIZ_FORMAT=mcq POP_QUIZ_DEFER_LIMIT=3 POP_QUIZ_JOURNAL=$HOME/notes/learning_journal.md python3 ~/.claude/hooks/pop_quiz.py prompt 2>/dev/null || true"
   ```

Changes take effect on **new chats** (or after `/hooks`). To see the current state at any time without opening Claude Code:

```bash
python3 ~/.claude/hooks/pop_quiz.py status
```

This prints the active sessions, action counts, defer tally, lock state, and lifetime quiz stats.

Common tweaks:

- **Quiz less often:** raise `POP_QUIZ_MIN` / `POP_QUIZ_MAX` (e.g. `90`/`110`).
- **Go fast:** `POP_QUIZ_FORMAT=mcq` — one-line answers.
- **Force discipline:** `POP_QUIZ_DEFER_LIMIT=3` — freeze after 3 skips.
- **Back up your journal:** point `POP_QUIZ_JOURNAL` at a tracked path. By default
  it's under `state/` (gitignored — personal data).
- **Cap journal size:** `POP_QUIZ_JOURNAL_MAX_ENTRIES=100` — Claude trims the oldest
  entries after each grading pass.
- **Turn it off entirely:** run `./uninstall.sh`, or remove the hook block from
  `settings.json` manually.

## 🎯 Quiz format & "can't I just skip it?"

### Format: pick your friction

| `POP_QUIZ_FORMAT`   | What you get                                                                                                                                  |
| ------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| `essay` _(default)_ | Free-response answers in your own words. If you're short on time, Claude offers the **MCQ quick version** of the _same_ questions.            |
| `mcq`               | Every question is **multiple choice** (A–D, one correct). You answer in a single line of letters — e.g. `1C 2A 3D 4B 5A`. Seconds, no typing. |
| `mixed`             | Half free-response, half multiple choice.                                                                                                     |

Either way you're graded, corrected, and **journaled** — a 15-second MCQ round is
real revision, not a bypass.

> [!NOTE]
> MCQ options are constrained to be **ungameable**: all four roughly equal in
> length and specificity, the correct letter varied across questions, and
> distractors that encode real misconceptions. The old "just pick the longest
> option" tell no longer works.

### Defer & the hard stop

When a quiz fires you can **defer** — say "defer" / "skip quiz", or just keep
working. But defers are counted **globally, across every chat** (opening a fresh
chat doesn't reset them). Once you hit `POP_QUIZ_DEFER_LIMIT`, the `PreToolUse`
hook **denies every tool call** — Claude can talk but can't edit, run, or search
anything until you answer a quiz. Submitting an answer (the hook recognizes the
one-line `1C 2A 3D 4B 5A` shape) resets the counter and unlocks.

> [!WARNING]
> The freeze is **global** — once the defer limit is hit, _every_ chat is frozen,
> not just the one you deferred in. Answering any pending quiz unlocks them all.
> Set `POP_QUIZ_DEFER_LIMIT=0` (the default) to stay in soft mode and never freeze.

```json
"command": "POP_QUIZ_FORMAT=mcq POP_QUIZ_DEFER_LIMIT=3 python3 ~/.claude/hooks/pop_quiz.py prompt 2>/dev/null || true"
```

### Honest about the limits

This is a **discipline gate, not a vault.** Two things no hook can do:

1. **Judge understanding.** The hook checks an answer's _shape_, not its
   correctness — only the model grades that, and the model is cooperative, so it
   can be talked past. The hard stop forces you to _submit an attempt_, not to
   pass.
2. **Defend against its owner.** It's your `settings.json` and your script — one
   commented line disables it. It exists to stop _drift_, not a determined you.

The real lever is making the quiz cheap (MCQ = seconds) so answering beats
dodging. The freeze is the backstop for when you'd otherwise let it slide.

> **Live per-question timer?** Not possible in Claude Code — it's turn-based, and
> nothing runs during your think-time to enforce a countdown or auto-skip. The
> hook _can_ read the clock and log how long you took, and a non-answer counts as
> a defer, but there's no live 15-second timer.

## 📁 Per-project topic hints

Drop a `.pop-quiz-topics` file in any project root (one topic per line; `#` lines are
comments). When a quiz fires in that project, the hook reads the file and tells Claude
to draw questions from those topics instead of guessing from the conversation alone:

```
# .pop-quiz-topics — Rust async project
Rust ownership and borrowing
async/await and tokio task model
error handling with anyhow / thiserror
```

The hook walks up from `cwd` to `~` — so a single file at `~/work/.pop-quiz-topics`
covers every project under `~/work/` unless a closer file overrides it.

## 🔍 Targeted questions via tool context

The `PreToolUse` hook now records every tool name and the files it touches into the
session state. When a quiz fires, the directive includes a summary like:

> Tools used this session: Edit(auth.py, config.json), Bash(), Read(auth.py, README.md)

This gives Claude concrete anchors to ask about — the actual files you changed — rather
than having to reconstruct activity from conversation history.

## 🌍 Portability / cross-device

Claude Code stores settings, hooks, and state on the **local filesystem** — none of
it syncs through your Claude account. This hook is built to travel: **no absolute
paths are baked in.** Both the counter state and the default journal live under
`<claude-dir>/state/`, so you have two clean options:

- **Copy `~/.claude/` to the new machine** — hooks, state, and journal come with it
  and just work.
- **Clone this repo + run `./install.sh`** on each device — installs the hook and
  merges the config. The journal defaults to `<claude-dir>/state/learning_journal.md`
  there; set `POP_QUIZ_JOURNAL` if you want it somewhere tracked/backed-up.

Because the default is relative to `<claude-dir>`, the same hook works on every
machine with zero edits.

## 🔗 Related / prior art

I built this independently to solve my own problem — staying fluent in work an agent
did on my behalf. After building it I found
[flyte/claude-code-quiz-master](https://github.com/flyte/claude-code-quiz-master),
which targets the same "agentic context loss" problem with a **manual** `/quiz`
command and adds nice extras (grading levels, a review queue, module-focused quizzes,
forcing you to open files). Credit to them for prior work on the idea.

This project differs in approach: it's a **mandatory, automatic** hook that fires on
an action-count cadence (every ~40–45 actions) across _every_ chat, rather than a
command you have to remember to run. The two are complementary — pull vs. push.

## 📜 Changelog

Version history lives in **[CHANGELOG.md](CHANGELOG.md)**. Current release: **v0.4.0**
(spaced repetition, accuracy in `status`, fairer MCQs, and the synthetic-prompt /
concise-answer bug fixes).

## 📄 License

MIT © 2026 Jayakrishna Konda — see [LICENSE](LICENSE).
