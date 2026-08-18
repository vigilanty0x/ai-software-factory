# AI Software Factory release contract

Candidate quality, signed provenance, publication, and post-publication verification are separate states.

- `PREPARED`: the exact source SHA passed tests/counter-proofs and produced installable wheel+sdist, checksums and SBOM.
- `ATTESTED`: the approved wheel has GitHub/Sigstore SLSA provenance that passed strict read-back verification.
- `TAGGED`: an explicit tag was created after approval.
- `RELEASED`: the expected immutable assets were published under that tag.
- `VERIFIED`: a separate read-back verified tag target, assets, checksums, provenance, installability and smoke behavior.
- `BLOCKED`: one or more required gates are missing or red.
- `ROLLED_BACK`: consumers returned to the documented 0.1.0 fallback while failed 1.0 evidence is preserved.

`release-policy.v1.json` deliberately sets `publish_enabled=false`; normal CI can reach PREPARED and ATTESTED only.

## Pre-publication evidence

The exact candidate must pass the complete 3 OS × 3 Python matrix, the full runtime test suite, legacy positive/negative gate, real wheel install, CLI smoke outside checkout, complete sdist tests, public-boundary checks, SHA-256 checksums, CycloneDX 1.6 SBOM, and strict GitHub/Sigstore provenance verification.

## Publication and rollback

Publication requires a separate reviewed change that enables one exact version/source SHA and defines immutable assets plus a read-only post-publication verifier. Rollback is 0.1.0 for the legacy gate; do not rewrite 1.0 SQLite/export evidence into an older format.

## Archive gate

Portfolio consolidation is not archive authorization. Source archival requires consumer inventory, compatibility/redirect evidence, rollback proof, and explicit human approval.
