import unittest
from ai_software_factory import evaluate, verify_evidence

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

    def test_mission_must_be_nonblank_string(self):
        for value in ("", "   ", 3, None):
            record = dict(GOOD)
            record["mission"] = value
            with self.subTest(value=value):
                self.assertEqual(evaluate(record)["status"], "failed")

    def test_booleans_are_not_test_counts(self):
        record = dict(GOOD, tests_total=True, tests_passed=True)
        self.assertEqual(evaluate(record)["status"], "failed")

    def test_evidence_does_not_alias_mutable_input(self):
        record = dict(GOOD)
        evidence = evaluate(record)
        record["tests_passed"] = 0
        self.assertEqual(evidence["record"]["tests_passed"], 20)
        self.assertTrue(verify_evidence(evidence))

    def test_evidence_tampering_is_detected(self):
        evidence = evaluate(GOOD)
        evidence["record"]["owner"] = "attacker"
        self.assertFalse(verify_evidence(evidence))

    def test_non_object_inputs_fail_closed(self):
        for value in (None, 42, [], ["mission", "owner", "tests_passed", "tests_total"]):
            with self.subTest(value=value):
                self.assertEqual(evaluate(value)["status"], "blocked")  # type: ignore[arg-type]

if __name__ == "__main__":
    unittest.main()
