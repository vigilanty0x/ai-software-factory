# Factory specification v1

The input is UTF-8 JSON capped at 4 MiB. Duplicate object keys, unknown fields,
non-finite numbers, and type coercions are rejected. Booleans never count as
integers. Detached evidence exports accepted by the CLI are capped at 64 MiB.

## Root object

| Field | Required | Contract |
| --- | --- | --- |
| `schema_version` | yes | Integer `1`. |
| `name` | yes | Nonblank string, at most 128 characters. |
| `workspace` | no | Normalized relative path; default `.factory/workspace`. |
| `budget` | no | Strict budget object. |
| `tasks` | yes | Non-empty array, bounded by `budget.max_tasks`. |

## Budget object

| Field | Default | Range / meaning |
| --- | ---: | --- |
| `max_tasks` | 1000 | 1..10,000 validated tasks. |
| `max_attempts` | 3000 | 1..1,000,000 attempts across the run. |
| `max_wall_seconds` | 3600 | 0.01..604,800 seconds across commands, tests, evidence, and publication. |
| `max_output_bytes` | 1,048,576 | Combined captured stdout/stderr prefix. |
| `default_task_timeout_seconds` | 300 | Default subprocess timeout. |
| `lease_seconds` | 60 | Worker lease and heartbeat recovery interval. |
| `retry_base_seconds` | 1 | First retry delay; zero is allowed. |
| `retry_cap_seconds` | 60 | Maximum retry delay; never below the base. |
| `default_max_attempts` | 3 | Default per-task attempt bound. |

## Task object

| Field | Required | Contract |
| --- | --- | --- |
| `id` | yes | Unique lowercase identifier matching `[a-z][a-z0-9_-]{0,63}`. |
| `owner` | yes | Nonblank, control-character-free attribution. |
| `description` | no | Human context, at most 2048 characters. |
| `command` | yes | Non-empty array of arguments; no shell parsing. |
| `depends_on` | no | Unique known task IDs; graph must be acyclic. |
| `owned_paths` | no | Normalized relative paths this task may change. |
| `artifacts` | no | Regular files that must exist and be hashed after success. |
| `tests` | no | Ordered test commands, at most 100. |
| `environment` | no | Non-secret string overrides with uppercase names. |
| `timeout_seconds` | no | Task-specific timeout. |
| `max_attempts` | no | Task-specific bound from 1 through 100. |

Paths use `/`, are relative, contain no `.` or `..` segments, and cannot be the
workspace root. Two different tasks cannot own the same path or overlapping
parent/child paths. Sibling paths are valid.

Artifact paths participate in ownership. A task may list both an owned
directory and artifacts below that directory without conflicting with itself.

Environment names containing common credential markers are rejected. Put no
secret values in factory JSON. If a future integration needs credentials, it
should resolve opaque references at an isolated provider boundary rather than
persisting values in this format.

## Test object

| Field | Required | Contract |
| --- | --- | --- |
| `name` | yes | Unique nonblank name within the task. |
| `command` | yes | Non-empty argument array. |
| `timeout_seconds` | no | Test-specific timeout. |

Tests execute in declared order after the main command. Each test timeout is
capped by the remaining global wall deadline. The first failure stops the
remaining tests; evidence marks each remaining expected test as `not_run`.

## Canonical form and idempotency

Canonical JSON uses sorted object keys, UTF-8 text, compact separators, finite
numbers, arrays, and explicit default budget values. Optional task/test values
that are absent remain absent. The SHA-256 of this canonical form is the spec
identity used by planning and evidence.

An idempotency key binds to exactly one spec digest. Reusing the key with a
different digest fails.
