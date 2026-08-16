from __future__ import annotations
from hashlib import sha256
import hmac
import json
from typing import Any

PROJECT = "ai-software-factory"
REQUIRED_FIELDS = ["mission", "owner", "tests_passed", "tests_total"]
RULE = "mission ownership must be explicit and all tests must pass"

def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )

def _valid(record: dict[str, Any]) -> bool:
    kind = "factory"
    if kind == "counts":
        total_key = "service_count" if "service_count" in record else "check_count"
        return isinstance(record[total_key], int) and record[total_key] > 0 and record["healthy_count"] == record[total_key]
    if kind == "port":
        return isinstance(record["port"], int) and 0 < record["port"] < 65536 and isinstance(record["owners"], list) and len(record["owners"]) <= 1 and record["conflict"] is False
    if kind == "digest":
        return all(isinstance(record[key], str) and record[key].startswith("sha256:") for key in ("expected_digest", "actual_digest")) and record["expected_digest"] == record["actual_digest"]
    if kind == "ready":
        return record["status"] == "ready" and all(isinstance(record[key], str) and record[key].strip() for key in ("runtime", "model"))
    if kind == "fleet":
        return isinstance(record["node_count"], int) and record["node_count"] > 0 and record["ready_nodes"] == record["node_count"]
    if kind == "benchmark":
        return isinstance(record["tokens_per_second"], (int, float)) and record["tokens_per_second"] > 0 and isinstance(record["latency_ms"], (int, float)) and 0 <= record["latency_ms"] <= 60000
    if kind == "embedding":
        return isinstance(record["dimensions"], int) and record["dimensions"] > 0 and isinstance(record["vector_count"], int) and record["vector_count"] > 0
    if kind == "corpus":
        return isinstance(record["documents"], int) and record["documents"] > 0 and record["indexed"] == record["documents"] and record["duplicates"] == 0
    if kind == "factory":
        return (
            isinstance(record["mission"], str)
            and bool(record["mission"].strip())
            and isinstance(record["owner"], str)
            and bool(record["owner"].strip())
            and type(record["tests_total"]) is int
            and record["tests_total"] > 0
            and type(record["tests_passed"]) is int
            and record["tests_passed"] == record["tests_total"]
        )
    if kind == "mesh":
        return isinstance(record["agent_count"], int) and record["agent_count"] > 0 and record["healthy_agents"] == record["agent_count"] and isinstance(record["route_count"], int) and record["route_count"] > 0
    return False

def evaluate(record: dict[str, Any]) -> dict[str, Any]:
    try:
        frozen_record = json.loads(_canonical(record))
    except (TypeError, ValueError):
        frozen_record = {}
        status = "blocked"
        reason = "record must be a finite JSON object"
    else:
        if not isinstance(frozen_record, dict):
            frozen_record = {}
            status = "blocked"
            reason = "record must be a JSON object"
        else:
            missing = [field for field in REQUIRED_FIELDS if field not in frozen_record]
            status = "blocked" if missing else ("passed" if _valid(frozen_record) else "failed")
            reason = ("missing required fields: " + ", ".join(missing)) if missing else RULE
    evidence = {
        "project": PROJECT,
        "status": status,
        "reason": reason,
        "record": frozen_record,
    }
    evidence["evidence_sha256"] = sha256(_canonical(evidence).encode()).hexdigest()
    return evidence


def verify_evidence(evidence: dict[str, Any]) -> bool:
    """Verify the legacy evidence envelope without trusting mutable aliases."""

    expected = evidence.get("evidence_sha256")
    if not isinstance(expected, str) or len(expected) != 64:
        return False
    if set(evidence) != {"project", "status", "reason", "record", "evidence_sha256"}:
        return False
    material = {key: value for key, value in evidence.items() if key != "evidence_sha256"}
    try:
        calculated = sha256(_canonical(material).encode()).hexdigest()
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(calculated, expected)
