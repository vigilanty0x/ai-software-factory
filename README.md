# AI Software Factory Starter Kit

Validate spec-to-release factory manifests with agents, worktrees, tests, and evidence.

## Quick start

```bash
python -m pip install -e .
ai-software-factory-starter-kit examples/valid.json
```

The command emits deterministic fail-closed JSON and a SHA-256 evidence identifier. It uses synthetic input and has zero runtime dependencies.

## Verify

```bash
python -m unittest discover -s tests -v
python scripts/check.py
```

Apache-2.0. Python 3.11+.

