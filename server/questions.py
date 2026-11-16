"""
CodeClash question bank — SERVER ONLY (it contains the answer key).

Four categories, medium difficulty:
  * Predict the Output
  * Programming Logic
  * Algorithms & Complexity
  * Debugging & Code Analysis

Clients only ever receive Question.public_payload(), which has no answer. Correctness is
decided here, on the server, by Question.is_correct().

The `verify` field is used by tests/test_questions.py to execute code snippets and prove that
every answer key is right:
  "stdout"           -> the printed output equals the correct answer text
  "lines"            -> the number of printed lines equals the correct answer
  "exception:<Name>" -> running the snippet raises that exception type
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from common.protocol import AnswerKind, normalize_text_answer

PREDICT = "Predict the Output"
LOGIC = "Programming Logic"
ALGO = "Algorithms & Complexity"
DEBUG = "Debugging & Code Analysis"
CATEGORIES = (PREDICT, LOGIC, ALGO, DEBUG)


@dataclass(frozen=True)
class Question:
    qid: str
    category: str
    prompt: str
    code: str | None
    kind: str                                   # AnswerKind.CHOICE or AnswerKind.TEXT
    answer: str                                 # option key ("B") or canonical text answer
    options: tuple[tuple[str, str], ...] = ()   # ((key, text), ...) for CHOICE questions
    accepted: tuple[str, ...] = ()              # extra accepted spellings for TEXT questions
    explanation: str = ""
    verify: str | None = field(default=None, compare=False)

    # ---- server-side checks -------------------------------------------------
    def option_keys(self) -> set[str]:
        return {key for key, _ in self.options}

    def is_valid_answer(self, answer: str) -> bool:
        """Shape check only (not correctness): a known option key, or non-empty text."""
        if self.kind == AnswerKind.CHOICE:
            return answer.strip().upper() in self.option_keys()
        return bool(answer.strip())

    def is_correct(self, answer: str) -> bool:
        if self.kind == AnswerKind.CHOICE:
            return answer.strip().upper() == self.answer
        accepted = {normalize_text_answer(self.answer)} | {normalize_text_answer(a) for a in self.accepted}
        return normalize_text_answer(answer) in accepted

    def correct_text(self) -> str:
        if self.kind == AnswerKind.CHOICE:
            return dict(self.options)[self.answer]
        return self.answer

    # ---- what clients may see -----------------------------------------------
    def public_payload(self) -> dict:
        """Question fields sent in QUESTION. Deliberately contains NO answer."""
        return {
            "question_id": self.qid,
            "category": self.category,
            "prompt": self.prompt,
            "code": self.code,
            "answer_kind": self.kind,
            "options": [{"key": k, "text": t} for k, t in self.options],
        }


def _choice(qid, category, prompt, code, options, answer, explanation, verify=None) -> Question:
    keys = "ABCD"
    return Question(qid=qid, category=category, prompt=prompt, code=code, kind=AnswerKind.CHOICE,
                    answer=answer, options=tuple(zip(keys, options)), explanation=explanation, verify=verify)


def _text(qid, category, prompt, code, answer, explanation, accepted=(), verify=None) -> Question:
    return Question(qid=qid, category=category, prompt=prompt, code=code, kind=AnswerKind.TEXT,
                    answer=answer, accepted=tuple(accepted), explanation=explanation, verify=verify)


QUESTIONS: tuple[Question, ...] = (
    # ------------------------------------------------------------------ Predict the Output
    _choice("PO-01", PREDICT, "What does this code print?",
            "x = [1, 2, 3]\ny = x\ny.append(4)\nprint(len(x))",
            ["3", "None", "4", "An error is raised"], "C",
            "y = x copies the reference, not the list. Both names point to the same list, so x has 4 items.",
            verify="stdout"),
    _choice("PO-02", PREDICT, "What does this code print?",
            "print(7 // 2, 7 % 2, -7 // 2)",
            ["3 1 -3", "3.5 1 -3.5", "4 1 -4", "3 1 -4"], "D",
            "// is floor division: it rounds toward negative infinity, so -7 // 2 is -4, not -3.",
            verify="stdout"),
    _choice("PO-03", PREDICT, "What does this code print?",
            "def add(item, bucket=[]):\n    bucket.append(item)\n    return bucket\n\nadd(1)\nprint(add(2))",
            ["[2]", "[1, 2]", "[2, 2]", "An error is raised"], "B",
            "The default list is created once, when the function is defined, and is shared between calls.",
            verify="stdout"),
    _text("PO-04", PREDICT, "Type exactly what this code prints.",
          's = "CodeClash"\nprint(s[::-1][:4])',
          "hsal",
          's[::-1] reverses the string to "hsalCedoC"; [:4] keeps the first four characters.',
          verify="stdout"),
    _choice("PO-05", PREDICT, "What does this code print?",
            "nums = [3, 1, 2]\nresult = sorted(nums)\nprint(result, nums)",
            ["[1, 2, 3] [3, 1, 2]", "[1, 2, 3] [1, 2, 3]", "None [1, 2, 3]", "[3, 1, 2] [1, 2, 3]"], "A",
            "sorted() returns a NEW sorted list and leaves the original unchanged (unlike list.sort()).",
            verify="stdout"),
    _text("PO-06", PREDICT, "Type exactly what this code prints.",
          "print(sum(i * i for i in range(4)))",
          "14",
          "range(4) is 0, 1, 2, 3; their squares 0 + 1 + 4 + 9 = 14.",
          verify="stdout"),
    _choice("PO-07", PREDICT, "What does this code print?",
            "a = (1, 2, 3)\nfirst, *rest = a\nprint(rest)",
            ["(2, 3)", "2", "[2, 3]", "An error is raised"], "C",
            "Starred assignment always collects the remaining items into a list, even from a tuple.",
            verify="stdout"),
    _choice("PO-08", PREDICT, "What does this code print?",
            'd = {"a": 1, "b": 2}\nd["a"] += d.get("c", 10)\nprint(d["a"])',
            ["1", "KeyError", "12", "11"], "D",
            'd.get("c", 10) returns the default 10 because "c" is missing, so d["a"] becomes 1 + 10.',
            verify="stdout"),
    _choice("PO-09", PREDICT, "What does this code print?",
            'print(bool([]), bool("0"), bool(0.0))',
            ["False False False", "False True False", "True True False", "False True True"], "B",
            'Empty containers and zero are falsy. "0" is a non-empty string, so it is truthy.',
            verify="stdout"),

    # ------------------------------------------------------------------ Programming Logic
    _choice("PL-01", LOGIC, 'How many times is "Hi" printed?',
            'for i in range(1, 10, 3):\n    print("Hi")',
            ["3", "4", "9", "10"], "A",
            "range(1, 10, 3) produces 1, 4 and 7, so the loop body runs three times.",
            verify="lines"),
    _text("PL-02", LOGIC, "What number does this code print?",
          "count = 0\nfor i in range(5):\n    for j in range(i):\n        count += 1\nprint(count)",
          "10",
          "The inner loop runs 0 + 1 + 2 + 3 + 4 = 10 times in total.",
          verify="stdout"),
    _choice("PL-03", LOGIC, "Which condition is True exactly when n is a positive even number?", None,
            ["n % 2 == 0 or n > 0", "n % 2 and n > 0", "n % 2 == 0 and n > 0", "not n % 2 or n >= 0"], "C",
            "Both conditions must hold, so we need 'and'. n % 2 == 0 tests evenness; n > 0 tests positivity."),
    _choice("PL-04", LOGIC, "What does this code print?",
            "def mystery(n):\n    if n <= 1:\n        return 1\n    return n * mystery(n - 2)\n\nprint(mystery(5))",
            ["120", "5", "8", "15"], "D",
            "mystery(5) = 5 * mystery(3) = 5 * 3 * mystery(1) = 5 * 3 * 1 = 15.",
            verify="stdout"),
    _choice("PL-05", LOGIC, "What is the final value of x?",
            "x = 5\nx = x * 2 + 3\nx //= 2\nprint(x)",
            ["6", "6.5", "7", "13"], "A",
            "5 * 2 + 3 = 13, then 13 // 2 = 6 (integer floor division).",
            verify="stdout"),
    _choice("PL-06", LOGIC, "What does this code print?",
            'def check():\n    print("checked")\n    return True\n\nresult = False and check()\nprint(result)',
            ["checked, then False", "False", "checked, then True", "True"], "B",
            "'and' short-circuits: the left side is False, so check() is never called.",
            verify="stdout"),
    _choice("PL-07", LOGIC, "What does this code print?",
            "n = 100\nsteps = 0\nwhile n > 1:\n    n //= 2\n    steps += 1\nprint(steps)",
            ["5", "7", "6", "50"], "C",
            "100 -> 50 -> 25 -> 12 -> 6 -> 3 -> 1: six halvings.",
            verify="stdout"),
    _choice("PL-08", LOGIC, "Which loop runs exactly 5 times?", None,
            ["for i in range(1, 5):", "for i in range(0, 10, 2):", "for i in range(5, 0):", "for i in range(1, 10, 3):"],
            "B",
            "range(0, 10, 2) is 0, 2, 4, 6, 8. range(1, 5) has 4 values, range(5, 0) is empty, range(1, 10, 3) has 3."),

    # ------------------------------------------------------------------ Algorithms & Complexity
    _choice("AC-01", ALGO, "What is the worst-case time complexity of binary search on a sorted list of n items?", None,
            ["O(n)", "O(log n)", "O(n log n)", "O(1)"], "B",
            "Each comparison halves the remaining search space."),
    _choice("AC-02", ALGO, "What is the time complexity of this code?",
            "total = 0\nfor i in range(n):\n    for j in range(i, n):\n        total += 1",
            ["O(n)", "O(n log n)", "O(n²)", "O(2ⁿ)"], "C",
            "The inner loop runs n + (n-1) + ... + 1 = n(n+1)/2 times, which grows like n²."),
    _choice("AC-03", ALGO, "Which data structure gives O(1) average-time lookup by key?", None,
            ["Python list", "Sorted array with binary search", "Linked list", "Hash table (Python dict)"], "D",
            "A hash table computes the bucket from the key directly. Lists and linked lists need O(n); binary search O(log n)."),
    _choice("AC-04", ALGO, "What is the worst-case time complexity of quicksort?", None,
            ["O(n²)", "O(n log n)", "O(n)", "O(log n)"], "A",
            "With consistently bad pivots (e.g. already sorted input and first-element pivot) every partition is lopsided."),
    _choice("AC-05", ALGO, "Breadth-first search (BFS) visits nodes using which data structure?", None,
            ["Stack", "Queue", "Priority queue", "Only a hash set"], "B",
            "BFS processes nodes in first-in, first-out order, level by level. A stack would give depth-first search."),
    _choice("AC-06", ALGO, "At most how many comparisons does binary search need on 1,000 sorted items?", None,
            ["1,000", "500", "100", "10"], "D",
            "2^10 = 1024 ≥ 1000, so about log2(1000) ≈ 10 comparisons are enough."),
    _choice("AC-07", ALGO, "Which sorting algorithm guarantees O(n log n) time in the worst case?", None,
            ["Quicksort", "Bubble sort", "Merge sort", "Insertion sort"], "C",
            "Merge sort always splits in half and merges in linear time. Quicksort can degrade to O(n²)."),
    _choice("AC-08", ALGO, "What is the amortised time complexity of list.append() in Python?", None,
            ["O(1)", "O(log n)", "O(n)", "O(n²)"], "A",
            "The list over-allocates capacity, so occasional O(n) resizes average out to O(1) per append."),
    _text("AC-09", ALGO, "A tree has 10 nodes. How many edges does it have? (type a number)", None,
          "9",
          "Every tree with n nodes has exactly n - 1 edges."),

    # ------------------------------------------------------------------ Debugging & Code Analysis
    _choice("DB-01", DEBUG, "What happens when this code runs?",
            "def average(nums):\n    total = 0\n    for n in nums:\n        total += n\n    return total / len(nums)\n\n"
            "print(average([]))",
            ["It prints 0", "It prints None", "It raises ZeroDivisionError", "It raises TypeError"], "C",
            "len([]) is 0, so total / 0 raises ZeroDivisionError. The function should handle empty input.",
            verify="exception:ZeroDivisionError"),
    _choice("DB-02", DEBUG, "For which input does find_max return a WRONG result?",
            "def find_max(nums):\n    best = 0\n    for n in nums:\n        if n > best:\n            best = n\n    return best",
            ["[3, 7, 2]", "[-5, -2, -9]", "[0, 0, 1]", "[10]"], "B",
            "best starts at 0, so for an all-negative list it returns 0. Fix: start with best = nums[0]."),
    _choice("DB-03", DEBUG, "This code tries to remove every even number. What does it actually print?",
            "items = [2, 4, 6, 7]\nfor x in items:\n    if x % 2 == 0:\n        items.remove(x)\nprint(items)",
            ["[7]", "[2, 4, 6, 7]", "[4, 7]", "RuntimeError"], "C",
            "Removing items while iterating shifts the list, so the loop skips 4. Iterate over a copy instead.",
            verify="stdout"),
    _choice("DB-04", DEBUG, "What happens when this code runs?",
            "count = 0\n\ndef increment():\n    count += 1\n\nincrement()",
            ["count becomes 1", "UnboundLocalError is raised", "SyntaxError", "Nothing happens; count stays 0"], "B",
            "Assigning to count inside the function makes it local, but it is read before being assigned. Use 'global count'.",
            verify="exception:UnboundLocalError"),
    _choice("DB-05", DEBUG, "last_item([1, 2, 3]) crashes. Which return statement fixes it?",
            "def last_item(lst):\n    return lst[len(lst)]",
            ["return lst[len(lst) + 1]", "return lst[0]", "return lst[len(lst) // 2]", "return lst[-1]"], "D",
            "Valid indexes go from 0 to len(lst) - 1, so lst[len(lst)] is out of range. lst[-1] is the last item."),
    _choice("DB-06", DEBUG, "What happens when this code runs?",
            's = "hello"\ns[0] = "H"',
            ["s becomes 'Hello'", "IndexError", "TypeError: strings are immutable", "SyntaxError"], "C",
            'Strings cannot be changed in place. Build a new one instead: s = "H" + s[1:].',
            verify="exception:TypeError"),
    _choice("DB-07", DEBUG, "What does this code print?",
            "print(0.1 + 0.2 == 0.3)",
            ["True", "False", "An error is raised", "None"], "B",
            "0.1 + 0.2 is 0.30000000000000004 in binary floating point. Compare floats with math.isclose().",
            verify="stdout"),
    _choice("DB-08", DEBUG, "What does this code print?",
            "a = [1, 2]\nb = [1, 2]\nprint(a == b, a is b)",
            ["True False", "True True", "False False", "False True"], "A",
            "== compares values (equal); 'is' compares identity (two different list objects)."),
    _text("DB-09", DEBUG, "Which exception does this code raise? Type its name.",
          'def greet(name):\n    print("Hello, " + name)\n\ngreet(42)',
          "TypeError",
          "You cannot concatenate str and int with +. Use f\"Hello, {name}\" or str(name).",
          verify="exception:TypeError"),
)

QUESTIONS_BY_ID = {q.qid: q for q in QUESTIONS}


def pick_questions(total_rounds: int, rng: random.Random | None = None) -> list[Question]:
    """Choose questions for one game, balanced across categories and in a random order.

    With 8 rounds every category appears twice. No question repeats within a game.
    """
    rng = rng or random.Random()
    pools = {cat: [q for q in QUESTIONS if q.category == cat] for cat in CATEGORIES}
    for pool in pools.values():
        rng.shuffle(pool)
    order = list(CATEGORIES)
    rng.shuffle(order)
    picked: list[Question] = []
    i = 0
    while len(picked) < total_rounds and any(pools.values()):
        pool = pools[order[i % len(order)]]
        if pool:
            picked.append(pool.pop())
        i += 1
    return picked
