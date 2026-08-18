# AI Software Factory Starter Kit

## Purpose

Validate an internally consistent declared delivery manifest with one-to-one ownership, test receipt, structured evidence, independent review, and release receipt.

## Non-goals

The package does not orchestrate agents, create worktrees, run tests, perform reviews, release artifacts, authenticate issuers, or establish external trust.

## Install

Requires Python 3.11 or newer: `python -m pip install .`

## API

`evaluate(record)` accepts `spec`, `ownership`, structured `tests`, evidence records, independent `review`, and a `release` receipt binding artifact and review digests.

## CLI

Run `ai-software-factory-starter-kit examples/valid.json`. Output is labeled `internally-consistent-declared-evidence` and `independently_verified_by_tool: false`.

## Example

The example uses placeholder digests, two unique agents/worktrees, an independent reviewer, and a release receipt with the review-record digest.

## Security

Duplicate ownership, reviewer/agent overlap, failing tests, plain-string evidence, mismatched digests, missing issuer/time fields, naive times, and oversized input fail closed.

## Limits

Internal digest consistency is not cryptographic issuer authentication. At most 100 ownership/evidence records and 128 KiB aggregate input are accepted.

## Tests

Run `python -m unittest discover -s tests -v` and `python scripts/check.py`.

## AI assistance

See `AI_ASSISTANCE.md`; release authorization and independent review remain human/governed-system decisions.

## License

Apache-2.0; see `LICENSE`.
