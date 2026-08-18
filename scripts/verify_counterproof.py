"""Exercise the installed legacy Factory gate through its CLI."""
from __future__ import annotations
import json
from pathlib import Path
import subprocess
import sys
import tempfile

CASES = (
    ("positive", {"mission": "synthetic", "owner": "ci", "tests_passed": 2, "tests_total": 2}, 0, "passed"),
    ("negative", {"mission": "synthetic", "owner": "ci", "tests_passed": 1, "tests_total": 2}, 2, "failed"),
    ("blocked", {"mission": "synthetic", "tests_passed": 2, "tests_total": 2}, 2, "blocked"),
)

def main() -> int:
    with tempfile.TemporaryDirectory(prefix="factory-counterproof-") as tmp:
        root = Path(tmp)
        for name, record, expected_code, expected_status in CASES:
            path = root / f"{name}.json"
            path.write_text(json.dumps(record), encoding="utf-8")
            result = subprocess.run([sys.executable, "-m", "ai_software_factory.cli", str(path)], capture_output=True, text=True, check=False)
            if result.returncode != expected_code:
                raise SystemExit(f"{name}: expected exit {expected_code}, got {result.returncode}: {result.stderr}")
            try:
                evidence = json.loads(result.stdout)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"{name}: CLI did not emit JSON") from exc
            if evidence.get("status") != expected_status:
                raise SystemExit(f"{name}: expected status {expected_status}, got {evidence.get('status')}")
    print("factory positive/counter-proof contract verified")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
