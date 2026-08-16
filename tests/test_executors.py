from __future__ import annotations

from hashlib import sha256
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from ai_software_factory.executors import (
    DeterministicMockExecutor,
    ExecutionRequest,
    SubprocessExecutor,
)

from tests.support import result


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name).resolve()
        self.executor = SubprocessExecutor(self.workspace)

    def request(
        self,
        code: str,
        *,
        timeout: float = 2,
        cap: int = 4096,
        cwd: Path | None = None,
    ) -> ExecutionRequest:
        return ExecutionRequest(
            label="synthetic",
            argv=(sys.executable, "-c", code),
            cwd=cwd or self.workspace,
            timeout_seconds=timeout,
            max_output_bytes=cap,
        )

    def test_success_captures_both_streams(self):
        actual = self.executor.execute(
            self.request("import sys; print('out'); print('err', file=sys.stderr)")
        )
        self.assertTrue(actual.succeeded)
        self.assertEqual(actual.stdout, f"out{os.linesep}".encode())
        self.assertEqual(actual.stderr, f"err{os.linesep}".encode())

    def test_nonzero_exit_is_failure(self):
        actual = self.executor.execute(self.request("raise SystemExit(7)"))
        self.assertFalse(actual.succeeded)
        self.assertEqual(actual.exit_code, 7)

    def test_combined_output_is_capped_but_full_streams_are_hashed(self):
        actual = self.executor.execute(
            self.request(
                "import os; os.write(1,b'a'*10000); os.write(2,b'b'*10000)", cap=257
            )
        )
        self.assertLessEqual(len(actual.stdout) + len(actual.stderr), 257)
        self.assertEqual(actual.stdout_bytes_seen, 10_000)
        self.assertEqual(actual.stderr_bytes_seen, 10_000)
        self.assertTrue(actual.output_truncated)
        self.assertEqual(actual.stdout_sha256, "sha256:" + sha256(b"a" * 10_000).hexdigest())
        self.assertEqual(actual.stderr_sha256, "sha256:" + sha256(b"b" * 10_000).hexdigest())

    def test_timeout_kills_process(self):
        started = time.monotonic()
        actual = self.executor.execute(self.request("import time; time.sleep(5)", timeout=0.1))
        self.assertTrue(actual.timed_out)
        self.assertLess(time.monotonic() - started, 2)

    @unittest.skipUnless(os.name == "posix", "POSIX process-group behavior")
    def test_timeout_kills_descendants_holding_output_pipes(self):
        code = (
            "import subprocess,sys,time; "
            "subprocess.Popen([sys.executable,'-c','import time; time.sleep(5)']); "
            "time.sleep(5)"
        )
        started = time.monotonic()
        actual = self.executor.execute(self.request(code, timeout=0.1))
        self.assertTrue(actual.timed_out)
        self.assertLess(time.monotonic() - started, 2)

    @unittest.skipUnless(os.name == "posix", "POSIX process-group behavior")
    def test_successful_parent_cannot_leave_descendant_past_deadline(self):
        code = (
            "import subprocess,sys; "
            "subprocess.Popen([sys.executable,'-c','import time; time.sleep(5)'],"
            "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)"
        )
        started = time.monotonic()
        actual = self.executor.execute(self.request(code, timeout=0.1))
        self.assertTrue(actual.timed_out)
        self.assertFalse(actual.succeeded)
        self.assertLess(time.monotonic() - started, 2)

    def test_cwd_escape_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "escapes"):
            self.executor.execute(self.request("pass", cwd=self.workspace.parent))

    def test_nul_in_argv_is_rejected(self):
        request = ExecutionRequest(
            "bad", ("python", "bad\0arg"), self.workspace, 1, 100
        )
        with self.assertRaisesRegex(ValueError, "NUL"):
            self.executor.execute(request)

    def test_parent_secret_environment_is_not_inherited(self):
        with patch.dict(os.environ, {"SUPER_SECRET_VALUE": "do-not-inherit"}):
            actual = self.executor.execute(
                self.request(
                    "import os; raise SystemExit(9 if 'SUPER_SECRET_VALUE' in os.environ else 0)"
                )
            )
        self.assertTrue(actual.succeeded)

    def test_deterministic_mock_consumes_script_then_defaults(self):
        mock = DeterministicMockExecutor({"synthetic": [result(3)]})
        request = self.request("pass")
        self.assertEqual(mock.execute(request).exit_code, 3)
        self.assertEqual(mock.execute(request).exit_code, 0)
        self.assertEqual(mock.execute(request).stdout, b"mock:synthetic:2")


if __name__ == "__main__":
    unittest.main()
