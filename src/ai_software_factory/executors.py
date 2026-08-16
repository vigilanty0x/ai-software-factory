"""Provider and executor boundaries for local task execution."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import os
from pathlib import Path
import signal
import subprocess
import threading
import time
from typing import Mapping, Protocol, Sequence

from .models import FactorySpec, TaskSpec, TestSpec


@dataclass(frozen=True, slots=True)
class ExecutionRequest:
    label: str
    argv: tuple[str, ...]
    cwd: Path
    timeout_seconds: float
    max_output_bytes: int
    environment: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    exit_code: int | None
    stdout: bytes
    stderr: bytes
    stdout_bytes_seen: int
    stderr_bytes_seen: int
    stdout_sha256: str
    stderr_sha256: str
    output_truncated: bool
    timed_out: bool
    duration_seconds: float
    executor: str

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


class Provider(Protocol):
    """Converts validated tasks into execution requests."""

    def task_request(
        self, spec: FactorySpec, task: TaskSpec, workspace: Path
    ) -> ExecutionRequest: ...

    def test_request(
        self, spec: FactorySpec, task: TaskSpec, test: TestSpec, workspace: Path
    ) -> ExecutionRequest: ...


class Executor(Protocol):
    """Executes an already policy-checked request."""

    name: str

    def execute(self, request: ExecutionRequest) -> ExecutionResult: ...


class SpecProvider:
    """Local provider that uses only values from a validated specification."""

    def task_request(
        self, spec: FactorySpec, task: TaskSpec, workspace: Path
    ) -> ExecutionRequest:
        return ExecutionRequest(
            label=task.id,
            argv=task.command,
            cwd=workspace,
            timeout_seconds=(
                task.timeout_seconds or spec.budget.default_task_timeout_seconds
            ),
            max_output_bytes=spec.budget.max_output_bytes,
            environment=task.environment,
        )

    def test_request(
        self, spec: FactorySpec, task: TaskSpec, test: TestSpec, workspace: Path
    ) -> ExecutionRequest:
        return ExecutionRequest(
            label=f"{task.id}:test:{test.name}",
            argv=test.command,
            cwd=workspace,
            timeout_seconds=(
                test.timeout_seconds
                or task.timeout_seconds
                or spec.budget.default_task_timeout_seconds
            ),
            max_output_bytes=spec.budget.max_output_bytes,
            environment=task.environment,
        )


class _OutputBudget:
    def __init__(self, maximum: int):
        self.maximum = maximum
        self.remaining = maximum
        self.seen = {"stdout": 0, "stderr": 0}
        self.digests = {"stdout": sha256(), "stderr": sha256()}
        self.truncated = False
        self.lock = threading.Lock()

    def accept(self, stream: str, chunk: bytes) -> bytes:
        with self.lock:
            self.seen[stream] += len(chunk)
            self.digests[stream].update(chunk)
            accepted = chunk[: self.remaining]
            self.remaining -= len(accepted)
            if len(accepted) != len(chunk):
                self.truncated = True
            return accepted


def _drain(
    pipe: object,
    stream: str,
    budget: _OutputBudget,
    destination: bytearray,
) -> None:
    try:
        while True:
            chunk = pipe.read(65_536)  # type: ignore[attr-defined]
            if not chunk:
                break
            destination.extend(budget.accept(stream, chunk))
    finally:
        pipe.close()  # type: ignore[attr-defined]


def _minimal_environment(overrides: Sequence[tuple[str, str]]) -> dict[str, str]:
    result = {
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
        "PATH": os.environ.get("PATH", os.defpath),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    # Windows needs these for process creation and temporary files.
    for name in ("SYSTEMROOT", "TEMP", "TMP"):
        if name in os.environ:
            result[name] = os.environ[name]
    result.update(dict(overrides))
    return result


class SubprocessExecutor:
    """Bounded subprocess execution without a shell.

    This is a process-safety boundary, not an OS sandbox.  Specifications are
    trusted local input and can request any executable the current user may run.
    """

    name = "subprocess-v1"

    def __init__(self, workspace_root: str | Path):
        self.workspace_root = Path(workspace_root).resolve()

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        if not request.argv or any(not isinstance(arg, str) or "\0" in arg for arg in request.argv):
            raise ValueError("argv must be a non-empty NUL-free sequence of strings")
        if request.timeout_seconds <= 0:
            raise ValueError("timeout must be positive")
        if request.max_output_bytes <= 0:
            raise ValueError("output cap must be positive")
        cwd = request.cwd.resolve()
        try:
            cwd.relative_to(self.workspace_root)
        except ValueError as exc:
            raise ValueError("execution cwd escapes the configured workspace") from exc
        cwd.mkdir(parents=True, exist_ok=True)

        started = time.monotonic()
        process = subprocess.Popen(
            list(request.argv),
            cwd=cwd,
            env=_minimal_environment(request.environment),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            start_new_session=(os.name == "posix"),
            creationflags=(
                subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            ),
        )
        assert process.stdout is not None and process.stderr is not None
        budget = _OutputBudget(request.max_output_bytes)
        stdout = bytearray()
        stderr = bytearray()
        threads = [
            threading.Thread(
                target=_drain,
                args=(process.stdout, "stdout", budget, stdout),
                daemon=True,
            ),
            threading.Thread(
                target=_drain,
                args=(process.stderr, "stderr", budget, stderr),
                daemon=True,
            ),
        ]
        for thread in threads:
            thread.start()
        deadline = started + request.timeout_seconds
        timed_out = False
        try:
            process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            timed_out = True
        else:
            # A direct parent can exit successfully while descendants retain
            # its output handles.  The request owns the whole process group,
            # so collection must also finish before the request deadline.
            while any(thread.is_alive() for thread in threads):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                for thread in threads:
                    thread.join(timeout=min(0.02, remaining))
            if not timed_out and os.name == "posix":
                try:
                    os.killpg(process.pid, 0)
                except ProcessLookupError:
                    pass
                else:
                    # The group leader is already reaped, so a live process
                    # group here consists of descendants that would escape the
                    # attempt lifetime even if they closed inherited pipes.
                    timed_out = True
        if timed_out:
            if os.name == "posix":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            elif process.poll() is None:  # pragma: no cover - Windows CI/users
                process.kill()
            if process.poll() is None:
                process.wait()
        cleanup_deadline = time.monotonic() + 1.0
        while any(thread.is_alive() for thread in threads):
            remaining = cleanup_deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("subprocess output collectors did not terminate")
            for thread in threads:
                thread.join(timeout=min(0.02, remaining))
        return ExecutionResult(
            exit_code=process.returncode,
            stdout=bytes(stdout),
            stderr=bytes(stderr),
            stdout_bytes_seen=budget.seen["stdout"],
            stderr_bytes_seen=budget.seen["stderr"],
            stdout_sha256="sha256:" + budget.digests["stdout"].hexdigest(),
            stderr_sha256="sha256:" + budget.digests["stderr"].hexdigest(),
            output_truncated=budget.truncated,
            timed_out=timed_out,
            duration_seconds=max(0.0, time.monotonic() - started),
            executor=self.name,
        )


class DeterministicMockExecutor:
    """Deterministic executor for examples and orchestration tests."""

    name = "deterministic-mock-v1"

    def __init__(
        self,
        scripted: Mapping[str, Sequence[ExecutionResult]] | None = None,
        *,
        default_exit_code: int = 0,
    ):
        self._scripted = {key: list(results) for key, results in (scripted or {}).items()}
        self._offsets: dict[str, int] = {}
        self._default_exit_code = default_exit_code
        self._lock = threading.Lock()

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        with self._lock:
            offset = self._offsets.get(request.label, 0)
            choices = self._scripted.get(request.label, [])
            self._offsets[request.label] = offset + 1
        if offset < len(choices):
            return choices[offset]
        output = f"mock:{request.label}:{offset}".encode("utf-8")
        output = output[: request.max_output_bytes]
        return ExecutionResult(
            exit_code=self._default_exit_code,
            stdout=output,
            stderr=b"",
            stdout_bytes_seen=len(output),
            stderr_bytes_seen=0,
            stdout_sha256="sha256:" + sha256(output).hexdigest(),
            stderr_sha256="sha256:" + sha256(b"").hexdigest(),
            output_truncated=False,
            timed_out=False,
            duration_seconds=0.0,
            executor=self.name,
        )


def command_digest(argv: Sequence[str]) -> str:
    rendered = "\0".join(argv).encode("utf-8")
    return "sha256:" + sha256(rendered).hexdigest()
