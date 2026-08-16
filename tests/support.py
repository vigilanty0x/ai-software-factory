from __future__ import annotations

from copy import deepcopy
from hashlib import sha256

from ai_software_factory.executors import ExecutionResult


def task(task_id: str = "build", **changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "id": task_id,
        "owner": "agent-one",
        "command": ["python", "-c", "pass"],
    }
    value.update(changes)
    return value


def spec(*tasks: dict[str, object], **changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": 1,
        "name": "synthetic-factory",
        "workspace": "workspace",
        "budget": {
            "max_tasks": 100,
            "max_attempts": 100,
            "max_wall_seconds": 60,
            "max_output_bytes": 4096,
            "default_task_timeout_seconds": 2,
            "lease_seconds": 10,
            "retry_base_seconds": 0,
            "retry_cap_seconds": 0,
            "default_max_attempts": 2,
        },
        "tasks": list(tasks or (task(),)),
    }
    value.update(changes)
    return deepcopy(value)


def result(
    exit_code: int = 0,
    *,
    stdout: bytes = b"ok",
    stderr: bytes = b"",
    timed_out: bool = False,
    executor: str = "test",
) -> ExecutionResult:
    return ExecutionResult(
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        stdout_bytes_seen=len(stdout),
        stderr_bytes_seen=len(stderr),
        stdout_sha256="sha256:" + sha256(stdout).hexdigest(),
        stderr_sha256="sha256:" + sha256(stderr).hexdigest(),
        output_truncated=False,
        timed_out=timed_out,
        duration_seconds=0.0,
        executor=executor,
    )


class ManualClock:
    def __init__(self, value: float = 1_000.0):
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds

