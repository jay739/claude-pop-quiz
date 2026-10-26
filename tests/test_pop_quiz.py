#!/usr/bin/env python3
"""Tests for claude-pop-quiz. Stdlib unittest only — no pip installs, matching
the hook's zero-dependency stance. Run: python3 -m unittest discover -s tests

Pure-function tests import hooks/pop_quiz.py directly. The fire->answer cycle is
exercised end-to-end via subprocess against a COPY of the hook in a temp dir, so
the test never touches the repo's own state/ or the developer's ~/.claude.
"""

import datetime
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK = os.path.join(ROOT, "hooks", "pop_quiz.py")


def _d(days_ago=0):
    """An ISO date `days_ago` days in the past. Journal fixtures use these
    rather than fixed dates because review intervals are now time-based, so a
    hardcoded 2026-01-15 would silently fall due as the suite ages."""
    return (datetime.date.today() - datetime.timedelta(days=days_ago)).isoformat()


def _load_module():
    spec = importlib.util.spec_from_file_location("pop_quiz_under_test", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pq = _load_module()


class ClassifyTests(unittest.TestCase):
    def test_concise_correct_answer_is_not_a_defer(self):
        # The regression that motivated v0.4.0: a short, correct reply used to be
        # scored as a skip because it was under 80 chars with one period.
        self.assertEqual(pq.classify("It memoizes with functools.lru_cache."), "answer")
        self.assertEqual(pq.classify("Because flock serializes the writes."), "answer")

    def test_mcq_shape_is_an_answer(self):
        self.assertEqual(pq.classify("1C 2A 3D 4B 5A"), "answer")
        self.assertEqual(pq.classify("1) c, 2) a, 3) d"), "answer")

    def test_explicit_defer(self):
        for word in ("defer", "skip quiz", "not now", "later.", "skip the quiz"):
            self.assertEqual(pq.classify(word), "defer", word)

    def test_new_work_instruction_is_a_soft_defer(self):
        for msg in ("let's continue the refactor", "run the tests", "what about auth?"):
            self.assertEqual(pq.classify(msg), "defer", msg)

    def test_empty_is_a_defer(self):
        self.assertEqual(pq.classify(""), "defer")
        self.assertEqual(pq.classify("   "), "defer")

    def test_questions_are_not_answers(self):
        # Regression: "who is the founder of git" used to grade as an answer
        # because no interrogative covered it. Questions must classify as defer.
        for q in (
            "who is the founder of git",
            "which branch did that merge into",
            "the founder of git?",
            "is macro F1 just the unweighted mean?",
        ):
            self.assertEqual(pq.classify(q), "defer", q)


class McqShapeTests(unittest.TestCase):
    def test_majority_pairs_required(self):
        self.assertTrue(pq.looks_like_mcq_answer("1C 2A 3D 4B 5A"))
        self.assertFalse(pq.looks_like_mcq_answer("I changed section 2 a bit"))

    def test_prose_does_not_trip_it(self):
        self.assertFalse(pq.looks_like_mcq_answer("the answer is that it caches"))


class SyntheticPromptTests(unittest.TestCase):
    def test_detects_harness_events(self):
        self.assertTrue(
            pq._is_synthetic_prompt("[SYSTEM NOTIFICATION - NOT USER INPUT] done")
        )
        self.assertTrue(
            pq._is_synthetic_prompt(
                "<task-notification><task-id>9</task-id></task-notification>"
            )
        )

    def test_real_text_passes(self):
        self.assertFalse(pq._is_synthetic_prompt("explain the lock"))
        self.assertFalse(pq._is_synthetic_prompt(""))


class VersionTests(unittest.TestCase):
    def test_ordering(self):
        self.assertGreater(pq._parse_version("0.4.0"), pq._parse_version("0.3.9"))
        self.assertGreater(pq._parse_version("1.0.0"), pq._parse_version("0.99.0"))
        self.assertEqual(pq._parse_version("v0.4.0"), (0, 4, 0))


SAMPLE_JOURNAL = """# Journal
**Legend** · ✅ correct · \U0001f7e1 partial · ❌ missed
| Date | Topic | Score |
| {date} | x | 1 ✅ · 1 \U0001f7e1 · 1 ❌ |

## \U0001f4c5 {date}
### ✅ Q1 · HTTP cache headers
> q
### \U0001f7e1 Q2 · Database index trade-offs
> q
### ❌ Q3 · Connection pooling
> q
""".format(date=_d(0))


class JournalParsingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(
            "w", suffix=".md", delete=False, encoding="utf-8"
        )
        self.tmp.write(SAMPLE_JOURNAL)
        self.tmp.close()
        self._orig = pq.JOURNAL
        pq.JOURNAL = self.tmp.name

    def tearDown(self):
        pq.JOURNAL = self._orig
        os.unlink(self.tmp.name)

    def test_accuracy_counts_only_question_headers(self):
        # Legend + summary-table emojis must NOT be counted, only the ### Qn rows.
        acc = pq._journal_accuracy()
        self.assertEqual(
            acc, {"correct": 1, "partial": 1, "missed": 1, "total": 3, "pct": 50}
        )

    def test_srs_due_topics_are_partial_and_missed_only(self):
        # One ✅ each promotes nothing here; the 🟡 and ❌ topics stay due (box<=1).
        self.assertEqual(
            sorted(pq._srs_due_topics()),
            ["Connection pooling", "Database index trade-offs"],
        )

    def test_accuracy_none_when_journal_missing(self):
        pq.JOURNAL = "/nonexistent/path/journal.md"
        self.assertIsNone(pq._journal_accuracy())
        self.assertEqual(pq._srs_due_topics(), [])
        self.assertEqual(pq._journal_entries(), [])


class LeitnerSrsTests(unittest.TestCase):
    """Verdict history -> Leitner box, driving due/mastered/streak selection."""

    def setUp(self):
        # Oldest section last (journal is newest-first). Caching: ✅ then ✅ -> box 3
        # (mastered). Pooling: ✅ then ❌ -> box 0 (due). Newest verdict is Caching ✅.
        journal = (
            "# J\n"
            f"## {_d(0)}\n"
            "### ✅ Q1 · Caching\n> what is caching again?\n"
            "- **Answer:** reuse stored results.\n"
            "### ❌ Q2 · Pooling\n> what is pooling?\n"
            "- **Answer:** reuse warm connections.\n"
            f"## {_d(1)}\n"
            "### ✅ Q1 · Caching\n> what is caching?\n"
            "- **Answer:** store results.\n"
            "### ✅ Q2 · Pooling\n> pooling intro?\n"
            "- **Answer:** a pool.\n"
        )
        self.tmp = tempfile.NamedTemporaryFile(
            "w", suffix=".md", delete=False, encoding="utf-8"
        )
        self.tmp.write(journal)
        self.tmp.close()
        self._orig = pq.JOURNAL
        pq.JOURNAL = self.tmp.name

    def tearDown(self):
        pq.JOURNAL = self._orig
        os.unlink(self.tmp.name)

    def test_boxes_promote_on_correct_and_reset_on_miss(self):
        boxes = pq._topic_boxes()
        self.assertEqual(boxes["caching"]["box"], 3)
        self.assertEqual(boxes["pooling"]["box"], 0)

    def test_due_and_mastered_split(self):
        self.assertEqual(pq._srs_due_topics(), ["Pooling"])
        self.assertEqual(pq._mastered_topics(), ["Caching"])

    def test_streak_counts_leading_correct_newest_first(self):
        # Newest entry is Caching ✅; the next is Pooling ❌ -> streak of 1.
        self.assertEqual(pq._current_streak(), 1)

    def test_entries_carry_question_and_answer(self):
        first = pq._journal_entries()[0]
        self.assertEqual(first["title"], "Caching")
        self.assertEqual(first["verdict"], pq._VERDICT_GOOD)
        self.assertIn("reuse stored results", first["answer"])

    def test_review_lists_due_topics(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            pq.cmd_review()
        out = buf.getvalue()
        self.assertIn("Pooling", out)
        self.assertNotIn("Caching", out)  # mastered topics are not drilled


class EndToEndCycleTests(unittest.TestCase):
    """Run the hook as a real subprocess against an isolated copy so BASE/state
    resolve into the temp dir, never the repo."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        hooks_dir = os.path.join(self.dir, "hooks")
        os.makedirs(hooks_dir)
        self.hook = os.path.join(hooks_dir, "pop_quiz.py")
        shutil.copy(HOOK, self.hook)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _run(self, mode, prompt, **env):
        e = dict(os.environ, POP_QUIZ_NO_UPDATE_CHECK="1", **env)
        payload = json.dumps({"session_id": "s1", "prompt": prompt})
        out = subprocess.run(
            [sys.executable, self.hook, mode],
            input=payload,
            capture_output=True,
            text=True,
            env=e,
        )
        return out.stdout

    def test_quiz_fires_then_grades(self):
        # MIN=MAX=1 so the very first prompt crosses the threshold.
        fire = self._run("prompt", "do the thing", POP_QUIZ_MIN="1", POP_QUIZ_MAX="1")
        self.assertIn("LEARNING CHECK", fire)
        # An answer-shaped reply gets the grading directive next turn.
        grade = self._run(
            "prompt", "1C 2A 3D 4B 5A", POP_QUIZ_MIN="1", POP_QUIZ_MAX="1"
        )
        self.assertIn("answered the pending learning check", grade)
        self.assertIn("Context:", grade)  # journal directive carries the new field

    def test_synthetic_prompt_does_not_consume_pending_quiz(self):
        self._run("prompt", "do the thing", POP_QUIZ_MIN="1", POP_QUIZ_MAX="1")
        # A harness task-notification must be ignored (no grading directive).
        passthru = self._run(
            "prompt",
            "[SYSTEM NOTIFICATION - NOT USER INPUT] background task finished",
            POP_QUIZ_MIN="1",
            POP_QUIZ_MAX="1",
        )
        self.assertEqual(passthru.strip(), "")
        # The real answer afterwards still grades — the quiz stayed pending.
        grade = self._run(
            "prompt", "1C 2A 3D 4B 5A", POP_QUIZ_MIN="1", POP_QUIZ_MAX="1"
        )
        self.assertIn("answered the pending learning check", grade)

    def test_freeze_only_unlocks_on_mcq_answer(self):
        # Drive the chat to the freeze (DEFER_LIMIT=1), then confirm a plain
        # question can NOT pop it — only one-line MCQ letters do.
        env = dict(POP_QUIZ_MIN="1", POP_QUIZ_MAX="1", POP_QUIZ_DEFER_LIMIT="1")
        self._run("prompt", "do the thing", **env)
        lock = self._run("prompt", "defer", **env)
        self.assertIn("FROZEN", lock)
        # A stray question must keep it frozen, not grade.
        still = self._run("prompt", "who is the founder of git", **env)
        self.assertIn("FROZEN", still)
        self.assertNotIn("answered the pending learning check", still)
        # The documented one-line MCQ shape unlocks and grades.
        grade = self._run("prompt", "1C 2A 3D 4B 5A", **env)
        self.assertIn("answered the pending learning check", grade)


class UpdatePreservesStateTests(unittest.TestCase):
    """An update rewrites ONLY the script file; the defer counter and the rest
    of state must survive untouched. Regression guard for the user's report that
    'updating resets the defer flag'. Runs network-free by stubbing the fetch."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.dir, "hooks"))
        os.makedirs(os.path.join(self.dir, "state"))
        self.hook = os.path.join(self.dir, "hooks", "pop_quiz.py")
        shutil.copy(HOOK, self.hook)
        self.state = os.path.join(self.dir, "state", "pop_quiz_state.json")
        with open(self.state, "w") as f:
            json.dump({"_global": {"defers": 3, "locked": True, "stats": {}}}, f)
        with open(self.state, "rb") as f:
            self.before = f.read()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_do_update_leaves_state_untouched(self):
        # Import the isolated copy so its __file__/STATE_FILE resolve into tmp.
        spec = importlib.util.spec_from_file_location("pq_update_copy", self.hook)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        with open(self.hook, "rb") as f:
            payload = f.read()  # a valid pop_quiz.py (all markers, > 4096 bytes)
        mod._remote_version = lambda timeout=2: "9.9.9"
        mod._fetch_remote = lambda timeout, max_bytes=0: payload
        applied, latest, msg = mod._do_update()
        self.assertTrue(applied, msg)
        self.assertEqual(latest, "9.9.9")
        # The state file must be byte-for-byte unchanged -> defers preserved.
        with open(self.state, "rb") as f:
            self.assertEqual(f.read(), self.before)


# --- regressions fixed in 0.5.2 ----------------------------------------------


class AnswerOpenerTests(unittest.TestCase):
    """A real essay answer must never be charged as a defer.

    The classifier used to treat a long list of first words as "new work",
    including "this", "so", "yes", "is" and "that's" — all ordinary ways to
    begin an answer. With POP_QUIZ_DEFER_LIMIT set, answering correctly could
    therefore freeze every chat.
    """

    def test_common_answer_openers_are_answers(self):
        for msg in (
            "So the flock is advisory, which means a process that ignores it "
            "can still write.",
            "This script uses os.replace because it is atomic on POSIX.",
            "Yes, the lock file is advisory only.",
            "That's because the counter is keyed by session_id.",
            "It does, since tailscaled intercepts in userspace.",
            "Docker gives each container its own network namespace.",
            "git keeps objects reachable from refs, so the orphan survives.",
            "No, the journal is never touched by an update.",
            "Also the threshold is randomised per chat.",
            "Actually the PreToolUse hook is the one that denies.",
            "These are atomic writes via os.replace.",
            "Because the state file is keyed per session, both chats count "
            "separately.",
        ):
            self.assertEqual(pq.classify(msg), "answer", msg)

    def test_bare_acknowledgements_are_still_defers(self):
        for msg in ("ok", "okay.", "sure", "thanks", "yes", "no", "got it", "hmm"):
            self.assertEqual(pq.classify(msg), "defer", msg)

    def test_work_instructions_are_still_defers(self):
        for msg in (
            "run the tests",
            "let's continue the refactor",
            "please fix the lint error",
            "commit this and push",
            "explain the lock to me",
            "skip it for now",
        ):
            self.assertEqual(pq.classify(msg), "defer", msg)

    def test_questions_are_still_defers(self):
        # The v0.5.1 freeze-bypass fix must not regress.
        for msg in (
            "who is the founder of git",
            "which branch did that merge into",
            "what does flock actually guarantee",
            "is there a way to see the state file?",
        ):
            self.assertEqual(pq.classify(msg), "defer", msg)


class HeaderToleranceTests(unittest.TestCase):
    """The journal parser must match what the MODEL writes, not only the ideal.

    On a real 283 KB journal the strict pattern matched 22 of 185 graded
    questions, because the grader renders the template's "<✅ correct | ...>" as
    "### ✅ correct Q1 · topic" and there was no slot for that word. Accuracy,
    adaptive difficulty, the Leitner boxes and the review drill were all reading
    12% of the history with no symptom.
    """

    SHAPES = (
        "### ✅ Q1 · Plain emoji",
        "### ✅ correct Q1 · Emoji and word",
        "### ❌ missed Q2 · Missed with word",
        "### \U0001f7e1 partial Q3 · Partial with word",
        "### ⚠️ unanswered Q4 · Invented fourth verdict",
        "#### ✅ Correct! Q5 · Punctuated and h4",
        "## ❌ wrong Q6 - Dashed separator at h2",
        "### **✅ Q7 · Bold heading**",
        "### ✅ right Q 8 : Spaced Q and colon",
    )

    def _parse(self, *lines):
        return pq._journal_entries("\n".join(lines) + "\n")

    def test_every_observed_shape_parses(self):
        for shape in self.SHAPES:
            entries = self._parse(shape, "> the question", "- **Answer:** a")
            self.assertEqual(len(entries), 1, f"did not parse: {shape}")

    def test_titles_are_extracted_not_the_next_line(self):
        # Separators must not be allowed to swallow the newline: a greedy \s*
        # would capture the following "> question" line as the topic title.
        entries = self._parse("### ✅ correct Q1 · Cache headers", "> the question")
        self.assertEqual(entries[0]["title"], "Cache headers")
        self.assertEqual(entries[0]["question"], "the question")

    def test_invented_verdict_counts_as_missed(self):
        entries = self._parse("### ⚠️ unanswered Q1 · Skipped one", "> q")
        self.assertEqual(entries[0]["verdict"], pq._VERDICT_MISS)

    def test_verdict_word_does_not_leak_into_the_title(self):
        entries = self._parse("### ❌ missed Q2 · Index trade-offs", "> q")
        self.assertEqual(entries[0]["title"], "Index trade-offs")

    def test_non_question_headings_are_ignored(self):
        text = (
            "# Journal\n"
            "**Legend** · ✅ correct · ❌ missed\n"
            f"## {_d(0)} · a session about ✅ things\n"
            "### ✅ Q1 · Real one\n> q\n"
        )
        entries = pq._journal_entries(text)
        self.assertEqual([e["title"] for e in entries], ["Real one"])


class TopicKeyTests(unittest.TestCase):
    """Titles are the primary key of the SRS, so near-identical ones must
    collapse. A real journal logged one topic twice as "`localhost` inside a
    reverse-proxy container" and "localhost inside a reverse-proxy container",
    so a correct second answer opened a new box instead of promoting."""

    def test_near_identical_titles_share_a_key(self):
        variants = (
            "`localhost` inside a reverse-proxy container",
            "localhost inside a reverse-proxy container",
            "localhost` inside a reverse-proxy container",
            "Localhost inside a reverse proxy container",
            "**localhost inside a reverse-proxy container**",
        )
        keys = {pq._topic_key(v) for v in variants}
        self.assertEqual(len(keys), 1, keys)

    def test_distinct_titles_keep_distinct_keys(self):
        self.assertNotEqual(pq._topic_key("Cache headers"), pq._topic_key("Caching"))

    def test_boxes_merge_backtick_variants(self):
        text = (
            f"## {_d(0)}\n"
            "### ✅ Q1 · `localhost` in a container\n> q\n"
            f"## {_d(3)}\n"
            "### ❌ Q1 · localhost in a container\n> q\n"
        )
        boxes = pq._topic_boxes(pq._journal_entries(text))
        self.assertEqual(len(boxes), 1)
        self.assertEqual(list(boxes.values())[0]["seen"], 2)

    def test_orphaned_backtick_is_cleaned_from_the_title(self):
        entries = pq._journal_entries("### ✅ Q1 · `localhost` in a container\n> q\n")
        self.assertEqual(entries[0]["title"], "localhost in a container")


class SrsDecayTests(unittest.TestCase):
    """Boxes need a clock. Without one a topic answered correctly twice retires
    permanently, so "mastered" means "never asked again" rather than "known"."""

    def _boxes(self, journal):
        return pq._topic_boxes(pq._journal_entries(journal))

    def test_recently_promoted_topic_is_not_due(self):
        j = f"## {_d(1)}\n### ✅ Q1 · Caching\n> q\n## {_d(2)}\n### ✅ Q2 · Caching\n> q\n"
        boxes = self._boxes(j)
        self.assertEqual(boxes[pq._topic_key("Caching")]["box"], 3)
        self.assertEqual(pq._srs_due_topics(boxes=boxes), [])
        self.assertEqual(pq._mastered_topics(boxes), ["Caching"])

    def test_stale_topic_falls_due_again(self):
        # Same history, but 200 days old: past the box-3 interval, so it should
        # resurface and no longer count as mastered.
        j = (
            f"## {_d(200)}\n### ✅ Q1 · Caching\n> q\n"
            f"## {_d(201)}\n### ✅ Q2 · Caching\n> q\n"
        )
        boxes = self._boxes(j)
        self.assertEqual(pq._srs_due_topics(boxes=boxes), ["Caching"])
        self.assertEqual(pq._mastered_topics(boxes), [])

    def test_missed_topic_is_due_regardless_of_date(self):
        j = f"## {_d(0)}\n### ❌ Q1 · Pooling\n> q\n"
        self.assertEqual(pq._srs_due_topics(boxes=self._boxes(j)), ["Pooling"])

    def test_undated_entries_are_treated_as_due(self):
        # No parseable section header: the safe failure is asking again, not
        # retiring something the user may never have learned.
        j = "### ✅ Q1 · Caching\n> q\n### ✅ Q2 · Caching\n> q\n"
        self.assertEqual(pq._srs_due_topics(boxes=self._boxes(j)), ["Caching"])

    def test_entries_carry_their_section_date(self):
        j = f"## {_d(5)} · a session\n### ✅ Q1 · Caching\n> q\n"
        entries = pq._journal_entries(j)
        self.assertEqual(
            entries[0]["date"],
            datetime.date.today().toordinal() - 5,
        )


class VocabularyTests(unittest.TestCase):
    """grade_directive writes the headers, so IT is what needs the existing
    topic titles. Without them the grader coins a new title per question: 173
    topics over 185 questions, only 4 ever repeated, so no box could ever be
    promoted and nothing was ever mastered."""

    def test_vocabulary_is_ranked_by_review_need(self):
        j = (
            f"## {_d(0)}\n"
            "### ❌ Q1 · Weak topic\n> q\n"
            "### ✅ Q2 · Strong topic\n> q\n"
        )
        boxes = pq._topic_boxes(pq._journal_entries(j))
        self.assertEqual(pq._topic_vocabulary(boxes=boxes)[0], "Weak topic")

    def test_grade_directive_carries_the_vocabulary_and_demands_exact_reuse(self):
        d = pq.grade_directive(False, None, vocab=["Cache headers", "Pooling"])
        self.assertIn("Cache headers", d)
        self.assertIn("Pooling", d)
        self.assertIn("CONTROLLED VOCABULARY", d)
        self.assertIn("character for character", d)

    def test_grade_directive_pins_the_header_shape_without_a_verdict_word(self):
        # The exact failure mode that cost 88% of the journal.
        d = pq.grade_directive(False, None, vocab=[])
        self.assertIn("### ✅ Q1", d)
        self.assertIn("NO word after it", d)

    def test_grade_directive_trim_note_matches_the_real_header_shape(self):
        # It used to tell the model to trim "## 📅 ..." headers, which the
        # journal never contained, so the cap silently trimmed nothing.
        orig = pq.JOURNAL_MAX
        try:
            pq.JOURNAL_MAX = 50
            d = pq.grade_directive(False, None, vocab=[])
            self.assertIn("'## <date> · <topic>'", d)
            self.assertNotIn("## 📅", d)
        finally:
            pq.JOURNAL_MAX = orig


class DoctorTests(unittest.TestCase):
    """doctor is the systemic fix: every failure in this hook is silent by
    construction, so the parse coverage has to be a number on a screen."""

    def _run_doctor(self, journal_text):
        tmp = tempfile.NamedTemporaryFile(
            "w", suffix=".md", delete=False, encoding="utf-8"
        )
        tmp.write(journal_text)
        tmp.close()
        orig = pq.JOURNAL
        pq.JOURNAL = tmp.name
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                code = pq.cmd_doctor()
        finally:
            pq.JOURNAL = orig
            os.unlink(tmp.name)
        return code, buf.getvalue()

    HEALTHY = (
        f"# J\n## {_d(0)} · s\n"
        "### ✅ Q1 · Alpha\n> q\n- **You said:** a\n- **Answer:** b\n"
        "### ❌ Q2 · Beta\n> q\n- **You said:** a\n- **Answer:** b\n"
    )

    def test_healthy_journal_reports_full_coverage(self):
        code, out = self._run_doctor(self.HEALTHY)
        self.assertIn("COVERAGE        : 100%", out)
        self.assertEqual(code, 0, out)

    def test_unparseable_headers_are_reported_as_a_problem(self):
        # A heading shape the parser cannot read must surface as a problem, not
        # as a silently lower topic count.
        drifted = (
            f"# J\n## {_d(0)} · s\n"
            "### Q1 ✅ Alpha backwards\n> q\n- **You said:** a\n- **Answer:** b\n"
        )
        code, out = self._run_doctor(drifted)
        self.assertEqual(code, 1)
        self.assertIn("UNRECOGNISED", out)
        self.assertIn("PROBLEM", out)

    def test_empty_journal_is_a_note_not_a_problem(self):
        code, out = self._run_doctor("")
        self.assertEqual(code, 0, out)
        self.assertIn("note:", out)

    def test_duplicate_wiring_is_detected(self):
        cmd = "python3 ~/.claude/hooks/pop_quiz.py prompt"
        orig = pq._wired_hooks
        try:
            pq._wired_hooks = lambda: {"UserPromptSubmit": [cmd, cmd]}
            code, out = self._run_doctor(self.HEALTHY)
        finally:
            pq._wired_hooks = orig
        self.assertEqual(code, 1)
        self.assertIn("2 pop-quiz hooks are wired", out)


class UpdateSafetyTests(unittest.TestCase):
    def setUp(self):
        pq._REF_CACHE.clear()

    def tearDown(self):
        pq._REF_CACHE.clear()
        for var in ("POP_QUIZ_CHANNEL", "POP_QUIZ_BRANCH", "POP_QUIZ_REPO"):
            os.environ.pop(var, None)

    def test_branch_channel_uses_the_branch(self):
        os.environ["POP_QUIZ_CHANNEL"] = "branch"
        os.environ["POP_QUIZ_BRANCH"] = "dev"
        self.assertEqual(pq._resolve_ref(), "dev")
        self.assertIn("branch dev", pq._ref_note())

    def test_release_channel_pins_to_the_tag(self):
        orig = pq._latest_release_tag
        try:
            pq._latest_release_tag = lambda timeout=3: "v9.9.9"
            self.assertEqual(pq._resolve_ref(), "v9.9.9")
            self.assertIn("release v9.9.9", pq._ref_note())
            self.assertIn("/v9.9.9/hooks/pop_quiz.py", pq._raw_url())
        finally:
            pq._latest_release_tag = orig

    def test_release_channel_falls_back_to_branch_when_unreleased(self):
        # An install must never be stranded with no update path, but the
        # fallback has to be visible rather than silent.
        orig = pq._latest_release_tag
        try:
            pq._latest_release_tag = lambda timeout=3: None
            self.assertEqual(pq._resolve_ref(), "main")
            self.assertIn("no release found", pq._ref_note())
        finally:
            pq._latest_release_tag = orig


class UpdateRejectsBrokenPayloadTests(unittest.TestCase):
    """A syntactically broken update would disable the hook on every machine
    with no symptom, because the wired command discards stderr."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.dir, "hooks"))
        self.hook = os.path.join(self.dir, "hooks", "pop_quiz.py")
        shutil.copy(HOOK, self.hook)
        spec = importlib.util.spec_from_file_location("pq_reject_copy", self.hook)
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)
        with open(self.hook, "rb") as f:
            self.original = f.read()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _attempt(self, payload):
        self.mod._remote_version = lambda timeout=2: "9.9.9"
        self.mod._fetch_remote = lambda timeout, max_bytes=0: payload
        return self.mod._do_update()

    def test_non_compiling_payload_is_rejected(self):
        broken = self.original.replace(b"def main():", b"def main(:")
        applied, _latest, msg = self._attempt(broken)
        self.assertFalse(applied)
        self.assertIn("does not compile", msg)
        # The running script must be left exactly as it was.
        with open(self.hook, "rb") as f:
            self.assertEqual(f.read(), self.original)

    def test_error_page_is_still_rejected(self):
        applied, _latest, msg = self._attempt(b"<html>404</html>")
        self.assertFalse(applied)
        self.assertIn("does not look like pop_quiz.py", msg)

    def test_valid_payload_still_applies(self):
        applied, _latest, msg = self._attempt(self.original)
        self.assertTrue(applied, msg)


class ErrorVisibilityTests(unittest.TestCase):
    """The wired command ends in "2>/dev/null || true", so a crash is invisible.
    It must be logged and surfaced instead."""

    def test_log_and_read_back_round_trip(self):
        tmpdir = tempfile.mkdtemp()
        orig_dir, orig_log = pq.STATE_DIR, pq.ERROR_LOG
        pq.STATE_DIR = tmpdir
        pq.ERROR_LOG = os.path.join(tmpdir, "errors.log")
        try:
            self.assertIsNone(pq._last_error())
            try:
                raise ValueError("boom in the hook")
            except ValueError:
                pq._log_error("prompt")
            err = pq._last_error()
            self.assertIsNotNone(err)
            self.assertIn("mode=prompt", err[0])
            self.assertIn("boom in the hook", err[1])
        finally:
            pq.STATE_DIR, pq.ERROR_LOG = orig_dir, orig_log
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_a_crash_never_breaks_the_users_turn(self):
        # state/ as a regular file makes os.makedirs raise inside the lock. The
        # hook must still exit 0 and print nothing, so the turn is unaffected.
        d = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(d, "hooks"))
            hook = os.path.join(d, "hooks", "pop_quiz.py")
            shutil.copy(HOOK, hook)
            with open(os.path.join(d, "state"), "w") as f:
                f.write("not a directory")
            out = subprocess.run(
                [sys.executable, hook, "prompt"],
                input=json.dumps({"session_id": "s1", "prompt": "hi"}),
                capture_output=True,
                text=True,
                env=dict(os.environ, POP_QUIZ_NO_UPDATE_CHECK="1"),
            )
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(out.stdout.strip(), "")
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
