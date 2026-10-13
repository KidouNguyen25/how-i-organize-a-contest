import json
import tempfile
import unittest
from pathlib import Path

from judge.problems import ProblemSet
from judge.scoring import output_tokens, summarize_cases


ROOT = Path(__file__).resolve().parents[1]


class ProblemAndScoringTests(unittest.TestCase):
    def test_bundled_problem_manifests_load_and_sum_to_100(self):
        problems = ProblemSet(ROOT / "problems")
        self.assertEqual(len(problems.list_public()), 2)
        for public_problem in problems.list_public():
            problem = problems.get(public_problem["id"])
            self.assertIsNotNone(problem)
            self.assertEqual(sum(test.points for test in problem.tests), 100)

    def test_problem_weights_must_sum_to_100(self):
        manifest = {
            "id": "bad",
            "title": "Bad",
            "statement": "Bad task",
            "sample_input": "",
            "sample_output": "",
            "tests": [{"input": "", "output": "", "points": 99}],
        }
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "bad.json").write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "sum to 100"):
                ProblemSet(directory)

    def test_output_comparison_uses_whitespace_tokens(self):
        self.assertEqual(output_tokens(b"  12\n34 \t"), output_tokens(b"12 34\n"))

    def test_score_and_verdict_summaries(self):
        self.assertEqual(summarize_cases([{"points": 100, "verdict": "AC"}]), ("AC", 100))
        self.assertEqual(
            summarize_cases(
                [{"points": 40, "verdict": "AC"}, {"points": 60, "verdict": "WA"}]
            ),
            ("PARTIAL", 40),
        )
        self.assertEqual(summarize_cases([{"points": 100, "verdict": "WA"}]), ("WA", 0))
        self.assertEqual(summarize_cases([{"points": 100, "verdict": "TLE"}]), ("TLE", 0))
        self.assertEqual(summarize_cases([{"points": 100, "verdict": "RE"}]), ("RE", 0))


if __name__ == "__main__":
    unittest.main()

