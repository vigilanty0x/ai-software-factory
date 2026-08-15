from __future__ import annotations
from hashlib import sha256
import json
from typing import Any

PROJECT = "ai-software-factory"
REQUIRED_FIELDS = ["mission", "owner", "tests_passed", "tests_total"]
RULE = "mission ownership must be explicit and all tests must pass"

def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

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
        return isinstance(record["owner"], str) and bool(record["owner"].strip()) and isinstance(record["tests_total"], int) and record["tests_total"] > 0 and record["tests_passed"] == record["tests_total"]
    if kind == "mesh":
        return isinstance(record["agent_count"], int) and record["agent_count"] > 0 and record["healthy_agents"] == record["agent_count"] and isinstance(record["route_count"], int) and record["route_count"] > 0
    return False

def evaluate(record: dict[str, Any]) -> dict[str, Any]:
    missing = [field for field in REQUIRED_FIELDS if field not in record]
    status = "blocked" if missing else ("passed" if _valid(record) else "failed")
    reason = ("missing required fields: " + ", ".join(missing)) if missing else RULE
    evidence = {"project": PROJECT, "status": status, "reason": reason, "record": record}
    evidence["evidence_sha256"] = sha256(_canonical(evidence).encode()).hexdigest()
    return evidence

