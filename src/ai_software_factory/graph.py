"""DAG and path-ownership validation."""

from __future__ import annotations

import heapq
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - import only for static analyzers
    from .models import TaskSpec


def _path_overlaps(left: str, right: str) -> bool:
    left_parts = PurePosixPath(left).parts
    right_parts = PurePosixPath(right).parts
    shorter = min(len(left_parts), len(right_parts))
    return left_parts[:shorter] == right_parts[:shorter]


def find_cycle(tasks: tuple["TaskSpec", ...]) -> tuple[str, ...] | None:
    """Find one cycle without recursion, even for very deep task graphs."""

    dependencies = {task.id: tuple(task.depends_on) for task in tasks}
    complete: set[str] = set()
    for root in sorted(dependencies):
        if root in complete:
            continue
        stack: list[tuple[str, int]] = [(root, 0)]
        path: list[str] = []
        positions: dict[str, int] = {}
        while stack:
            task_id, child_index = stack[-1]
            if child_index == 0:
                positions[task_id] = len(path)
                path.append(task_id)
            children = dependencies.get(task_id, ())
            if child_index >= len(children):
                stack.pop()
                complete.add(task_id)
                positions.pop(task_id, None)
                path.pop()
                continue
            dependency = children[child_index]
            stack[-1] = (task_id, child_index + 1)
            if dependency in positions:
                start = positions[dependency]
                return tuple(path[start:] + [dependency])
            if dependency not in complete:
                stack.append((dependency, 0))
    return None


def topological_order(tasks: tuple["TaskSpec", ...]) -> tuple[str, ...]:
    """Return a deterministic topological ordering.

    Specifications must already have passed :func:`validate_graph`.
    """

    indegree = {task.id: len(task.depends_on) for task in tasks}
    dependents: dict[str, list[str]] = {task.id: [] for task in tasks}
    for task in tasks:
        for dependency in task.depends_on:
            dependents[dependency].append(task.id)
    ready = [task_id for task_id, degree in indegree.items() if degree == 0]
    heapq.heapify(ready)
    result: list[str] = []
    while ready:
        task_id = heapq.heappop(ready)
        result.append(task_id)
        for dependent in dependents[task_id]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                heapq.heappush(ready, dependent)
    if len(result) != len(tasks):
        raise ValueError("task graph contains a cycle")
    return tuple(result)


def validate_graph(tasks: tuple["TaskSpec", ...]) -> list[str]:
    issues: list[str] = []
    identifiers = {task.id for task in tasks}
    for task in tasks:
        for dependency in task.depends_on:
            if dependency not in identifiers:
                issues.append(
                    f"task {task.id!r} depends on unknown task {dependency!r}"
                )
            elif dependency == task.id:
                issues.append(f"task {task.id!r} cannot depend on itself")

    if not issues:
        cycle = find_cycle(tasks)
        if cycle:
            issues.append("dependency cycle detected: " + " -> ".join(cycle))

    entries: list[tuple[str, str]] = []
    for task in tasks:
        effective_paths = tuple(dict.fromkeys(task.owned_paths + task.artifacts))
        for path in effective_paths:
            entries.append((path, task.id))
    # Parents are processed before descendants regardless of task order.
    entries.sort(key=lambda item: (len(PurePosixPath(item[0]).parts), item[0], item[1]))
    owners: dict[str, list[str]] = {}
    for path, task_id in entries:
            candidate_paths = [path]
            parent = PurePosixPath(path).parent
            while parent.parts:
                candidate_paths.append(parent.as_posix())
                parent = parent.parent
            for other_path in candidate_paths:
                for other_task in owners.get(other_path, []):
                    if task_id == other_task:
                        continue
                    issues.append(
                        "path ownership conflict: "
                        f"task {other_task!r} owns {other_path!r} while "
                        f"task {task_id!r} owns {path!r}"
                    )
            owners.setdefault(path, []).append(task_id)
    return issues
