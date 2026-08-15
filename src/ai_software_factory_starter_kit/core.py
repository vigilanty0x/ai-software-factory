from __future__ import annotations
from datetime import datetime
from hashlib import sha256
import json
import re
from typing import Any

PROJECT = "ai-software-factory-starter-kit"
REQUIRED_FIELDS = ["spec","agents","worktrees","tests_passed","tests_total","evidence"]

def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())

def _string_list(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(_text(item) for item in value)

def _integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)

def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)

def build_factory_manifest(record: dict[str, Any]) -> dict[str, Any]:
    if not _text(record["spec"]) or not _string_list(record["agents"]) or len(record["agents"]) < 2 or len(record["agents"]) != len(set(record["agents"])):
        raise ValueError("at least two unique agents are required")
    if not _string_list(record["worktrees"]) or len(record["worktrees"]) != len(set(record["worktrees"])):
        raise ValueError("unique worktrees are required")
    if not _integer(record["tests_passed"]) or not _integer(record["tests_total"]) or record["tests_total"] <= 0 or record["tests_passed"] != record["tests_total"]:
        raise ValueError("all tests must pass")
    if not _string_list(record["evidence"]):
        raise ValueError("release evidence is required")
    return {"spec": record["spec"], "owners": record["agents"], "worktrees": record["worktrees"], "stages": ["spec", "agents", "worktrees", "tests", "review", "evidence", "release"], "tests": {"passed": record["tests_passed"], "total": record["tests_total"]}, "evidence": record["evidence"]}

def evaluate(record: dict[str, Any]) -> dict[str, Any]:
    missing = [field for field in REQUIRED_FIELDS if field not in record]
    artifact: Any = None
    if missing:
        status = "blocked"
        reason = "missing required fields: " + ", ".join(missing)
    else:
        try:
            artifact = build_factory_manifest(record)
            status = "passed"
            reason = "build_factory_manifest completed"
        except (TypeError, ValueError, KeyError) as exc:
            status = "failed"
            reason = str(exc)
    receipt = {"project": PROJECT, "status": status, "reason": reason, "record": record, "factory_manifest": artifact}
    receipt["evidence_sha256"] = sha256(_canonical(receipt).encode()).hexdigest()
    return receipt

