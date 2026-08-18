import hashlib
import json
import unittest

from ai_software_factory_starter_kit import evaluate

ARTIFACT = "a" * 64
TESTS = "b" * 64
REVIEW = {"reviewer": "independent-reviewer", "subject_sha256": ARTIFACT, "decision": "approved", "issued_at": "2026-08-15T10:00:00Z"}
REVIEW_DIGEST = hashlib.sha256(json.dumps(REVIEW, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
GOOD = {
    "spec": "spec.md",
    "ownership": [{"agent": "builder", "worktree": "agent/build"}, {"agent": "tester", "worktree": "agent/test"}],
    "tests": {"passed": 20, "total": 20, "sha256": TESTS},
    "evidence": [{"kind": "artifact", "sha256": ARTIFACT, "issuer": "build-job", "issued_at": "2026-08-15T09:00:00Z"}, {"kind": "test", "sha256": TESTS, "issuer": "test-job", "issued_at": "2026-08-15T09:30:00Z"}],
    "review": REVIEW,
    "release": {"artifact_sha256": ARTIFACT, "review_sha256": REVIEW_DIGEST, "issuer": "release-job", "issued_at": "2026-08-15T11:00:00Z"},
}


class ContractTests(unittest.TestCase):
    def test_structured_factory_manifest_is_consistent_but_declared(self):
        result = evaluate(GOOD)
        self.assertEqual(result["status"], "passed")
        self.assertIn("independent-review", result["factory_manifest"]["stages"])
        self.assertFalse(result["factory_manifest"]["independently_verified_by_tool"])

    def test_agent_and_worktree_ownership_must_be_unique(self):
        duplicate = [{"agent": "builder", "worktree": "one"}, {"agent": "builder", "worktree": "two"}]
        self.assertEqual(evaluate({**GOOD, "ownership": duplicate})["status"], "failed")
        duplicate = [{"agent": "builder", "worktree": "one"}, {"agent": "tester", "worktree": "one"}]
        self.assertEqual(evaluate({**GOOD, "ownership": duplicate})["status"], "failed")

    def test_trust_me_evidence_fails(self):
        evidence = [{**GOOD["evidence"][0], "sha256": "trust me"}, GOOD["evidence"][1]]
        self.assertEqual(evaluate({**GOOD, "evidence": evidence})["status"], "failed")

    def test_reviewer_must_be_independent(self):
        self.assertEqual(evaluate({**GOOD, "review": {**REVIEW, "reviewer": "builder"}})["status"], "failed")

    def test_review_and_release_must_bind_artifact(self):
        self.assertEqual(evaluate({**GOOD, "review": {**REVIEW, "subject_sha256": "c" * 64}})["status"], "failed")
        self.assertEqual(evaluate({**GOOD, "release": {**GOOD["release"], "review_sha256": "c" * 64}})["status"], "failed")

    def test_tests_must_all_pass_and_match_evidence(self):
        self.assertEqual(evaluate({**GOOD, "tests": {**GOOD["tests"], "passed": 19}})["status"], "failed")
        self.assertEqual(evaluate({**GOOD, "tests": {**GOOD["tests"], "sha256": "c" * 64}})["status"], "failed")

    def test_non_object_and_missing_field_fail_closed(self):
        self.assertEqual(evaluate([])["status"], "failed")
        self.assertEqual(evaluate({})["status"], "blocked")


if __name__ == "__main__":
    unittest.main()
