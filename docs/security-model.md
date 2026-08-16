# Security model

## Intended use

AI Software Factory executes trusted local specifications with bounded process
and persistence behavior. It is suitable for CI-like automation, deterministic
agent handoffs, synthetic examples, and local development workflows.

It is not a multi-tenant code sandbox. Do not run untrusted commands under an
account that can access valuable files, credentials, sockets, or services.

## Trust boundaries

| Boundary | Control | Residual risk |
| --- | --- | --- |
| JSON input | Strict schema, no unknowns/duplicates, path and budget validation. | A valid command is still executable code. |
| Process launch | Argument array, `shell=False`, minimal inherited environment. | The executable retains current-user OS permissions. |
| Runtime | Deadline, POSIX process-group cleanup, combined captured-output cap. | Windows descendant termination is best-effort without a Job Object. |
| Workspace | Per-attempt copy, provider-request confinement, ownership diff, symlink rejection, compensated handled failures. | A hostile process can address paths outside its working directory. |
| Scheduling | SQLite write transactions, heartbeat-renewed leases, attempt fencing, kill switch. | A database administrator can rewrite all anchors coherently. |
| Evidence | Full-stream digests, artifact hashes, receipt/event correlations, single-snapshot export digest. | Hashes are not signatures or trusted timestamps. |

## Secret handling

Specifications, event logs, receipts, and exports are designed to be shareable.
Do not place secrets in them. Secret-looking environment names are rejected.
The subprocess environment starts from a small allowlist rather than inheriting
the full parent environment.

Raw stdout and stderr are not persisted, but their captured prefixes live in
memory during execution. Commands should not print sensitive values.

## Subprocess details

The executor never invokes a shell. On POSIX, the process group is cleaned up
when the direct parent times out or exits while descendants remain; such an
attempt is not considered successful. Descendants retaining output pipes cannot
extend collection past the request deadline. Captured stdout and stderr share
one byte budget, while SHA-256 is calculated over the complete drained streams.

On Windows, the standard-library implementation terminates the direct process
and uses a new process group, but cannot provide Job Object guarantees. Use a
container or dedicated VM when descendant containment matters.

## Workspace integrity

Every attempt receives a disposable copy of the last successful canonical
workspace. Only declared changes from a fully successful attempt are published.
An undeclared or symlink change is non-retryable and the attempt is discarded.
This prevents a failed attempt from poisoning the baseline used by a retry.

Provider-produced execution requests are rejected unless their working directory
stays inside the exact disposable attempt and their timeout/output values remain
within the spec policy. Publishing regular files reopens them without following
the final symlink, rehashes copied bytes against the captured snapshot, and
pre-stages both replacements and backups. Lease validation, publication, and
task completion are fenced by one SQLite write transaction. Handled I/O or
database failures compensate already-replaced targets before the attempt fails.

Multi-file publication is still not a native filesystem transaction. Abrupt
host or process loss between a replacement and SQLite commit can leave recovery
material or require operator inspection; use a snapshotting filesystem or an
external transactional release boundary when that failure model is in scope.

## Evidence limitations

The event count/head anchor catches alteration, insertion, reorder, and deletion
when the run row is intact. Export uses one SQLite read snapshot; the offline
verifier also correlates receipt identities, counts, spec digests, and completion
events. An attacker able to rewrite every SQLite row or recompute an entire
export can also recompute hashes. Use external signing, append-only storage, or
a transparency log for adversarial provenance.

## Recommended deployment

- Run as a dedicated unprivileged account.
- Put the workspace and database on a private local filesystem.
- Apply OS resource controls when executing heavy or unknown tools.
- Keep network disabled unless a task explicitly requires and reviews it.
- Export and externally sign evidence at a release boundary.
- Back up the SQLite file using SQLite-aware snapshot tooling.
