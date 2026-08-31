"""Dev runner: starts the FastAPI server + the async worker in one shell.

Production setup
================
- A long-running `python scripts/worker.py` process for processing
- A long-running `python scripts/run_web.py` (uvicorn) for the dashboard
  + chat page
- Optionally: a Supabase Edge Function for storage-webhook triggers

This script just starts both in subprocesses so dev iterations are easy.
"""
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _python_exe() -> str:
    return sys.executable


def main() -> int:
    procs = []

    def _spawn(name: str, args: list[str]) -> subprocess.Popen:
        print(f"starting {name}…", flush=True)
        return subprocess.Popen([_python_exe()] + args, cwd=ROOT)

    procs.append(_spawn("worker", ["scripts/worker.py", "--poll-interval", "5", "--chunk-budget", "150"]))
    procs.append(_spawn("web",    ["scripts/run_web.py"]))

    def _shutdown(*_):
        print("\nshutting down…", flush=True)
        for p in procs:
            try:
                p.terminate()
            except Exception:
                pass
        for p in procs:
            try:
                p.wait(timeout=10)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    print("\nKateb is up.")
    print("  chat page:  http://127.0.0.1:8000/")
    print("  dashboard:  http://127.0.0.1:8000/admin")
    print("  api health: http://127.0.0.1:8000/api/health")
    print("  ctrl-c to stop\n", flush=True)

    # Stream their stdout/stderr
    while True:
        for p in procs:
            if p.poll() is not None:
                print(f"[{p.args[1] if len(p.args) > 1 else 'proc'}] exited with {p.returncode}")
                _shutdown()
        time.sleep(1)


if __name__ == "__main__":
    raise SystemExit(main())
