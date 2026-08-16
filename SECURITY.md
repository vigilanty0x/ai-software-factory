# Security policy

## Reporting a vulnerability

Use this repository's private GitHub Security Advisory flow. Do not open a
public issue for a suspected vulnerability. Never attach credentials, private
production records, or an exploit against a system you do not own.

Include the affected version, platform, minimal synthetic reproduction, impact,
and any proposed mitigation. Maintainers will acknowledge a complete report as
capacity permits and coordinate disclosure after a fix is available.

## Supported versions

The latest major release receives security fixes. Version 1.x is currently
supported; the 0.x prototype is not.

## Scope

Security-sensitive areas include specification validation, workspace
confinement, process timeouts, output limits, SQLite transaction/fencing logic,
evidence verification, and accidental secret retention.

The subprocess executor is explicitly not an OS sandbox. A report that merely
demonstrates that a trusted specification can execute its declared command is
not a vulnerability. Escapes from documented bounds, unintended inheritance of
sensitive environment data, stale-worker publication, or evidence verification
bypasses are in scope.

See [docs/security-model.md](docs/security-model.md) for the complete trust model.

