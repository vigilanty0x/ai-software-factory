"""Validate the AI Software Factory 1.0 prepared release policy."""
from __future__ import annotations
import json
from pathlib import Path
import tomllib

ROOT = Path(__file__).resolve().parents[1]

class ReleasePolicyError(ValueError):
    pass

def validate_release_policy(root: Path = ROOT) -> dict:
    root = Path(root)
    try:
        policy = json.loads((root / "release-policy.v1.json").read_text(encoding="utf-8"))
        project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
        workflow = (root / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        migration = (root / "MIGRATION-1.0.md").read_text(encoding="utf-8")
    except (OSError, UnicodeError, json.JSONDecodeError, tomllib.TOMLDecodeError, KeyError, TypeError) as exc:
        raise ReleasePolicyError("cannot read release policy inputs") from exc
    expected = {
        "schema_version": "1.0", "repository": "vigilanty0x/ai-software-factory",
        "product": "AI Software Factory", "distribution": "ai-software-factory",
        "version": "1.0.0", "proposed_tag": "v1.0.0", "state": "PREPARED",
        "publish_enabled": False, "rollback_version": "0.1.0",
    }
    for field, value in expected.items():
        if policy.get(field) != value:
            raise ReleasePolicyError(f"{field} must equal {value!r}")
    if project.get("name") != policy["distribution"] or project.get("version") != policy["version"]:
        raise ReleasePolicyError("pyproject identity/version does not match release policy")
    required = policy.get("requires")
    if not isinstance(required, list) or len(required) != len(set(required)) or len(required) < 9:
        raise ReleasePolicyError("release policy requires must be a unique complete gate list")
    for gate in ("multi_os_runtime_ci", "wheel_and_sdist", "installed_artifact_smoke", "positive_and_counterproof", "cyclonedx_sbom", "verified_slsa_provenance", "consumer_compatibility", "explicit_publication_decision", "post_publication_verification"):
        if gate not in required:
            raise ReleasePolicyError(f"release policy is missing gate {gate}")
    for marker in ("publish-release:", "gh release create", "contents: write", "git tag", "twine upload", "pypa/gh-action-pypi-publish"):
        if marker in workflow:
            raise ReleasePolicyError(f"publication is disabled but CI contains publication authority {marker!r}")
    if "Rollback" not in migration or "0.1.0" not in migration:
        raise ReleasePolicyError("migration guide must contain explicit 0.1.0 Rollback")
    return policy

def main() -> int:
    try:
        policy = validate_release_policy()
    except ReleasePolicyError as exc:
        raise SystemExit(f"release policy gate: {exc}") from exc
    print(f"release policy verified: version={policy['version']} state={policy['state']} publish_enabled=false rollback={policy['rollback_version']}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
