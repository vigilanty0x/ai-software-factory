"""Explicit run and task state machines."""

from __future__ import annotations

from enum import StrEnum


class InvalidTransition(ValueError):
    pass


class TaskState(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    RETRY_WAIT = "retry_wait"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class RunState(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


TASK_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    TaskState.PENDING: frozenset(
        {TaskState.READY, TaskState.BLOCKED, TaskState.CANCELLED}
    ),
    TaskState.READY: frozenset({TaskState.RUNNING, TaskState.CANCELLED}),
    TaskState.RUNNING: frozenset(
        {TaskState.SUCCEEDED, TaskState.RETRY_WAIT, TaskState.FAILED, TaskState.CANCELLED}
    ),
    TaskState.RETRY_WAIT: frozenset(
        {TaskState.READY, TaskState.FAILED, TaskState.CANCELLED}
    ),
    TaskState.SUCCEEDED: frozenset(),
    TaskState.FAILED: frozenset(),
    TaskState.BLOCKED: frozenset(),
    TaskState.CANCELLED: frozenset(),
}

RUN_TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    RunState.CREATED: frozenset({RunState.RUNNING, RunState.CANCELLED}),
    RunState.RUNNING: frozenset(
        {RunState.SUCCEEDED, RunState.FAILED, RunState.CANCELLED}
    ),
    RunState.SUCCEEDED: frozenset(),
    RunState.FAILED: frozenset(),
    RunState.CANCELLED: frozenset(),
}


def validate_task_transition(current: str | TaskState, target: str | TaskState) -> None:
    source = TaskState(current)
    destination = TaskState(target)
    if destination not in TASK_TRANSITIONS[source]:
        raise InvalidTransition(f"invalid task transition: {source} -> {destination}")


def validate_run_transition(current: str | RunState, target: str | RunState) -> None:
    source = RunState(current)
    destination = RunState(target)
    if destination not in RUN_TRANSITIONS[source]:
        raise InvalidTransition(f"invalid run transition: {source} -> {destination}")

