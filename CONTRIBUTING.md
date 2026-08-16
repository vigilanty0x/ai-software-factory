# Contributing

Thank you for improving AI Software Factory.

## Development contract

- Python 3.11+ and standard-library runtime only.
- Keep specifications and fixtures synthetic, offline, and free of credentials.
- Preserve strict fail-closed behavior; never silently ignore a new field.
- Add tests at the boundary carrying the risk: storage tests for transactions,
  subprocess tests for process limits, and CLI tests for public behavior.
- Do not weaken path ownership, attempt isolation, lease fencing, or evidence
  verification for convenience.

## Local checks

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
PYTHONPATH=src python scripts/check.py
python -m compileall -q src tests
```

Run the focused module while iterating, then the complete suite before opening a
pull request. Include the exact commands and outcomes in the PR description.

## Change design

1. State the observable behavior and failure mode.
2. Add a regression or acceptance test.
3. Make the smallest coherent implementation change.
4. Verify recovery, negative paths, and legacy `evaluate()` compatibility.
5. Update the specification, architecture, or security model when a contract
   changes.

Schema changes require a real `PRAGMA user_version` migration and rollback plan.
Do not modify version 1 tables in place without migration coverage.

## Pull requests

Keep commits intentional and scoped. Explain compatibility decisions, evidence
format changes, and any residual platform limitation. CI must pass on the
supported Python and operating-system matrix.

