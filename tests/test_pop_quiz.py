#!/usr/bin/env python3
"""Tests for claude-pop-quiz. Stdlib unittest only — no pip installs, matching
the hook's zero-dependency stance. Run: python3 -m unittest discover -s tests

Pure-function tests import hooks/pop_quiz.py directly. The fire->answer cycle is
exercised end-to-end via subprocess against a COPY of the hook in a temp dir, so
the test never touches the repo's own state/ or the developer's ~/.claude.
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK = os.path.join(ROOT, "hooks", "pop_quiz.py")


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
| 2026-01-15 | x | 1 ✅ · 1 \U0001f7e1 · 1 ❌ |

## \U0001f4c5 2026-01-15
### ✅ Q1 · HTTP cache headers
> q
### \U0001f7e1 Q2 · Database index trade-offs
> q
### ❌ Q3 · Connection pooling
> q
"""


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
            "## 2026-02-02\n"
            "### ✅ Q1 · Caching\n> what is caching again?\n"
            "- **Answer:** reuse stored results.\n"
            "### ❌ Q2 · Pooling\n> what is pooling?\n"
            "- **Answer:** reuse warm connections.\n"
            "## 2026-02-01\n"
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
        import io
        from contextlib import redirect_stdout

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


if __name__ == "__main__":
    unittest.main()
