# Changelog

All notable changes are documented here. The format follows Keep a Changelog
and the project uses semantic versioning.

## 1.0.0 - 2026-08-16

### Added

- Strict factory specification v1 and canonical serialization.
- Iterative DAG validation and path-ownership conflict detection.
- SQLite run/task/event/receipt store with WAL, schema versioning, idempotency,
  atomic claims, leases, retries, budgets, and kill switch.
- Explicit task and run state machines.
- Provider/executor protocols, deterministic mock, and bounded subprocess
  executor.
- Isolated per-attempt workspaces and declared-change publication.
- Lease-fenced publication with copy-time digest revalidation against captured evidence.
- Bounded spec/export loading, duplicate-key rejection, and strict export field verification.
- Artifact, test, output, receipt, event-chain, and export evidence.
- CLI commands `init`, `validate`, `plan`, `run`, `status`, `replay`, `export`,
  `verify`, and `kill`.
- Synthetic offline example and positive, negative, concurrency, corruption,
  timeout, retry, and end-to-end tests.
- Complete source distribution manifest and CI that builds, installs, and
  smoke-tests the real wheel with an exactly pinned build backend.

### Compatibility

- Preserved `evaluate(record)` and the legacy single-record CLI.
- Added `verify_evidence()` and hardened legacy JSON/type handling.

### Fixed

- Replaced run-length leases with heartbeat-renewed short leases so a dead
  worker is recoverable before the global wall deadline.
- Applied the remaining global wall budget to every test and publication step.
- Terminated surviving POSIX process-group descendants even when their direct
  parent exited successfully.
- Exported evidence from one SQLite snapshot and verified receipt counts,
  identities, spec bindings, and completion-event bindings.
- Confined custom provider requests to the disposable attempt workspace and
  factory timeout/output policy.
- Pre-staged publication backups and compensated partial filesystem changes when
  a handled replacement or database transition fails.
- Closed every short-lived SQLite read handle explicitly so Windows can remove
  completed temporary databases, and made subprocess newline assertions
  platform-aware without normalizing captured evidence.

## 0.1.0 - 2026-08-15

### Added

- Deterministic mission ownership and test-count gate.
