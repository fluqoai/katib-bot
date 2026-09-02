"""Letter-generation REST API.

POST /api/letters/generate
  Body: { "request": "...", "fields": { "recipient_name": "..." }, "user_id": "..." }
  Returns: { "ok": true, "final_status": "ok"|"fixable"|"needs_review"|...,
             "needs_review": bool, "pdf_available": bool,
             "intent": {...}, "draft": {...}, "compliance": {...},
             "corrected_draft": {...}, "re_compliance": {...},
             "chunks_used": [...], "template_profile": {...},
             "clean_body": "...",
             "stages": [...],
             "docx_url": null|"<signed-url>",
             "pdf_url":  null|"<signed-url>",
             "draft_id": "<uuid>" }

The DOCX and PDF are uploaded to a `generated` bucket (private) when
the bucket exists; the dashboard can fetch them via the signed URLs
returned here. If the bucket is missing, the response just has
``docx_url=null``/``pdf_url=null`` — the client should fall back to
the direct-download endpoints below.

The final DOCX is OFFICIAL: it does NOT include the
``## المصادر`` section nor any ``[source: ...]`` tags. All
provenance lives in the ``drafts`` row and in the response
``chunks_used`` array.

POST /api/letters/generate/docx
POST /api/letters/generate/pdf
  Stream the file directly. ``/pdf`` returns 503 if soffice isn't
  installed (no DOCX-as-PDF fallback — the API never lies about PDF
  availability).

All endpoints are gated by the same ``X-Admin-Token`` as the rest of
the admin API.
"""
from __future__ import annotations

import base64
import io
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any, Optional

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from bot.config import get_settings
from rag.embeddings import from_env as emb_from_env
from rag.export import has_soffice
from rag.letter_pipeline import LetterPipeline
from rag.generator import from_env as gen_from_env
from rag.compliance import from_env as comp_from_env


logger = logging.getLogger(__name__)


router = APIRouter(prefix="/api/letters", tags=["letters"])


# -- auth -----------------------------------------------------------------

def _check_admin(x_admin_token: Optional[str]) -> None:
    expected = os.environ.get("ADMIN_TOKEN", "dev")
    if not x_admin_token or x_admin_token != expected:
        raise HTTPException(401, "invalid or missing X-Admin-Token")


# -- schemas --------------------------------------------------------------

class GenerateRequest(BaseModel):
    request: str = Field(..., min_length=3, description="Free-form Arabic letter request")
    fields: dict[str, str] = Field(
        default_factory=dict,
        description="Known placeholder values (e.g. {'recipient_name': 'وزارة الثقافة'})",
    )
    user_id: Optional[str] = None
    upload_outputs: bool = Field(
        default=True,
        description="If true, upload DOCX + PDF to the 'generated' bucket "
                    "and return signed URLs.",
    )
    # --- Phase 2: style-driven path (NEW, optional) ---
    use_legacy_template: bool = Field(
        default=False,
        description="If True, force the legacy template-driven export path "
                    "(requires a template to be present). If False (default), "
                    "use the style-driven path: the export builds a fresh DOCX "
                    "from LetterStyle + intent, independent of any template file.",
    )
    style_overrides: dict = Field(
        default_factory=dict,
        description="Optional per-request LetterStyle field overrides "
                    "(e.g. {'include_basmala': false, 'font_size_pt': 13.0}). "
                    "Applied on top of the per-intent default style.",
    )


# -- one-time pipeline construction ----------------------------------------

_pipeline_singleton: LetterPipeline | None = None


def _get_pipeline() -> LetterPipeline:
    global _pipeline_singleton
    if _pipeline_singleton is None:
        from supabase import create_client
        s = get_settings()
        sb = create_client(s.supabase_url, s.resolved_service_key())
        _pipeline_singleton = LetterPipeline(
            embedder=emb_from_env(),
            generator=gen_from_env(),
            reviewer=comp_from_env(),
            supabase=sb,
        )
    return _pipeline_singleton


def _ascii_only(name: str) -> str:
    import re
    p = PurePosixPath(name)
    suffix = p.suffix.lower() or ".bin"
    stem = re.sub(r"[^a-zA-Z0-9._-]+", "_", p.stem).strip("_") or "file"
    if len(stem) > 80:
        stem = stem[:80]
    return f"{stem}{suffix}"


