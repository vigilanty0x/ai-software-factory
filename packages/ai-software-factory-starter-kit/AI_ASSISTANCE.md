# AI assistance

AI assisted with scaffolding and tests. Human review remains required for security decisions and release claims. The repository contains only generic code and synthetic fixtures.

## Assisted work

AI assistance covered implementation drafts, adversarial test cases, documentation, and continuous-integration configuration. Maintainers are responsible for reviewing every accepted change.

## Data boundary

Examples and tests use synthetic public-safe data. Do not submit credentials, personal data, production records, proprietary prompts, or confidential incident material.

## Verification boundary

Generated receipts prove only the deterministic checks documented in the README. They do not authenticate caller assertions, evidence issuers, external systems, or release decisions unless the implementation explicitly performs that check.

## Maintainer checklist

Before release, run the unit suite, repository check, wheel build, installed-wheel import, CLI example, and malformed-input smoke test. Review security-sensitive claims and dependency changes manually.
