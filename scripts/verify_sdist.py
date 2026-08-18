"""Safely extract and test one AI Software Factory source distribution."""
from __future__ import annotations
import argparse
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile

REQUIRED = {"/.github/workflows/ci.yml", "/MIGRATION-1.0.md", "/release-policy.v1.json", "/requirements-build.txt", "/docs/release.md", "/scripts/build_release_evidence.py", "/scripts/check_release_policy.py", "/examples/factory.json"}

class SdistError(ValueError):
    pass

def verify_sdist(archive: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="factory-sdist-") as tmp:
        destination = Path(tmp).resolve()
        with tarfile.open(archive, "r:gz") as bundle:
            members = bundle.getmembers(); names = {member.name for member in members}
            missing = [suffix for suffix in REQUIRED if not any(name.endswith(suffix) for name in names)]
            if missing: raise SdistError(f"incomplete sdist: {missing!r}")
            for member in members:
                target = (destination / member.name).resolve()
                if member.issym() or member.islnk() or not target.is_relative_to(destination):
                    raise SdistError(f"unsafe sdist path: {member.name}")
            bundle.extractall(destination, members=members)
        roots = [path for path in destination.iterdir() if path.is_dir()]
        if len(roots) != 1: raise SdistError("sdist must contain one top-level directory")
        root = roots[0]; env = dict(os.environ); env["PYTHONPATH"] = str(root / "src")
        subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"], cwd=root, env=env, check=True)
        subprocess.run([sys.executable, "scripts/check_release_policy.py"], cwd=root, env=env, check=True)

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("archive", type=Path); args = parser.parse_args(argv)
    try: verify_sdist(args.archive)
    except (OSError, tarfile.TarError, subprocess.CalledProcessError, SdistError) as exc: raise SystemExit(f"sdist verification: {exc}") from exc
    print(f"sdist verified: {args.archive.name}"); return 0

if __name__ == "__main__": raise SystemExit(main())
