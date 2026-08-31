"""FastAPI app — serves the chat page and the `/api/chat` endpoint.

Start with:
    python scripts/run_web.py
or:
    uvicorn api.main:app --host 127.0.0.1 --port 8000 --reload
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

# Make the project root importable
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Load .env into os.environ so the legacy `os.environ["…"]` lookups
# in `rag.embeddings.from_env()` and similar keep working under uvicorn.
from dotenv import load_dotenv  # noqa: E402
load_dotenv(ROOT / ".env")

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from api.admin import router as admin_router  # noqa: E402
from api.letters import router as letters_router  # noqa: E402
from bot.config import get_settings  # noqa: E402
from rag.embeddings import from_env as emb_from_env  # noqa: E402
from rag.generator import from_env as gen_from_env  # noqa: E402
from rag.search import from_env as search_from_env  # noqa: E402


logger = logging.getLogger(__name__)


# --- App ---------------------------------------------------------------------

app = FastAPI(
    title="Kateb",
    description="Arabic writing assistant — REST + static chat page.",
    version="0.1.0",
)


# --- CORS -------------------------------------------------------------------
# When the frontend is hosted on Vercel and the backend on a VPS, the
# browser will send cross-origin requests from katibai.xyz to
# api.katibai.xyz. CORS_ORIGINS is a comma-separated list of allowed
# origins. In production, set:
#   CORS_ORIGINS=https://katibai.xyz,https://www.katibai.xyz
# During local development, the Vite dev server runs on a different
# origin (localhost:5175) so we include it by default.
_cors_origins_raw = os.environ.get("CORS_ORIGINS", "http://localhost:5175,http://127.0.0.1:5175")
_cors_origins = [o.strip() for o in _cors_origins_raw.split(",") if o.strip()]
# Vercel preview deployments use random subdomains; allow the
# `vercel.app` origin pattern by also adding a wildcard origin
# (credentials must be disabled for the wildcard to work, so we
# split: explicit origins with credentials, plus an open origin
# pattern for vercel.app previews).
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_origin_regex=r"https://.*\.vercel\.app" if any("vercel" in o for o in _cors_origins) else None,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
    max_age=600,
)
# The os import (needed for the CORS env var above) is also used
# elsewhere in the module — keep the import at the top.


# Lazy-initialized components (so missing creds fail on first request, not on import)
_state: dict = {}


def _get_components():
    """Load the RAG stack on first call and cache it."""
    if "searcher" not in _state:
        client, searcher = search_from_env()
        embedder = emb_from_env()
        generator = gen_from_env()
        _state["client"] = client
        _state["searcher"] = searcher
        _state["embedder"] = embedder
        _state["generator"] = generator
    return _state["searcher"], _state["generator"]


# --- Schemas ----------------------------------------------------------------

class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, description="Arabic natural-language request")
    type: str | None = Field(None, description="Optional document type hint")


class SourceOut(BaseModel):
    document_id: str | None = None
    title: str | None = None
    category: str | None = None
    contribution: str | None = None


class ChatResponse(BaseModel):
    draft_markdown: str
    sources: list[SourceOut] = []
    status: str = "ok"
    retrieved_count: int = 0


class HealthResponse(BaseModel):
    status: str
    supabase_url: str
    embedding_model: str
    embedding_dim: int
    llm_model: str


# --- Routes -----------------------------------------------------------------

@app.get("/api/health", response_model=HealthResponse)
async def health():
    settings = get_settings()
    embedder = _state.get("embedder")
    return HealthResponse(
        status="ok",
        supabase_url=settings.supabase_url,
        embedding_model=(
            f"{embedder.provider}/{embedder.model}" if embedder else "(not initialised)"
        ),
        embedding_dim=embedder.dimension if embedder else 0,
        llm_model=settings.openrouter_model,
    )


@app.post("/api/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    if not req.message.strip():
        raise HTTPException(400, "Empty message")

    try:
        searcher, generator = _get_components()
    except Exception as e:  # noqa: BLE001
        logger.exception("Failed to initialise RAG components")
        raise HTTPException(503, f"Backend not ready: {e}") from e

    try:
        ctx = await searcher.search(req.message)
        draft = await generator.generate(req.message, ctx)
    except Exception as e:  # noqa: BLE001
        logger.exception("Generation failed")
        raise HTTPException(500, f"Generation failed: {e}") from e

    sources = [
        SourceOut(
            document_id=s.get("document_id"),
            title=s.get("title"),
            category=s.get("category"),
            contribution=s.get("contribution"),
        )
        for s in (draft.sources or [])
    ]
    return ChatResponse(
        draft_markdown=draft.body or "_(لم يتم إنشاء مسودة)_",
        sources=sources,
        status="ok" if draft.body else "empty",
        retrieved_count=ctx.total,
    )


# --- Static files (the chat page) -------------------------------------------

STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)


# Legacy chat page (moved to /chat so the root URL can serve the new
# client UI). If the old index.html is present, it's served at /chat.
@app.get("/chat")
async def chat_page():
    index = STATIC_DIR / "index.html"
    if not index.exists():
        raise HTTPException(404, f"index.html not found at {index}")
    return FileResponse(str(index))


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Admin API (upload, list, reprocess, deactivate, delete) — token-gated
app.include_router(admin_router)

# Letter-generation pipeline (multi-stage: intent → retrieval →
# evidence → draft → compliance → export)
app.include_router(letters_router)


# --- Admin dashboard (Vite build output) ---------------------------------

ADMIN_DIST = Path(__file__).parent / "static" / "admin"
if ADMIN_DIST.exists():
    # Serve the built SPA at /admin
    app.mount(
        "/admin",
        StaticFiles(directory=str(ADMIN_DIST), html=True),
        name="admin-dashboard",
    )
    logger.info("Admin dashboard mounted at /admin  (build: %s)", ADMIN_DIST)
else:
    logger.info(
        "Admin dashboard NOT built (looked for %s). "
        "Run `cd admin && npm run build` to enable /admin.",
        ADMIN_DIST,
    )


# --- Client app (Vite build output) ---------------------------------------

CLIENT_DIST = Path(__file__).parent / "static" / "client"

if CLIENT_DIST.exists():
    # Serve the built SPA at the root using a single catch-all route.
    # A mount() would intercept everything and bypass the SPA fallback;
    # this route serves real files (JS, CSS, favicon, ...) and falls
    # back to index.html for any unknown path so client-side routing
    # works on page refresh.
    @app.get("/{full_path:path}", include_in_schema=False)
    async def client_spa(full_path: str):
        # Never serve SPA files for API / admin / static / chat paths
        if full_path.startswith(("api/", "admin/", "static/", "chat")):
            raise HTTPException(404)
        # Try to serve the requested file first
        target = (CLIENT_DIST / full_path).resolve()
        try:
            target.relative_to(CLIENT_DIST.resolve())  # path-traversal guard
        except ValueError:
            raise HTTPException(404)
        if target.is_file():
            return FileResponse(str(target))
        # Otherwise serve the SPA index (root or unknown route)
        index = CLIENT_DIST / "index.html"
        if not index.exists():
            raise HTTPException(404, "client app index not found")
        return FileResponse(str(index))

    logger.info("Client app served at /  (build: %s)", CLIENT_DIST)
else:
    # Fallback: if the client isn't built, fall back to the legacy
    # chat page so the root URL still serves something useful.
    @app.get("/")
    async def root_fallback():
        index = STATIC_DIR / "index.html"
        if not index.exists():
            raise HTTPException(
                404,
                f"client app not built (looked for {CLIENT_DIST}) "
                f"and no legacy index.html at {index}",
            )
        return FileResponse(str(index))
    logger.info(
        "Client app NOT built (looked for %s). "
        "Run `python client/build.py` to enable /.",
        CLIENT_DIST,
    )


# --- Entrypoint (for `python -m api.main`) ----------------------------------

def main() -> None:
    import uvicorn
    settings = get_settings()
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
    )
    uvicorn.run(
        "api.main:app",
        host="127.0.0.1",
        port=8000,
        reload=False,
    )


if __name__ == "__main__":
    main()
