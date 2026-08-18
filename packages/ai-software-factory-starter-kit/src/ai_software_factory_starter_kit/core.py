from __future__ import annotations

from datetime import datetime
from hashlib import sha256
import json
import re
from typing import Any

PROJECT = "ai-software-factory-starter-kit"
REQUIRED_FIELDS = ("spec", "ownership", "tests", "evidence", "review", "release")
MAX_INPUT_BYTES = 131_072
SHA256 = re.compile(r"[0-9a-f]{64}")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _text(value: Any, limit: int = 300) -> bool:
    return isinstance(value, str) and 0 < len(value.strip()) <= limit and not any(ord(c) < 32 or ord(c) == 127 for c in value)


def _digest(value: Any) -> bool:
    return isinstance(value, str) and bool(SHA256.fullmatch(value))


def _instant(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError("evidence timestamps must be timezone-aware ISO-8601")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("evidence timestamps must be timezone-aware ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("evidence timestamps must include an offset")
    return parsed.isoformat()


def build_factory_manifest(record: dict[str, Any]) -> dict[str, Any]:
    if not _text(record.get("spec"), 500):
        raise ValueError("spec must be a bounded reference")
    ownership = record.get("ownership")
    if not isinstance(ownership, list) or not 2 <= len(ownership) <= 100:
        raise ValueError("ownership must map at least two agents to worktrees")
    agents: set[str] = set()
    worktrees: set[str] = set()
    for item in ownership:
        if not isinstance(item, dict) or set(item) != {"agent", "worktree"} or not _text(item["agent"]) or not _text(item["worktree"], 500) or item["agent"] in agents or item["worktree"] in worktrees:
            raise ValueError("agent-to-worktree ownership must be explicit and one-to-one")
        agents.add(item["agent"])
        worktrees.add(item["worktree"])
    tests = record.get("tests")
    if not isinstance(tests, dict) or set(tests) != {"passed", "total", "sha256"} or any(not isinstance(tests[key], int) or isinstance(tests[key], bool) for key in ("passed", "total")) or not 1 <= tests["total"] <= 10_000_000 or tests["passed"] != tests["total"] or not _digest(tests["sha256"]):
        raise ValueError("tests require equal bounded pass/total counts and an exact digest")
    evidence = record.get("evidence")
    if not isinstance(evidence, list) or not 2 <= len(evidence) <= 100:
        raise ValueError("structured artifact and test evidence records are required")
    kinds: dict[str, dict[str, Any]] = {}
    for item in evidence:
        if not isinstance(item, dict) or set(item) != {"kind", "sha256", "issuer", "issued_at"} or item["kind"] not in {"artifact", "test", "build", "provenance"} or item["kind"] in kinds or not _digest(item["sha256"]) or not _text(item["issuer"]):
            raise ValueError("evidence records require unique allowlisted kinds, digests, issuers, and timestamps")
        _instant(item["issued_at"])
        kinds[item["kind"]] = item
    if "artifact" not in kinds or "test" not in kinds or kinds["test"]["sha256"] != tests["sha256"]:
        raise ValueError("artifact evidence and test evidence matching the test receipt are required")
    review = record.get("review")
    if not isinstance(review, dict) or set(review) != {"reviewer", "subject_sha256", "decision", "issued_at"} or not _text(review["reviewer"]) or review["reviewer"] in agents or not _digest(review["subject_sha256"]) or review["subject_sha256"] != kinds["artifact"]["sha256"] or review["decision"] != "approved":
        raise ValueError("an independent approved review of the artifact digest is required")
    _instant(review["issued_at"])
    review_digest = sha256(_canonical(review).encode()).hexdigest()
    release = record.get("release")
    if not isinstance(release, dict) or set(release) != {"artifact_sha256", "review_sha256", "issuer", "issued_at"} or release["artifact_sha256"] != kinds["artifact"]["sha256"] or release["review_sha256"] != review_digest or not _text(release["issuer"]):
        raise ValueError("release receipt must bind the artifact and independent review digests")
    _instant(release["issued_at"])
    return {"spec": record["spec"], "ownership": ownership, "stages": ["spec", "ownership", "tests", "independent-review", "release-receipt"], "tests": tests, "evidence": evidence, "review": {**review, "sha256": review_digest}, "release": release, "trust_state": "internally-consistent-declared-evidence", "independently_verified_by_tool": False}


def evaluate(record: Any) -> dict[str, Any]:
    artifact: Any = None
    safe_record = None
    try:
        if not isinstance(record, dict):
            raise ValueError("record must be a JSON object")
        if len(_canonical(record).encode()) > MAX_INPUT_BYTES:
            raise ValueError("record exceeds 131072 bytes")
        safe_record = record
        missing = [field for field in REQUIRED_FIELDS if field not in record]
        if missing:
            status, reason = "blocked", "missing required fields: " + ", ".join(missing)
        else:
            artifact = build_factory_manifest(record)
            status, reason = "passed", "declared evidence is internally consistent; external trust was not established"
    except (TypeError, ValueError, KeyError, OverflowError) as exc:
        status, reason = "failed", str(exc)
    receipt = {"project": PROJECT, "status": status, "reason": reason, "record": safe_record, "factory_manifest": artifact}
    receipt["evidence_sha256"] = sha256(_canonical(receipt).encode()).hexdigest()
    return receipt
