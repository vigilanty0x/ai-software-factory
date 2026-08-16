"""Content-addressed evidence receipts."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Iterable

from .executors import ExecutionRequest, ExecutionResult, command_digest


RECEIPT_FORMAT = "ai-software-factory/receipt-v1"


def canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def digest_json(value: object) -> str:
    return sha256(canonical_json(value).encode("utf-8")).hexdigest()


def hash_file(path: Path) -> tuple[str, int]:
    digest = sha256()
    size = 0
    with path.open("rb") as stream:
        before = path.stat()
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
        after = path.stat()
    if (
        before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or getattr(before, "st_ino", None) != getattr(after, "st_ino", None)
    ):
        raise OSError(f"file changed while hashing: {path}")
    return "sha256:" + digest.hexdigest(), size


def artifact_manifest(workspace: Path, relative_paths: Iterable[str]) -> list[dict[str, Any]]:
    root = workspace.resolve()
    result: list[dict[str, Any]] = []
    for relative in sorted(relative_paths):
        candidate = root / relative
        if candidate.is_symlink():
            result.append({"path": relative, "exists": True, "kind": "symlink"})
            continue
        candidate = candidate.resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"artifact path escapes workspace: {relative!r}") from exc
        if not candidate.exists():
            result.append({"path": relative, "exists": False})
        elif not candidate.is_file():
            result.append({"path": relative, "exists": True, "kind": "non-file"})
        else:
            digest, size = hash_file(candidate)
            result.append(
                {
                    "path": relative,
                    "exists": True,
                    "kind": "file",
                    "size": size,
                    "sha256": digest,
                }
            )
    return result


def workspace_snapshot(workspace: Path) -> dict[str, dict[str, Any]]:
    """Hash regular files and record symlinks without following them."""

    root = workspace.resolve()
    if not root.exists():
        return {}
    result: dict[str, dict[str, Any]] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            result[relative] = {"kind": "symlink", "target": str(path.readlink())}
        elif path.is_file():
            digest, size = hash_file(path)
            result[relative] = {"kind": "file", "size": size, "sha256": digest}
    return result


def ownership_report(
    before: dict[str, dict[str, Any]],
    after: dict[str, dict[str, Any]],
    owned_paths: Iterable[str],
) -> dict[str, Any]:
    changed = sorted(
        path for path in set(before) | set(after) if before.get(path) != after.get(path)
    )
    owners = [tuple(Path(path).parts) for path in owned_paths]

    def allowed(path: str) -> bool:
        parts = tuple(Path(path).parts)
        return any(parts[: len(owner)] == owner for owner in owners)

    violations = [path for path in changed if not allowed(path)]
    unsafe = [path for path in changed if after.get(path, {}).get("kind") == "symlink"]
    violations = sorted(set(violations + unsafe))
    return {"changed_paths": changed, "violations": violations, "unsafe_paths": unsafe}


def summarize_result(request: ExecutionRequest, result: ExecutionResult) -> dict[str, Any]:
    return {
        "label": request.label,
        "command_digest": command_digest(request.argv),
        "executor": result.executor,
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "duration_seconds": round(result.duration_seconds, 6),
        "output_truncated": result.output_truncated,
        "stdout": {
            "bytes_seen": result.stdout_bytes_seen,
            "captured_bytes": len(result.stdout),
            "sha256": result.stdout_sha256,
        },
        "stderr": {
            "bytes_seen": result.stderr_bytes_seen,
            "captured_bytes": len(result.stderr),
            "sha256": result.stderr_sha256,
        },
    }


def build_receipt(
    *,
    run_id: str,
    task_id: str,
    attempt: int,
    started_at: float,
    finished_at: float,
    request: ExecutionRequest,
    result: ExecutionResult,
    tests: list[tuple[str, ExecutionRequest, ExecutionResult]],
    artifacts: list[dict[str, Any]],
    owner: str,
    spec_hash: str,
    expected_tests: tuple[str, ...],
    ownership: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    executed_tests = [
        {"name": name, **summarize_result(test_request, test_result)}
        for name, test_request, test_result in tests
    ]
    by_name = {item["name"]: item for item in executed_tests}
    tests_summary = [
        by_name.get(name, {"name": name, "status": "not_run"})
        for name in expected_tests
    ]
    artifacts_ok = all(
        artifact.get("exists") and artifact.get("kind") == "file"
        for artifact in artifacts
    )
    tests_ok = all(test_result.succeeded for _, _, test_result in tests)
    succeeded = (
        result.succeeded
        and tests_ok
        and len(tests) == len(expected_tests)
        and artifacts_ok
        and not ownership["violations"]
    )
    receipt: dict[str, Any] = {
        "format": RECEIPT_FORMAT,
        "run_id": run_id,
        "task_id": task_id,
        "owner": owner,
        "spec_hash": spec_hash,
        "attempt": attempt,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": round(max(0.0, finished_at - started_at), 6),
        "outcome": "succeeded" if succeeded else "failed",
        "execution": summarize_result(request, result),
        "tests": tests_summary,
        "artifacts": artifacts,
        "ownership": ownership,
    }
    return receipt, digest_json(receipt)


def verify_receipt(receipt: dict[str, Any], expected_hash: str) -> bool:
    return receipt.get("format") == RECEIPT_FORMAT and digest_json(receipt) == expected_hash


def verify_export(exported: dict[str, Any]) -> tuple[bool, tuple[str, ...]]:
    """Offline verification for an exported evidence bundle."""

    issues: list[str] = []
    expected_fields = {
        "format", "spec", "status", "events", "event_chain_root",
        "receipts", "export_sha256",
    }
    if set(exported) != expected_fields:
        issues.append("export has an invalid top-level field set")
    if exported.get("format") != "ai-software-factory/export-v1":
        issues.append("unsupported export format")
    expected = exported.get("export_sha256")
    material = {key: value for key, value in exported.items() if key != "export_sha256"}
    try:
        actual = digest_json(material)
    except (TypeError, ValueError):
        actual = ""
        issues.append("export is not canonical finite JSON")
    if not isinstance(expected, str) or expected != actual:
        issues.append("export digest mismatch")

    status = exported.get("status")
    run_id = status.get("run_id") if isinstance(status, dict) else None
    events = exported.get("events")
    previous = "0" * 64
    if not isinstance(events, list) or not isinstance(run_id, str):
        issues.append("events or run status are malformed")
        events = []
    else:
        for index, event in enumerate(events):
            if not isinstance(event, dict):
                issues.append(f"event {index} is malformed")
                continue
            event_material = {
                "run_id": run_id,
                "task_id": event.get("task_id"),
                "event_type": event.get("event_type"),
                "payload": event.get("payload"),
                "created_at": event.get("created_at"),
                "event_key": event.get("event_key"),
                "previous_hash": previous,
            }
            calculated = digest_json(event_material)
            if (
                event.get("previous_hash") != previous
                or event.get("event_hash") != calculated
            ):
                issues.append(f"event chain mismatch at index {index}")
            previous = calculated
    if exported.get("event_chain_root") != previous:
        issues.append("event chain root mismatch")
    if isinstance(status, dict):
        if status.get("event_count") != len(events):
            issues.append("event count mismatch")
        if status.get("event_head_hash") != previous:
            issues.append("status event head mismatch")

    spec_hash: str | None = None
    try:
        from .models import FactorySpec

        parsed = FactorySpec.from_dict(exported.get("spec"))
        spec_hash = sha256(parsed.canonical_json().encode("utf-8")).hexdigest()
    except (TypeError, ValueError):
        issues.append("exported specification is invalid")
    else:
        if isinstance(status, dict) and status.get("spec_hash") != spec_hash:
            issues.append("specification digest mismatch")

    receipts = exported.get("receipts")
    if not isinstance(receipts, list):
        issues.append("receipts are malformed")
    else:
        if (
            not isinstance(status, dict)
            or isinstance(status.get("receipt_count"), bool)
            or not isinstance(status.get("receipt_count"), int)
            or status.get("receipt_count") != len(receipts)
        ):
            issues.append("receipt count mismatch")
        seen_receipts: set[tuple[str, int]] = set()
        completion_events = {
            event.get("event_key"): event
            for event in events
            if isinstance(event, dict)
            and isinstance(event.get("event_key"), str)
            and event["event_key"].startswith("task.completed:")
        }
        for index, item in enumerate(receipts):
            if not isinstance(item, dict) or not isinstance(item.get("receipt"), dict):
                issues.append(f"receipt {index} is malformed")
                continue
            if set(item) != {
                "task_id", "attempt", "receipt_hash", "receipt", "created_at"
            }:
                issues.append(f"receipt {index} wrapper has an invalid field set")
            receipt = item["receipt"]
            expected_receipt_fields = {
                "format", "run_id", "task_id", "owner", "spec_hash", "attempt",
                "started_at", "finished_at", "duration_seconds", "outcome",
                "execution", "tests", "artifacts", "ownership",
            }
            if set(receipt) != expected_receipt_fields:
                issues.append(f"receipt {index} has an invalid field set")
            try:
                receipt_valid = verify_receipt(receipt, item.get("receipt_hash"))
            except (TypeError, ValueError):
                receipt_valid = False
            if not receipt_valid:
                issues.append(f"receipt {index} digest mismatch")
            task_id = item.get("task_id")
            attempt = item.get("attempt")
            if (
                not isinstance(task_id, str)
                or isinstance(attempt, bool)
                or not isinstance(attempt, int)
                or attempt < 1
            ):
                issues.append(f"receipt {index} identity is malformed")
                continue
            identity = (task_id, attempt)
            if identity in seen_receipts:
                issues.append(f"receipt {index} duplicates an attempt identity")
            seen_receipts.add(identity)
            if (
                receipt.get("run_id") != run_id
                or receipt.get("task_id") != task_id
                or receipt.get("attempt") != attempt
                or receipt.get("spec_hash") != spec_hash
            ):
                issues.append(f"receipt {index} identity does not match its export")
            event = completion_events.get(f"task.completed:{task_id}:{attempt}")
            payload = event.get("payload") if isinstance(event, dict) else None
            if (
                not isinstance(payload, dict)
                or payload.get("receipt_hash") != item.get("receipt_hash")
            ):
                issues.append(f"receipt {index} has no matching completion event")
        if len(completion_events) != len(receipts):
            issues.append("completion event count does not match receipts")
    return not issues, tuple(issues)
