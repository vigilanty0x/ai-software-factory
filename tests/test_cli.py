from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

from ai_software_factory.cli import main


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def invoke(self, arguments: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(arguments)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_full_command_workflow(self):
        code, output, error = self.invoke(["init", str(self.root)])
        self.assertEqual((code, error), (0, ""))
        spec = self.root / "factory.json"
        self.assertTrue(spec.is_file())

        code, output, _ = self.invoke(["validate", str(spec)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["task_count"], 2)

        code, output, _ = self.invoke(
            ["plan", str(spec), "--idempotency-key", "cli-e2e"]
        )
        self.assertEqual(code, 0)
        plan = json.loads(output)
        run_id = plan["run_id"]
        database = Path(plan["database"])

        code, output, error = self.invoke(
            ["run", str(spec), "--idempotency-key", "cli-e2e"]
        )
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(json.loads(output)["state"], "succeeded")

        code, output, _ = self.invoke(
            ["status", "--db", str(database), "--run-id", run_id]
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["receipt_count"], 2)

        code, output, _ = self.invoke(
            ["replay", "--db", str(database), "--run-id", run_id]
        )
        self.assertEqual(code, 0)
        self.assertGreater(len(json.loads(output)["events"]), 5)

        exported = self.root / "evidence.json"
        code, output, _ = self.invoke(
            [
                "export",
                "--db",
                str(database),
                "--run-id",
                run_id,
                "--output",
                str(exported),
            ]
        )
        self.assertEqual((code, output), (0, ""))
        self.assertEqual(json.loads(exported.read_text())["status"]["state"], "succeeded")

        code, output, _ = self.invoke(["verify", str(exported)])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(output)["valid"])

        tampered = json.loads(exported.read_text())
        tampered["status"]["state"] = "failed"
        exported.write_text(json.dumps(tampered), encoding="utf-8")
        code, output, _ = self.invoke(["verify", str(exported)])
        self.assertEqual(code, 2)
        self.assertFalse(json.loads(output)["valid"])

        tampered = json.loads(exported.read_text())
        tampered["unknown"] = True
        material = {key: value for key, value in tampered.items() if key != "export_sha256"}
        from ai_software_factory.evidence import digest_json

        tampered["export_sha256"] = digest_json(material)
        exported.write_text(json.dumps(tampered), encoding="utf-8")
        code, output, _ = self.invoke(["verify", str(exported)])
        self.assertEqual(code, 2)
        self.assertTrue(
            any(
                "invalid top-level field set" in issue
                for issue in json.loads(output)["issues"]
            )
        )

    def test_init_refuses_overwrite(self):
        self.invoke(["init", str(self.root)])
        code, _, error = self.invoke(["init", str(self.root)])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(error)["error"], "FileExistsError")

    def test_invalid_spec_returns_structured_error(self):
        invalid = self.root / "invalid.json"
        invalid.write_text('{"schema_version":1,"name":"x","tasks":[]}', encoding="utf-8")
        code, output, error = self.invoke(["validate", str(invalid)])
        self.assertEqual((code, output), (2, ""))
        self.assertEqual(json.loads(error)["error"], "SpecError")

    def test_missing_read_database_is_not_created(self):
        database = self.root / "missing.sqlite3"
        code, _, error = self.invoke(
            ["status", "--db", str(database), "--run-id", "missing"]
        )
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(error)["error"], "FileNotFoundError")
        self.assertFalse(database.exists())

    def test_kill_command_cancels_planned_run(self):
        self.invoke(["init", str(self.root)])
        spec = self.root / "factory.json"
        _, output, _ = self.invoke(
            ["plan", str(spec), "--idempotency-key", "kill-me"]
        )
        plan = json.loads(output)
        code, output, _ = self.invoke(
            [
                "kill",
                "--db",
                plan["database"],
                "--run-id",
                plan["run_id"],
                "--reason",
                "operator test",
            ]
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["state"], "cancelled")

    def test_legacy_cli_remains_compatible(self):
        record = self.root / "record.json"
        record.write_text(
            json.dumps(
                {
                    "mission": "release",
                    "owner": "agent",
                    "tests_passed": 2,
                    "tests_total": 2,
                }
            ),
            encoding="utf-8",
        )
        code, output, _ = self.invoke([str(record)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["status"], "passed")

    def test_legacy_non_object_fails_cleanly(self):
        record = self.root / "record.json"
        record.write_text("null", encoding="utf-8")
        code, _, error = self.invoke([str(record)])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(error)["error"], "SpecError")


if __name__ == "__main__":
    unittest.main()
