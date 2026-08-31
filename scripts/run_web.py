"""Run the Kateb web server.

Convenience wrapper that adds the project root to sys.path and starts
uvicorn. Use this instead of `python -m api.main` if you don't want to
install the package first.

    python scripts/run_web.py
    python scripts/run_web.py --port 9000 --host 0.0.0.0
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true", help="Reload on code changes")
    args = parser.parse_args()

    import uvicorn
    from api.main import app  # noqa: F401  (import to register routes)

    uvicorn.run(
        "api.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
