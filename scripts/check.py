from __future__ import annotations

import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
SKIP_PARTS = {"build", "dist", "__pycache__", ".venv", ".pytest_cache", ".mypy_cache"}
TEXT_SUFFIXES = {".py", ".md", ".toml", ".yml", ".yaml", ".json"}
README_HEADINGS = (
    "## Purpose", "## Non-goals", "## Install", "## API", "## CLI",
    "## Example", "## Security", "## Limits", "## Tests",
    "## AI assistance", "## License",
)
ACTION_PINS = (
    "actions/checkout@11d5960a326750d5838078e36cf38b85af677262",
    "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065",
)
SECRET_PATTERNS = (
    re.compile("-----BEGIN " + "(?:RSA |EC |OPENSSH )?" + "PRIVATE KEY-----"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"(?i)\b(?:password|passwd|api[_-]?key|secret|token)\b\s*[:=]\s*['\"]?[A-Za-z0-9_./+=-]{20,}"),
    re.compile(r"https?://[^\s/@:]+:[^\s/@]+@"),
)


def main() -> int:
    problems: list[str] = []
    for path in ROOT.rglob("*"):
        if path.is_symlink():
            problems.append(f"{path.relative_to(ROOT)}: symlink is not allowed in public package")
            continue
        if not path.is_file() or any(part in SKIP_PARTS or part.endswith(".egg-info") for part in path.parts):
            continue
        if path.suffix not in TEXT_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeError as exc:
            problems.append(f"{path.relative_to(ROOT)}: invalid UTF-8: {exc}")
            continue
        relative = path.relative_to(ROOT)
        if relative != Path("scripts/check.py"):
            if ("sky" + "om").casefold() in text.casefold():
                problems.append(f"{relative}: non-public product marker")
            if "/workspace/" + "scratch/" in text or "/home/" in text:
                problems.append(f"{relative}: local absolute path")
            for pattern in SECRET_PATTERNS:
                if pattern.search(text):
                    problems.append(f"{relative}: possible credential material")
                    break
        if path.suffix == ".json":
            try:
                json.loads(text, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
            except (json.JSONDecodeError, ValueError) as exc:
                problems.append(f"{relative}: invalid strict JSON: {exc}")

    required = ("README.md", "LICENSE", "SECURITY.md", "AI_ASSISTANCE.md", "pyproject.toml", ".github/workflows/ci.yml", "examples/valid.json")
    for name in required:
        if not (ROOT / name).is_file():
            problems.append(f"missing {name}")

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for heading in README_HEADINGS:
        if heading not in readme:
            problems.append(f"README.md: missing {heading}")

    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    if "\\n" in ignore:
        problems.append(".gitignore: contains a literal backslash-n sequence")
    for entry in ("__pycache__/", "*.py[cod]", "*.egg-info/", "build/", "dist/", ".venv/"):
        if entry not in ignore.splitlines():
            problems.append(f".gitignore: missing {entry}")

    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    for pin in ACTION_PINS:
        if pin not in workflow:
            problems.append(f"ci.yml: missing immutable action pin {pin}")
    for required_text in ("contents: read", "timeout-minutes:", '"3.11"', '"3.12"', "python -m build --no-isolation", "unittest discover", "scripts/check.py", "examples/valid.json"):
        if required_text not in workflow:
            problems.append(f"ci.yml: missing {required_text}")
    if re.search(r"uses:\s*actions/(?:checkout|setup-python)@v\d", workflow):
        problems.append("ci.yml: mutable action tag is forbidden")

    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1
    print("public-boundary and repository checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

