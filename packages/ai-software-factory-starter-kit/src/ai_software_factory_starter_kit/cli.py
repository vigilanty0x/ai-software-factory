from __future__ import annotations
import argparse
import json
from pathlib import Path
from .core import evaluate

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate internally consistent declared delivery evidence and ownership.")
    parser.add_argument("record", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        result = evaluate(json.loads(args.record.read_text(encoding="utf-8")))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        result = {"project": parser.prog, "status": "failed", "reason": f"invalid input file: {exc}", "factory_manifest": None}
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0 if result["status"] == "passed" else 2

if __name__ == "__main__":
    raise SystemExit(main())