def _try_upload_outputs(result, req: GenerateRequest) -> tuple[str | None, str | None]:
    """Best-effort upload of DOCX + PDF to the ``generated`` bucket.

    Returns ``(docx_url, pdf_url)``; either or both may be None if
    the bucket is missing or the upload fails. We never fail the
    request on upload errors — the response body still has the
    full draft + sources + provenance.
    """
    docx_url: str | None = None
    pdf_url:  str | None = None
    if not req.upload_outputs:
        return docx_url, pdf_url
    if not result.docx_bytes:
        return docx_url, pdf_url
    try:
        from supabase import create_client
        s = get_settings()
        sb = create_client(s.supabase_url, s.resolved_service_key())
        bucket = "generated"
        base = f"{uuid.uuid4()}"
        stem = _ascii_only(result.intent.letter_type_ar if result.intent else "draft")
        docx_path = f"{base}/{stem}.docx"
        sb.storage.from_(bucket).upload(
            docx_path, result.docx_bytes,
            {"content-type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
             "x-upsert": "false"},
        )
        docx_url = sb.storage.from_(bucket).create_signed_url(docx_path, 3600)["signedURL"]
        if result.pdf_bytes:
            pdf_path = f"{base}/{stem}.pdf"
            sb.storage.from_(bucket).upload(
                pdf_path, result.pdf_bytes,
                {"content-type": "application/pdf", "x-upsert": "false"},
            )
            pdf_url = sb.storage.from_(bucket).create_signed_url(pdf_path, 3600)["signedURL"]
    except Exception as e:  # noqa: BLE001
        logger.warning("upload of generated files failed (bucket likely missing): %s", e)
    return docx_url, pdf_url


def _draft_to_dict(d) -> dict[str, Any] | None:
    if d is None:
        return None
    return {
        "body":            d.body,
        "body_only":       d.body_only,
        "sources_block":   d.sources_block,
        "clean_body":      __import__("re").sub(r"\s*\[source:\s*[^\]]+\]", "", d.body_only or "").strip(),
        "claim_count":     d.claim_count(),
        "has_unverified":  d.has_unverified_claims(),
        "citations":       d.parsed_citations,
        "model":           d.model,
    }


def _compliance_to_dict(c) -> dict[str, Any] | None:
    if c is None:
        return None
    return {
        "verdict":                c.verdict,
        "unverified_claims":      c.unverified_claims,
        "dangling_citations":     c.dangling_citations,
        "placeholder_claims":     c.placeholder_claims,
        "duplicate_segments":     c.duplicate_segments,
        "incomplete_sentences":   c.incomplete_sentences,
        "unintended_placeholders":c.unintended_placeholders,
        "contradictions":         c.contradictions,
        "wrong_names":            c.wrong_names,
        "unsupported_facts":      c.unsupported_facts,
        "unprofessional_phrasing":c.unprofessional_phrasing,
        "suggested_corrections":  c.suggested_corrections,
        "summary_ar":             c.summary_ar,
    }


# -- route: /generate -----------------------------------------------------

@router.post("/generate")
async def generate(
    req: GenerateRequest,
    x_admin_token: Optional[str] = Header(default=None),
):
    _check_admin(x_admin_token)
    pipeline = _get_pipeline()
    result = await pipeline.run(
        req.request,
        known_fields=req.fields,
        user_id=req.user_id,
        # Phase 2: pass style + legacy flag through to the pipeline
        style_overrides=req.style_overrides or None,
        use_legacy_template=req.use_legacy_template,
    )

    docx_url, pdf_url = _try_upload_outputs(result, req)

    # Phase 4: deprecation log for legacy callers. The style-driven
    # path is the new default and is what production uses. Anyone who
    # explicitly opts into the legacy template path gets a warning
    # so we can track adoption and remove the legacy path in a future
    # release.
    if req.use_legacy_template:
        import logging
        logging.getLogger("kateb.api").warning(
            "DEPRECATED: use_legacy_template=True requested — this path is "
            "kept for backward compatibility but the style-driven path is "
            "the recommended default. Please migrate to use_legacy_template=False."
        )

    # Phase 4: serialise the resolved LetterStyle (if any) so the UI
    # can show the user "what formatting was applied". Only include it
    # when the style-driven path was actually used (legacy template
    # path also resolves a style for its export, but the user opted
    # into the legacy flow — they should not see a "style badge" then).
    style_dict = None
    if not req.use_legacy_template and result.letter_style is not None:
        # LetterStyle is a slots=True dataclass; use asdict for safety.
        from dataclasses import asdict
        style_dict = asdict(result.letter_style)

    return {
        "ok":             result.ok,
        "final_status":   result.final_status,
        "needs_review":   result.needs_review,
        "error":          result.error,
        "draft_id":       result.draft_id,
        "stages":         result.stages,
        "pdf_available":  result.pdf_available,

        "intent": {
            "letter_type":    result.intent.letter_type if result.intent else None,
            "letter_type_ar": result.intent.letter_type_ar if result.intent else None,
            "fields":         result.intent.fields if result.intent else [],
            "confidence":     result.intent.confidence if result.intent else 0.0,
        } if result.intent else None,

        "draft":           _draft_to_dict(result.draft),
        "compliance":      _compliance_to_dict(result.compliance),
        "corrected_draft": _draft_to_dict(result.corrected_draft),
        "re_compliance":   _compliance_to_dict(result.re_compliance),
        "final_draft":     _draft_to_dict(result.final_draft),

        "chunks_used":     result.chunks_used,

        "template_profile": {
            "has_body_marker":   result.template_profile.has_body_marker,
            "placeholder_names": result.template_profile.placeholder_names,
            "rtl":               result.template_profile.rtl,
            "paragraph_count":   result.template_profile.paragraph_count,
            "section_count":     result.template_profile.section_count,
        } if result.template_profile else None,

        # Phase 4: the resolved LetterStyle from the style-driven path.
        # Null when the legacy template path was used.
        "letter_style":    style_dict,

        "docx_url": docx_url,
        "pdf_url":  pdf_url,
        # Carry the already-generated Word file with the reviewed response.
        # The client can therefore download exactly what it previewed without
        # running the AI pipeline a second time.
        "docx_base64": (
            base64.b64encode(result.docx_bytes).decode("ascii")
            if result.docx_bytes else None
        ),
    }


