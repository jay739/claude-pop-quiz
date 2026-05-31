# claude-pop-quiz

A **mandatory, automatic** learning-check hook for [Claude Code](https://github.com/anthropics/claude-code).

Agentic coding lets you ship work without reading what the agent wrote. The risk: you end up unable to *explain* code, decisions, and tooling that are nominally "yours" — in an interview, a review, or just when the agent isn't around. This hook counters that by **pop-quizzing you on your own sessions, on a cadence you can't skip.**

Every ~40–45 *actions* in a chat (your messages **plus** every tool/file/agent call Claude makes), it pauses and makes you answer 5 questions about the technical substance of that session — the jargon used, the code written and *why*, and the files touched. You answer in your own words; Claude grades you and flags gaps.

After grading, Claude appends the results to a **learning journal** — a markdown revision log of every quiz: each question, your answer, the correct answer, a ✅/🟡/❌ verdict, and links to study. Re-read it later, or point it at a git repo to version your progress. See [`JOURNAL.example.md`](JOURNAL.example.md) for the format. Your real journal is personal — it's gitignored by default.

## How it works

It's two hooks pointing at one script, wired in your Claude Code `settings.json`:

| Event | Mode | What it does |
|---|---|---|
| `PreToolUse` (all tools) | `tool` | Increments the per-chat counter **silently** — never interrupts |
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

### Scope

- Put the hook in **`~/.claude/settings.json`** (user scope) → fires in *every* project.
- Put it in a project's **`.claude/settings.json`** → fires only in that project.

## Configuration

All optional, via environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `POP_QUIZ_MIN` | `40` | Lower bound of the random action threshold |
| `POP_QUIZ_MAX` | `45` | Upper bound of the random action threshold |
| `POP_QUIZ_QUESTIONS` | `5` | Number of questions per check |
| `POP_QUIZ_FORMAT` | `essay` | `essay`, `mcq`, or `mixed` — see [Quiz format](#quiz-format--cant-i-just-skip-it) |
| `POP_QUIZ_JOURNAL` | `<claude-dir>/state/learning_journal.md` | Path to the markdown learning journal |

The 40–45 default is tuned for typical chat lengths; raise it for longer sessions.
By default the journal lives under `state/` (gitignored — it's personal data); set
`POP_QUIZ_JOURNAL` to a tracked path if you want to version or back it up.
Example — quiz less often with 3 questions, journal into a repo:

```json
"command": "POP_QUIZ_MIN=90 POP_QUIZ_MAX=110 POP_QUIZ_QUESTIONS=3 POP_QUIZ_JOURNAL=$HOME/notes/learning_journal.md python3 ~/.claude/hooks/pop_quiz.py prompt 2>/dev/null || true"
```

## Quiz format & "can't I just skip it?"

A hook injects *instructions*, not a hard lock — Claude can't physically force you
to answer, and a determined user can always say "skip." So instead of fighting that,
the design **removes the reason to skip**: the usual excuse is "I don't have time to
write essays," so there's a fast path.

| `POP_QUIZ_FORMAT` | What you get |
|---|---|
| `essay` *(default)* | Free-response answers in your own words. If you're short on time or try to bail, Claude offers the **MCQ quick version** of the *same* questions instead of letting you skip outright. |
| `mcq` | Every question is **multiple choice** (A–D, one correct). You answer in a single line of letters — e.g. `1C 2A 3D 4B 5A`. Seconds, no typing. |
| `mixed` | Half free-response, half multiple choice. |

Either way you still get graded, corrected, and **journaled** — so a 15-second MCQ
round is real revision, not a bypass. Set it globally, e.g.:

```json
"command": "POP_QUIZ_FORMAT=mcq python3 ~/.claude/hooks/pop_quiz.py prompt 2>/dev/null || true"
```

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
