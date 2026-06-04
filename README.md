# claude-pop-quiz

A **mandatory, automatic** learning-check hook for [Claude Code](https://github.com/anthropics/claude-code).

Agentic coding lets you ship work without reading what the agent wrote. The risk: you end up unable to *explain* code, decisions, and tooling that are nominally "yours" — in an interview, a review, or just when the agent isn't around. This hook counters that by **pop-quizzing you on your own sessions, on a cadence you can't skip.**

Every ~40–45 *actions* in a chat (your messages **plus** every tool/file/agent call Claude makes), it pauses and makes you answer 5 questions about the technical substance of that session — the jargon used, the code written and *why*, and the files touched. You answer in your own words; Claude grades you and flags gaps.

After grading, Claude appends the results to a **learning journal** — a markdown revision log of every quiz: each question, your answer, the correct answer, a ✅/🟡/❌ verdict, and links to study. Re-read it later, or point it at a git repo to version your progress. See [`JOURNAL.example.md`](JOURNAL.example.md) for the format. Your real journal is personal — it's gitignored by default.

## How it works

It's two hooks pointing at one script, wired in your Claude Code `settings.json`:

| Event | Mode | What it does |
|---|---|---|
| `PreToolUse` (all tools) | `tool` | Increments the per-chat counter **silently**, records the tool name and files touched for targeted questions — never interrupts |
| `UserPromptSubmit` | `prompt` | Increments, and if the chat crossed its threshold, **injects the quiz** and resets |

Design choices worth knowing:

- **Counts all activity, not just messages.** A heavily agentic chat with 3 typed messages but 80 tool calls still gets examined. (`UserPromptSubmit` alone would never fire there.)
- **Fires only at a message boundary.** Tool calls increment but never inject, so the quiz appears at the *start of your next message* — never mid-task.
- **Per chat.** State is keyed by `session_id`, so concurrent chats each track their own count.
- **Randomized threshold** (default 40–45) so the cadence isn't gameable.
- **Self-contained.** State lives under `<claude-dir>/state/`; copy `~/.claude/` to another machine and it just works.

## Install

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

### Scope

- Put the hook in **`~/.claude/settings.json`** (user scope) → fires in *every* project.
- Put it in a project's **`.claude/settings.json`** → fires only in that project.

## Configuration

**Everything is optional and tweakable** — the hook ships with sane defaults and
nothing is hard-coded into the script. Every knob is an environment variable:

| Variable | Default | What it does |
|---|---|---|
| `POP_QUIZ_MIN` | `40` | Lower bound of the random action threshold (how *often* it fires) |
| `POP_QUIZ_MAX` | `45` | Upper bound of the random action threshold |
| `POP_QUIZ_QUESTIONS` | `5` | Number of questions per check |
| `POP_QUIZ_FORMAT` | `essay` | `essay`, `mcq`, or `mixed` — see [Quiz format](#quiz-format--cant-i-just-skip-it) |
| `POP_QUIZ_DEFER_LIMIT` | `0` | Consecutive defers (across all chats) before tool use **freezes**; `0` = soft mode, never freeze |
| `POP_QUIZ_JOURNAL` | `<claude-dir>/state/learning_journal.md` | Where the graded journal is written |
| `POP_QUIZ_JOURNAL_MAX_ENTRIES` | `0` | Keep at most this many dated entries in the journal; `0` = unlimited |

### How to set / change them

These values get **baked into the hook command** in your `~/.claude/settings.json`.
Two ways to change them — both safe to repeat:

1. **Re-run the installer with env vars** (it rewrites the command for you):
   ```bash
   POP_QUIZ_FORMAT=mcq POP_QUIZ_DEFER_LIMIT=3 ./install.sh
   ```
   Run with no env and it also **prompts** you for format, defer limit, and max journal entries.

2. **Edit the command directly** in `~/.claude/settings.json` — prepend the vars
   to *both* the `UserPromptSubmit` and `PreToolUse` commands (the freeze needs
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

## Quiz format & "can't I just skip it?"

### Format: pick your friction

| `POP_QUIZ_FORMAT` | What you get |
|---|---|
| `essay` *(default)* | Free-response answers in your own words. If you're short on time, Claude offers the **MCQ quick version** of the *same* questions. |
| `mcq` | Every question is **multiple choice** (A–D, one correct). You answer in a single line of letters — e.g. `1C 2A 3D 4B 5A`. Seconds, no typing. |
| `mixed` | Half free-response, half multiple choice. |

Either way you're graded, corrected, and **journaled** — a 15-second MCQ round is
real revision, not a bypass.

### Defer & the hard stop

When a quiz fires you can **defer** — say "defer" / "skip quiz", or just keep
working. But defers are counted **globally, across every chat** (opening a fresh
chat doesn't reset them). Once you hit `POP_QUIZ_DEFER_LIMIT`, the `PreToolUse`
hook **denies every tool call** — Claude can talk but can't edit, run, or search
anything until you answer a quiz. Submitting an answer (the hook recognizes the
one-line `1C 2A 3D 4B 5A` shape) resets the counter and unlocks.

```json
"command": "POP_QUIZ_FORMAT=mcq POP_QUIZ_DEFER_LIMIT=3 python3 ~/.claude/hooks/pop_quiz.py prompt 2>/dev/null || true"
```

### Honest about the limits

This is a **discipline gate, not a vault.** Two things no hook can do:

1. **Judge understanding.** The hook checks an answer's *shape*, not its
   correctness — only the model grades that, and the model is cooperative, so it
   can be talked past. The hard stop forces you to *submit an attempt*, not to
   pass.
2. **Defend against its owner.** It's your `settings.json` and your script — one
   commented line disables it. It exists to stop *drift*, not a determined you.

The real lever is making the quiz cheap (MCQ = seconds) so answering beats
dodging. The freeze is the backstop for when you'd otherwise let it slide.

> **Live per-question timer?** Not possible in Claude Code — it's turn-based, and
> nothing runs during your think-time to enforce a countdown or auto-skip. The
> hook *can* read the clock and log how long you took, and a non-answer counts as
> a defer, but there's no live 15-second timer.

## Per-project topic hints

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

## Targeted questions via tool context

The `PreToolUse` hook now records every tool name and the files it touches into the
session state. When a quiz fires, the directive includes a summary like:

> Tools used this session: Edit(auth.py, config.json), Bash(), Read(auth.py, README.md)

This gives Claude concrete anchors to ask about — the actual files you changed — rather
than having to reconstruct activity from conversation history.

## Portability / cross-device

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

## Related / prior art

I built this independently to solve my own problem — staying fluent in work an agent
did on my behalf. After building it I found
[flyte/claude-code-quiz-master](https://github.com/flyte/claude-code-quiz-master),
which targets the same "agentic context loss" problem with a **manual** `/quiz`
command and adds nice extras (grading levels, a review queue, module-focused quizzes,
forcing you to open files). Credit to them for prior work on the idea.

This project differs in approach: it's a **mandatory, automatic** hook that fires on
an action-count cadence (every ~40–45 actions) across *every* chat, rather than a
command you have to remember to run. The two are complementary — pull vs. push.

## License

MIT © 2026 Jayakrishna Konda — see [LICENSE](LICENSE).
