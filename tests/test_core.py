import unittest
from ai_software_factory import evaluate

GOOD = {"mission":"release","owner":"agent-1","tests_passed":20,"tests_total":20}
BAD = {"mission":"release","owner":"agent-1","tests_passed":19,"tests_total":20}

class CoreTests(unittest.TestCase):
    def test_good_record_passes_deterministically(self):
        first = evaluate(GOOD)
        self.assertEqual(first["status"], "passed")
        self.assertEqual(first, evaluate(dict(reversed(list(GOOD.items())))))
        self.assertEqual(len(first["evidence_sha256"]), 64)

    def test_bad_record_fails(self):
        self.assertEqual(evaluate(BAD)["status"], "failed")

    def test_missing_field_blocks(self):
        incomplete = dict(GOOD)
        incomplete.pop(next(iter(incomplete)))
        self.assertEqual(evaluate(incomplete)["status"], "blocked")

if __name__ == "__main__":
    unittest.main()

