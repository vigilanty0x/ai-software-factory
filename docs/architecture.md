# Architecture

## Design goals

AI Software Factory optimizes for deterministic local operation, explicit
failure, resumability, and evidence that can be inspected without the original
process. Python 3.11 standard library and SQLite are the only runtime platform.

The core invariants are:

1. Invalid or ambiguous specifications never create a run.
2. A task becomes runnable only after every dependency succeeds.
3. Only one unexpired lease owns a task attempt.
4. A stale worker cannot publish a receipt.
5. Handled attempt, publication, and database failures restore the canonical
   workspace before retry or terminal failure.
6. Undeclared or unsafe writes fail closed and are never retried.
7. A terminal run contains only terminal tasks.
8. Evidence is content-addressed and replay is hash-chain verified.

## Components

| Module | Responsibility |
| --- | --- |
| `models.py` | Strict parsing, immutable models, canonical serialization. |
| `graph.py` | Iterative cycle detection, deterministic Kahn ordering, ownership conflicts. |
| `state.py` | Allowed task and run state transitions. |
| `store.py` | Schema v1, transactions, claims, leases, retries, events, receipts, export. |
| `executors.py` | Provider/executor protocols, bounded subprocesses, deterministic mock. |
| `evidence.py` | Output summaries, artifact hashes, workspace deltas, receipt/export verification. |
| `engine.py` | Attempt isolation, orchestration, policy checks, publish, run closure. |
| `cli.py` | Stable JSON command interface and legacy adapter. |

## Persistence and concurrency

SQLite runs in WAL mode with foreign keys and a busy timeout. Scheduling writes
begin with `BEGIN IMMEDIATE`. Claiming performs lease recovery, retry promotion,
dependency refresh, global-attempt-budget checking, and a conditional
`READY -> RUNNING` update in one write transaction.

Workers fence completion with the tuple `(run_id, task_id, attempt,
lease_owner)`. Receipt insertion and state transition share a transaction.
Repeating the same completed receipt is idempotent; changing it is a conflict.
An active engine worker renews its short configured lease on a heartbeat. If the
worker process dies, heartbeats stop and another worker can recover the attempt
after `lease_seconds` rather than waiting for the run wall budget.

The database uses `PRAGMA user_version = 1`. An unknown or unversioned non-empty
database is rejected rather than guessed into a schema.

## Attempt isolation and publish

Before every attempt, the engine copies the canonical workspace to a temporary
sibling workspace. The task and all of its tests execute only in that copy.
The engine hashes its before/after file maps, rejects changed symlinks and
undeclared changes, and checks every declared artifact.

If any command, test, artifact, or policy check fails, the entire temporary
workspace is deleted. After a complete success, every changed regular file and
its prior value are staged before the first replacement. Publication uses
same-directory `os.replace` operations. A handled later replacement or SQLite
transition failure runs the compensating rollback before retry.

The engine validates every provider-produced request against the exact attempt
workspace, task/test timeout policy, remaining run deadline, and output cap.
The global deadline is recalculated before every test and evidence/publication
boundary instead of applying only to the main command.

This is application-level isolation for correctness. It is not a security
sandbox because a hostile process can address paths outside its working
directory. OS isolation is required for untrusted plans.

## Recovery behavior

- An expired running lease becomes retryable or terminal according to the
  per-task attempt limit.
- Live workers renew leases; dead workers therefore become recoverable after one
  configured lease interval.
- Retry delay is `min(cap, base * 2 ** (attempt - 1))`.
- A failed/cancelled dependency blocks all descendants to a fixed point.
- The kill switch fences and cancels all nonterminal tasks.
- Global attempt and wall-time exhaustion activate the same durable cancellation
  path.
- Reopening the database reuses the persisted graph; no in-memory scheduler
  state is authoritative.

## Evidence chain

Each event contains the previous event digest. The run stores an independent
event count and head digest. Receipt content is canonical JSON and addressed by
SHA-256. Export reads spec, status, events, and receipts from one SQLite read
snapshot, then hashes the full bundle. `verify` repeats those checks offline,
including canonical spec identity, receipt counts/identities, and their matching
completion events.

This provides deterministic corruption detection. It does not prove who
created a run; external signatures are intentionally a separate integration.
