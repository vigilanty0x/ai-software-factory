# AI Software Factory

Gate AI software missions on ownership, tests, and evidence.

## Quick start

```bash
python -m pip install -e .
ai-software-factory record.json
```

The CLI emits deterministic fail-closed JSON plus a SHA-256 evidence identifier. Required fields: `mission`, `owner`, `tests_passed`, `tests_total`. Rule: mission ownership must be explicit and all tests must pass.

## Verify

```bash
python -m unittest discover -s tests -v
python scripts/check.py
```

Apache-2.0. Python 3.11+. Zero runtime dependencies.

