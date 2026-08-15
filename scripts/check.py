from __future__ import annotations
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = ("BEGIN " + "PRIVATE KEY", "gh" + "p_", "api" + "_key=", "pass" + "word=", "/workspace/" + "scratch/")

def main() -> int:
    problems: list[str] = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or any(part in {"build", "dist", "__pycache__"} or part.endswith(".egg-info") for part in path.parts):
            continue
        if path.suffix not in {".py", ".md", ".toml", ".yml", ".yaml", ".json"}:
            continue
        text = path.read_text(encoding="utf-8")
        if any(marker.casefold() in text.casefold() for marker in FORBIDDEN):
            problems.append(f"{path.relative_to(ROOT)}: public-boundary marker")
        if path.suffix == ".json":
            try:
                json.loads(text)
            except json.JSONDecodeError as exc:
                problems.append(f"{path.relative_to(ROOT)}: {exc}")
    for required in ("README.md", "LICENSE", "SECURITY.md", "AI_ASSISTANCE.md", "pyproject.toml", ".github/workflows/ci.yml"):
        if not (ROOT / required).is_file():
            problems.append(f"missing {required}")
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1
    print("public-boundary and repository checks passed")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())

