"""
Proves the answer key: runs every code snippet that has a `verify` mode and checks the result
against the question's correct answer. Also checks the bank's structure and selection.

Run:  python -m unittest -v tests.test_questions
"""

from __future__ import annotations

import contextlib
import io
import random
import unittest
from collections import Counter

from common.protocol import AnswerKind
from server import questions as Q


def run_snippet(code: str) -> tuple[str, BaseException | None]:
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            exec(compile(code, "<question>", "exec"), {"__name__": "__question__"})
    except Exception as exc:  # noqa: BLE001 - we want the exception object
        return out.getvalue(), exc
    return out.getvalue(), None


class AnswerKeyTests(unittest.TestCase):

    def test_verifiable_snippets_match_answer_key(self):
        checked = 0
        for q in Q.QUESTIONS:
            if not q.verify:
                continue
            with self.subTest(q=q.qid):
                stdout, exc = run_snippet(q.code)
                if q.verify == "stdout":
                    self.assertIsNone(exc, f"{q.qid} raised {exc!r}")
                    self.assertTrue(q.is_correct(stdout.strip()) or stdout.strip() == q.correct_text(),
                                    f"{q.qid}: printed {stdout.strip()!r}, key says {q.correct_text()!r}")
                elif q.verify == "lines":
                    self.assertIsNone(exc)
                    self.assertEqual(str(len(stdout.strip().splitlines())), q.correct_text())
                elif q.verify.startswith("exception:"):
                    self.assertIsNotNone(exc, f"{q.qid} did not raise")
                    self.assertEqual(type(exc).__name__, q.verify.split(":", 1)[1])
                else:
                    self.fail(f"unknown verify mode {q.verify}")
                checked += 1
        self.assertGreaterEqual(checked, 15)

    def test_text_answers_are_normalised(self):
        q = Q.QUESTIONS_BY_ID["PO-04"]
        self.assertTrue(q.is_correct("  hsal "))
        self.assertTrue(q.is_correct("HSAL"))
        self.assertFalse(q.is_correct("hsalC"))
        self.assertTrue(Q.QUESTIONS_BY_ID["DB-09"].is_correct("typeerror"))


class BankStructureTests(unittest.TestCase):

    def test_ids_unique_and_categories_balanced(self):
        ids = [q.qid for q in Q.QUESTIONS]
        self.assertEqual(len(ids), len(set(ids)))
        counts = Counter(q.category for q in Q.QUESTIONS)
        self.assertEqual(set(counts), set(Q.CATEGORIES))
        self.assertTrue(all(c >= 8 for c in counts.values()), counts)

    def test_choice_questions_well_formed(self):
        for q in Q.QUESTIONS:
            with self.subTest(q=q.qid):
                if q.kind == AnswerKind.CHOICE:
                    self.assertEqual([k for k, _ in q.options], ["A", "B", "C", "D"])
                    self.assertIn(q.answer, q.option_keys())
                    self.assertEqual(len({t for _, t in q.options}), 4, "duplicate option text")
                    self.assertTrue(q.is_correct(q.answer.lower()))
                else:
                    self.assertEqual(q.options, ())
                self.assertTrue(q.explanation)

    def test_answer_keys_are_varied(self):
        keys = Counter(q.answer for q in Q.QUESTIONS if q.kind == AnswerKind.CHOICE)
        self.assertTrue(all(keys[k] >= 4 for k in "ABCD"), keys)

    def test_public_payload_hides_answer(self):
        for q in Q.QUESTIONS:
            payload = q.public_payload()
            self.assertNotIn("answer", payload)
            self.assertNotIn("explanation", payload)

    def test_pick_questions_balanced_and_unique(self):
        for seed in range(20):
            picked = Q.pick_questions(8, random.Random(seed))
            self.assertEqual(len(picked), 8)
            self.assertEqual(len({q.qid for q in picked}), 8)
            self.assertEqual(set(Counter(q.category for q in picked).values()), {2})
        self.assertEqual(len(Q.pick_questions(500)), len(Q.QUESTIONS))


if __name__ == "__main__":
    unittest.main(verbosity=2)
