"""Strict, dependency-free models for factory specifications.

The parser deliberately accepts a small JSON vocabulary.  Misspelled and
unknown fields are errors instead of being silently ignored.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import PurePosixPath
import re
from typing import Any, Mapping


SCHEMA_VERSION = 1
MAX_SPEC_BYTES = 4 * 1024 * 1024
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_ENVIRONMENT_NAME = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
_SENSITIVE_ENVIRONMENT_FRAGMENTS = (
    "AUTH",
    "COOKIE",
    "CREDENTIAL",
    "KEY",
    "PASS",
    "SECRET",
    "TOKEN",
)


class SpecError(ValueError):
    """Raised when a factory specification violates its JSON contract."""

    def __init__(self, issues: list[str] | tuple[str, ...] | str):
        if isinstance(issues, str):
            issues = [issues]
        self.issues = tuple(issues)
        super().__init__("invalid factory specification: " + "; ".join(self.issues))


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SpecError(f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _expect_object(value: object, location: str, issues: list[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        issues.append(f"{location} must be an object")
        return {}
    return value


def _unknown_fields(
    value: Mapping[str, object], allowed: set[str], location: str, issues: list[str]
) -> None:
    unknown = sorted((field for field in value if field not in allowed), key=repr)
    if unknown:
        issues.append(
            f"{location} has unknown field(s): {', '.join(repr(item) for item in unknown)}"
        )


def _required_fields(
    value: Mapping[str, object], required: set[str], location: str, issues: list[str]
) -> None:
    missing = sorted(required - set(value))
    if missing:
        issues.append(f"{location} is missing field(s): {', '.join(missing)}")


def _string(
    value: object,
    location: str,
    issues: list[str],
    *,
    nonempty: bool = True,
    maximum: int = 4096,
) -> str:
    if not isinstance(value, str):
        issues.append(f"{location} must be a string")
        return ""
    if nonempty and not value.strip():
        issues.append(f"{location} must not be blank")
    if len(value) > maximum:
        issues.append(f"{location} must be at most {maximum} characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        issues.append(f"{location} must not contain control characters")
    return value


def _integer(
    value: object,
    location: str,
    issues: list[str],
    *,
    minimum: int,
    maximum: int,
) -> int:
    if not _is_int(value):
        issues.append(f"{location} must be an integer")
        return minimum
    if not minimum <= value <= maximum:
        issues.append(f"{location} must be between {minimum} and {maximum}")
    return value


def _number(
    value: object,
    location: str,
    issues: list[str],
    *,
    minimum: float,
    maximum: float,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        issues.append(f"{location} must be a number")
        return minimum
    result = float(value)
    if not minimum <= result <= maximum:
        issues.append(f"{location} must be between {minimum:g} and {maximum:g}")
    return result


def normalize_relative_path(value: object, location: str, issues: list[str]) -> str:
    """Validate and normalize a portable path confined to a workspace."""

    raw = _string(value, location, issues, maximum=512)
    if not raw:
        return ""
    if "\\" in raw:
        issues.append(f"{location} must use '/' separators")
        return raw
    path = PurePosixPath(raw)
    if path.is_absolute() or raw.startswith("/"):
        issues.append(f"{location} must be relative")
    if any(part in {"", ".", ".."} for part in path.parts):
        issues.append(f"{location} must be normalized and must not contain '.' or '..'")
    if path.parts and ":" in path.parts[0]:
        issues.append(f"{location} must not contain a drive prefix")
    normalized = path.as_posix()
    if normalized != raw:
        issues.append(f"{location} must already be normalized")
    return normalized


def _argv(value: object, location: str, issues: list[str]) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        issues.append(f"{location} must be a non-empty array of strings")
        return ()
    result: list[str] = []
    if len(value) > 128:
        issues.append(f"{location} must contain at most 128 arguments")
    for index, item in enumerate(value):
        if not isinstance(item, str):
            issues.append(f"{location}[{index}] must be a string")
            argument = ""
        else:
            argument = item
            if not argument:
                issues.append(f"{location}[{index}] must not be blank")
            if len(argument) > 8192:
                issues.append(f"{location}[{index}] must be at most 8192 characters")
        if "\0" in argument:
            issues.append(f"{location}[{index}] must not contain NUL")
        result.append(argument)
    return tuple(result)


@dataclass(frozen=True, slots=True)
class TestSpec:
    name: str
    command: tuple[str, ...]
    timeout_seconds: float | None = None

    @classmethod
    def parse(cls, raw: object, location: str, issues: list[str]) -> "TestSpec":
        value = _expect_object(raw, location, issues)
        allowed = {"name", "command", "timeout_seconds"}
        _unknown_fields(value, allowed, location, issues)
        _required_fields(value, {"name", "command"}, location, issues)
        timeout = None
        if "timeout_seconds" in value:
            timeout = _number(
                value["timeout_seconds"],
                f"{location}.timeout_seconds",
                issues,
                minimum=0.01,
                maximum=86_400,
            )
        return cls(
            name=_string(value.get("name"), f"{location}.name", issues, maximum=128),
            command=_argv(value.get("command"), f"{location}.command", issues),
            timeout_seconds=timeout,
        )


@dataclass(frozen=True, slots=True)
class TaskSpec:
    id: str
    owner: str
    description: str
    command: tuple[str, ...]
    depends_on: tuple[str, ...]
    owned_paths: tuple[str, ...]
    artifacts: tuple[str, ...]
    tests: tuple[TestSpec, ...]
    environment: tuple[tuple[str, str], ...]
    timeout_seconds: float | None
    max_attempts: int | None

    @classmethod
    def parse(cls, raw: object, index: int, issues: list[str]) -> "TaskSpec":
        location = f"tasks[{index}]"
        value = _expect_object(raw, location, issues)
        allowed = {
            "id",
            "owner",
            "description",
            "command",
            "depends_on",
            "owned_paths",
            "artifacts",
            "tests",
            "environment",
            "timeout_seconds",
            "max_attempts",
        }
        _unknown_fields(value, allowed, location, issues)
        _required_fields(value, {"id", "owner", "command"}, location, issues)

        task_id = _string(value.get("id"), f"{location}.id", issues, maximum=64)
        if task_id and not _IDENTIFIER.fullmatch(task_id):
            issues.append(
                f"{location}.id must match {_IDENTIFIER.pattern!r}"
            )
        owner = _string(value.get("owner"), f"{location}.owner", issues, maximum=128)
        description = _string(
            value.get("description", ""),
            f"{location}.description",
            issues,
            nonempty=False,
            maximum=2048,
        )

        dependencies_raw = value.get("depends_on", [])
        dependencies: list[str] = []
        if not isinstance(dependencies_raw, list):
            issues.append(f"{location}.depends_on must be an array")
        else:
            if len(dependencies_raw) > 1000:
                issues.append(f"{location}.depends_on must contain at most 1000 items")
            for dep_index, dependency in enumerate(dependencies_raw):
                dependencies.append(
                    _string(
                        dependency,
                        f"{location}.depends_on[{dep_index}]",
                        issues,
                        maximum=64,
                    )
                )
            if len(set(dependencies)) != len(dependencies):
                issues.append(f"{location}.depends_on must not contain duplicates")

        def paths(field: str) -> tuple[str, ...]:
            raw_paths = value.get(field, [])
            result: list[str] = []
            if not isinstance(raw_paths, list):
                issues.append(f"{location}.{field} must be an array")
                return ()
            if len(raw_paths) > 1000:
                issues.append(f"{location}.{field} must contain at most 1000 items")
            for path_index, path in enumerate(raw_paths):
                result.append(
                    normalize_relative_path(
                        path, f"{location}.{field}[{path_index}]", issues
                    )
                )
                if result[-1] == ".":
                    issues.append(
                        f"{location}.{field}[{path_index}] must name a path below the workspace"
                    )
            if len(set(result)) != len(result):
                issues.append(f"{location}.{field} must not contain duplicates")
            return tuple(result)

        tests_raw = value.get("tests", [])
        tests: list[TestSpec] = []
        if not isinstance(tests_raw, list):
            issues.append(f"{location}.tests must be an array")
        else:
            if len(tests_raw) > 100:
                issues.append(f"{location}.tests must contain at most 100 items")
            for test_index, test in enumerate(tests_raw):
                tests.append(
                    TestSpec.parse(test, f"{location}.tests[{test_index}]", issues)
                )
            names = [test.name for test in tests]
            if len(set(names)) != len(names):
                issues.append(f"{location}.tests must have unique names")

        environment_raw = value.get("environment", {})
        environment: list[tuple[str, str]] = []
        if not isinstance(environment_raw, dict):
            issues.append(f"{location}.environment must be an object")
        else:
            for name, env_value in sorted(
                environment_raw.items(), key=lambda item: str(item[0])
            ):
                if not isinstance(name, str) or not _ENVIRONMENT_NAME.fullmatch(name):
                    issues.append(
                        f"{location}.environment key {name!r} must be an uppercase environment name"
                    )
                elif any(fragment in name for fragment in _SENSITIVE_ENVIRONMENT_FRAGMENTS):
                    issues.append(
                        f"{location}.environment key {name!r} looks sensitive; "
                        "factory specifications must never contain secrets"
                    )
                parsed_value = _string(
                    env_value,
                    f"{location}.environment[{name!r}]",
                    issues,
                    nonempty=False,
                    maximum=4096,
                )
                if "\0" in parsed_value:
                    issues.append(
                        f"{location}.environment[{name!r}] must not contain NUL"
                    )
                environment.append((str(name), parsed_value))

        timeout = None
        if "timeout_seconds" in value:
            timeout = _number(
                value["timeout_seconds"],
                f"{location}.timeout_seconds",
                issues,
                minimum=0.01,
                maximum=86_400,
            )
        max_attempts = None
        if "max_attempts" in value:
            max_attempts = _integer(
                value["max_attempts"],
                f"{location}.max_attempts",
                issues,
                minimum=1,
                maximum=100,
            )
        return cls(
            id=task_id,
            owner=owner,
            description=description,
            command=_argv(value.get("command"), f"{location}.command", issues),
            depends_on=tuple(dependencies),
            owned_paths=paths("owned_paths"),
            artifacts=paths("artifacts"),
            tests=tuple(tests),
            environment=tuple(environment),
            timeout_seconds=timeout,
            max_attempts=max_attempts,
        )


@dataclass(frozen=True, slots=True)
class BudgetSpec:
    max_tasks: int = 1000
    max_attempts: int = 3000
    max_wall_seconds: float = 3600.0
    max_output_bytes: int = 1_048_576
    default_task_timeout_seconds: float = 300.0
    lease_seconds: float = 60.0
    retry_base_seconds: float = 1.0
    retry_cap_seconds: float = 60.0
    default_max_attempts: int = 3

    @classmethod
    def parse(cls, raw: object, issues: list[str]) -> "BudgetSpec":
        location = "budget"
        value = _expect_object(raw, location, issues)
        allowed = {
            "max_tasks",
            "max_attempts",
            "max_wall_seconds",
            "max_output_bytes",
            "default_task_timeout_seconds",
            "lease_seconds",
            "retry_base_seconds",
            "retry_cap_seconds",
            "default_max_attempts",
        }
        _unknown_fields(value, allowed, location, issues)
        defaults = cls()
        return cls(
            max_tasks=_integer(
                value.get("max_tasks", defaults.max_tasks),
                f"{location}.max_tasks",
                issues,
                minimum=1,
                maximum=10_000,
            ),
            max_attempts=_integer(
                value.get("max_attempts", defaults.max_attempts),
                f"{location}.max_attempts",
                issues,
                minimum=1,
                maximum=1_000_000,
            ),
            max_wall_seconds=_number(
                value.get("max_wall_seconds", defaults.max_wall_seconds),
                f"{location}.max_wall_seconds",
                issues,
                minimum=0.01,
                maximum=604_800,
            ),
            max_output_bytes=_integer(
                value.get("max_output_bytes", defaults.max_output_bytes),
                f"{location}.max_output_bytes",
                issues,
                minimum=1,
                maximum=100_000_000,
            ),
            default_task_timeout_seconds=_number(
                value.get(
                    "default_task_timeout_seconds",
                    defaults.default_task_timeout_seconds,
                ),
                f"{location}.default_task_timeout_seconds",
                issues,
                minimum=0.01,
                maximum=86_400,
            ),
            lease_seconds=_number(
                value.get("lease_seconds", defaults.lease_seconds),
                f"{location}.lease_seconds",
                issues,
                minimum=0.1,
                maximum=86_400,
            ),
            retry_base_seconds=_number(
                value.get("retry_base_seconds", defaults.retry_base_seconds),
                f"{location}.retry_base_seconds",
                issues,
                minimum=0,
                maximum=86_400,
            ),
            retry_cap_seconds=_number(
                value.get("retry_cap_seconds", defaults.retry_cap_seconds),
                f"{location}.retry_cap_seconds",
                issues,
                minimum=0,
                maximum=604_800,
            ),
            default_max_attempts=_integer(
                value.get("default_max_attempts", defaults.default_max_attempts),
                f"{location}.default_max_attempts",
                issues,
                minimum=1,
                maximum=100,
            ),
        )


@dataclass(frozen=True, slots=True)
class FactorySpec:
    schema_version: int
    name: str
    workspace: str
    budget: BudgetSpec
    tasks: tuple[TaskSpec, ...]

    @classmethod
    def from_dict(cls, raw: object) -> "FactorySpec":
        issues: list[str] = []
        value = _expect_object(raw, "root", issues)
        allowed = {"schema_version", "name", "workspace", "budget", "tasks"}
        _unknown_fields(value, allowed, "root", issues)
        _required_fields(value, {"schema_version", "name", "tasks"}, "root", issues)
        version = _integer(
            value.get("schema_version"),
            "schema_version",
            issues,
            minimum=SCHEMA_VERSION,
            maximum=SCHEMA_VERSION,
        )
        name = _string(value.get("name"), "name", issues, maximum=128)
        workspace = normalize_relative_path(
            value.get("workspace", ".factory/workspace"), "workspace", issues
        )
        budget = BudgetSpec.parse(value.get("budget", {}), issues)

        tasks_raw = value.get("tasks")
        tasks: list[TaskSpec] = []
        if not isinstance(tasks_raw, list) or not tasks_raw:
            issues.append("tasks must be a non-empty array")
        else:
            for index, task in enumerate(tasks_raw):
                tasks.append(TaskSpec.parse(task, index, issues))

        identifiers = [task.id for task in tasks]
        if len(set(identifiers)) != len(identifiers):
            issues.append("task ids must be unique")
        if len(tasks) > budget.max_tasks:
            issues.append(
                f"tasks contains {len(tasks)} items, exceeding budget.max_tasks={budget.max_tasks}"
            )
        if budget.retry_cap_seconds < budget.retry_base_seconds:
            issues.append("budget.retry_cap_seconds must be >= retry_base_seconds")

        # Graph-level checks are imported lazily to keep model parsing acyclic.
        if not issues:
            from .graph import validate_graph

            issues.extend(validate_graph(tuple(tasks)))
        if issues:
            raise SpecError(issues)
        return cls(version, name, workspace, budget, tuple(tasks))

    @classmethod
    def from_json(cls, source: str | bytes | bytearray) -> "FactorySpec":
        encoded_size = len(source.encode("utf-8")) if isinstance(source, str) else len(source)
        if encoded_size > MAX_SPEC_BYTES:
            raise SpecError(f"factory specification exceeds {MAX_SPEC_BYTES} bytes")
        try:
            raw = json.loads(
                source,
                object_pairs_hook=_unique_object,
                parse_constant=lambda value: (_ for _ in ()).throw(
                    SpecError(f"non-finite JSON number is forbidden: {value}")
                ),
            )
        except SpecError:
            raise
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise SpecError(f"invalid JSON: {exc}") from exc
        return cls.from_dict(raw)

    def to_dict(self) -> dict[str, Any]:
        budget = {
            "max_tasks": self.budget.max_tasks,
            "max_attempts": self.budget.max_attempts,
            "max_wall_seconds": self.budget.max_wall_seconds,
            "max_output_bytes": self.budget.max_output_bytes,
            "default_task_timeout_seconds": self.budget.default_task_timeout_seconds,
            "lease_seconds": self.budget.lease_seconds,
            "retry_base_seconds": self.budget.retry_base_seconds,
            "retry_cap_seconds": self.budget.retry_cap_seconds,
            "default_max_attempts": self.budget.default_max_attempts,
        }
        tasks: list[dict[str, Any]] = []
        for task in self.tasks:
            item: dict[str, Any] = {
                "id": task.id,
                "owner": task.owner,
                "description": task.description,
                "command": list(task.command),
                "depends_on": list(task.depends_on),
                "owned_paths": list(task.owned_paths),
                "artifacts": list(task.artifacts),
                "tests": [],
                "environment": dict(task.environment),
            }
            if task.timeout_seconds is not None:
                item["timeout_seconds"] = task.timeout_seconds
            if task.max_attempts is not None:
                item["max_attempts"] = task.max_attempts
            for test in task.tests:
                test_item: dict[str, Any] = {
                    "name": test.name,
                    "command": list(test.command),
                }
                if test.timeout_seconds is not None:
                    test_item["timeout_seconds"] = test.timeout_seconds
                item["tests"].append(test_item)
            tasks.append(item)
        return {
            "schema_version": self.schema_version,
            "name": self.name,
            "workspace": self.workspace,
            "budget": budget,
            "tasks": tasks,
        }

    def canonical_json(self) -> str:
        return json.dumps(
            self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )

    def task(self, task_id: str) -> TaskSpec:
        for task in self.tasks:
            if task.id == task_id:
                return task
        raise KeyError(task_id)
