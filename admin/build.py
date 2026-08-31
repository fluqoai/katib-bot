"""Build the Vite admin dashboard and copy the dist into api/static/admin/.

Run from the project root:
    python admin/build.py
"""
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ADMIN = ROOT / "admin"
DIST = ADMIN / "dist"
TARGET = ROOT / "api" / "static" / "admin"


def _npm_exe() -> str:
    """Locate the npm executable (works even when shell PATH is odd)."""
    for name in ("npm.cmd", "npm.exe", "npm"):
        for p_dir in (
            Path(r"C:\Program Files\nodejs"),
            Path(r"C:\Program Files (x86)\nodejs"),
        ):
            cand = p_dir / name
            if cand.exists():
                return str(cand)
    return "npm"  # fall through to PATH


def main() -> int:
    print(f"building dashboard in {ADMIN}...")
    proc = subprocess.run(
        [_npm_exe(), "run", "build"],
        cwd=ADMIN, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        print("BUILD FAILED")
        print(proc.stdout[-2000:])
        print(proc.stderr[-2000:])
        return proc.returncode
    if not DIST.exists():
        print(f"expected {DIST} but it's missing")
        return 1

    # Wipe and copy
    if TARGET.exists():
        shutil.rmtree(TARGET)
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(DIST, TARGET)
    print(f"copied to {TARGET}")
    print("now start the API: python scripts/run_web.py  →  http://127.0.0.1:8000/admin")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