# -- route: /generate/docx -----------------------------------------------

@router.post("/generate/docx")
async def generate_docx_only(
    req: GenerateRequest,
    x_admin_token: Optional[str] = Header(default=None),
):
    """Stream the final DOCX directly (official, no sources block)."""
    _check_admin(x_admin_token)
    pipeline = _get_pipeline()
    result = await pipeline.run(
        req.request,
        known_fields=req.fields,
        user_id=req.user_id,
        style_overrides=req.style_overrides or None,
        use_legacy_template=req.use_legacy_template,
    )
    if not result.docx_bytes:
        raise HTTPException(
            422, f"could not generate DOCX: {result.error or result.final_status}",
        )
    filename = f"{_ascii_only(result.intent.letter_type_ar if result.intent else 'draft')}.docx"
    return Response(
        content=result.docx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# -- route: /generate/pdf ------------------------------------------------

@router.post("/generate/pdf")
async def generate_pdf_only(
    req: GenerateRequest,
    x_admin_token: Optional[str] = Header(default=None),
):
    """Stream the final PDF. Returns 503 if soffice is not installed
    (no DOCX-as-PDF fallback — the API never lies about availability).
    """
    _check_admin(x_admin_token)
    if not has_soffice():
        raise HTTPException(
            503,
            "PDF conversion unavailable: LibreOffice (soffice) is not "
            "installed on the server. Install it and retry, or download "
            "the DOCX instead via /api/letters/generate/docx.",
        )
    pipeline = _get_pipeline()
    result = await pipeline.run(
        req.request,
        known_fields=req.fields,
        user_id=req.user_id,
    )
    if not result.pdf_bytes:
        raise HTTPException(
            422, f"could not generate PDF: {result.error or result.final_status}",
        )
    filename = f"{_ascii_only(result.intent.letter_type_ar if result.intent else 'draft')}.pdf"
    return Response(
        content=result.pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# -- route: /generate/status ----------------------------------------------

@router.post("/generate/status")
async def generate_status(
    req: GenerateRequest,
    x_admin_token: Optional[str] = Header(default=None),
):
    """Lightweight endpoint: returns intent + retrieval + compliance
    but does NOT run the generator or build DOCX/PDF. Useful for the
    UI to show "here's what we found, do you want to proceed?"."""
    _check_admin(x_admin_token)
    pipeline = _get_pipeline()
    # Reuse the pipeline's first 5 stages without writing the draft
    from rag.intent import detect_intent
    import asyncio
    intent = detect_intent(req.request)
    tq = intent.template_query or req.request
    pq = intent.policy_query or req.request
    rq = intent.regulation_query or req.request
    templates, policies, regulations = await asyncio.gather(
        pipeline.retrieve_templates_for_status(tq) if hasattr(pipeline, "retrieve_templates_for_status")
        else _retrieve(pipeline, "templates", tq),
        _retrieve(pipeline, "policies",  pq),
        _retrieve(pipeline, "regulations", rq),
    )
    return {
        "pdf_available": has_soffice(),
        "intent": {
            "letter_type":    intent.letter_type,
            "letter_type_ar": intent.letter_type_ar,
            "fields":         intent.fields,
            "confidence":     intent.confidence,
            "template_query": intent.template_query,
            "policy_query":   intent.policy_query,
            "regulation_query": intent.regulation_query,
        },
        "templates":   [{"id": h.document_id, "title": h.title, "category": h.category,
                         "similarity": h.similarity} for h in templates.hits],
        "policies":    [{"id": h.document_id, "title": h.title, "category": h.category,
                         "similarity": h.similarity} for h in policies.hits],
        "regulations": [{"id": h.document_id, "title": h.title, "category": h.category,
                         "similarity": h.similarity} for h in regulations.hits],
    }


async def _retrieve(pipeline: LetterPipeline, category: str, query: str):
    """Helper for /generate/status: run a single retrieval call."""
    from rag.retrieval import (
        retrieve_templates, retrieve_policies, retrieve_regulations,
    )
    if category == "templates":
        return await retrieve_templates(pipeline.embedder, pipeline.supabase, query)
    if category == "policies":
        return await retrieve_policies(pipeline.embedder, pipeline.supabase, query)
    if category == "regulations":
        return await retrieve_regulations(pipeline.embedder, pipeline.supabase, query)
    raise ValueError(category)
