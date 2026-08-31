"""Apply `db/schema.sql` to the configured Supabase project.

Connects with the service-role key and runs the SQL through the
PostgREST management API. This works for the initial setup; for schema
migrations later, use `supabase db push` from the Supabase CLI instead.

Usage:
    python scripts/init_db.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

import httpx  # noqa: E402

SQL_PATH = ROOT / "db" / "schema.sql"


def main() -> int:
    import os
    url = os.environ["SUPABASE_URL"].rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ["SUPABASE_SECRET_KEY"]
    project_ref = os.environ.get("SUPABASE_PROJECT_REF") or url.split("//", 1)[-1].split(".", 1)[0]
    sql = SQL_PATH.read_text(encoding="utf-8")

    # The Supabase REST API doesn't expose DDL. We use the platform's
    # direct database connection over the pg endpoint if available, else
    # we print the SQL and tell the user to paste it into the SQL editor.
    db_url = f"https://{project_ref}.supabase.co/rest/v1/rpc/exec_sql"

    # Try a simple RPC roundtrip — if the user has set up an `exec_sql`
    # RPC on their project, this will run the schema. Otherwise, fall back
    # to the SQL editor instructions.
    try:
        r = httpx.post(
            db_url,
            headers={
                "apikey": key,
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            json={"sql": sql},
            timeout=30.0,
        )
        if r.status_code == 200:
            print("✅ schema applied via exec_sql RPC")
            return 0
        print(f"exec_sql RPC returned {r.status_code}; falling back to manual instructions.")
    except Exception as e:  # noqa: BLE001
        print(f"Could not reach exec_sql RPC: {e}")

    # Manual fallback — print where to paste the SQL.
    print("\n" + "=" * 70)
    print("MANUAL STEP REQUIRED")
    print("=" * 70)
    print(
        f"\n1. Open the Supabase SQL editor for project {project_ref}:\n"
        f"   https://supabase.com/dashboard/project/{project_ref}/sql/new\n"
        f"\n2. Paste the contents of:\n   {SQL_PATH}\n"
        f"\n3. Click 'Run'.\n"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
