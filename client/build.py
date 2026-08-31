"""Build the Vite client app and copy the dist into api/static/client/.

Run from the project root:
    python client/build.py
"""
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLIENT = ROOT / "client"
DIST = CLIENT / "dist"
TARGET = ROOT / "api" / "static" / "client"


def _npm_exe() -> str:
    for name in ("npm.cmd", "npm.exe", "npm"):
        for p_dir in (
            Path(r"C:\Program Files\nodejs"),
            Path(r"C:\Program Files (x86)\nodejs"),
        ):
            cand = p_dir / name
            if cand.exists():
                return str(cand)
    return "npm"


def main() -> int:
    if not DIST.exists():
        # First-time: install
        print("installing client dependencies...")
        proc = subprocess.run(
            [_npm_exe(), "install"],
            cwd=CLIENT, capture_output=True, text=True,
        )
        if proc.returncode != 0:
            print("INSTALL FAILED")
            print(proc.stdout[-2000:])
            print(proc.stderr[-2000:])
            return proc.returncode

    print(f"building client in {CLIENT}...")
    proc = subprocess.run(
        [_npm_exe(), "run", "build"],
        cwd=CLIENT, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        print("BUILD FAILED")
        print(proc.stdout[-2000:])
        print(proc.stderr[-2000:])
        return proc.returncode
    if not DIST.exists():
        print(f"expected {DIST} but it's missing")
        return 1

    if TARGET.exists():
        shutil.rmtree(TARGET)
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(DIST, TARGET)
    print(f"copied to {TARGET}")
    print("now start the API: python scripts/run_web.py  →  http://127.0.0.1:8000/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
