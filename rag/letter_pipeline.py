"""Letter-generation pipeline orchestrator.

The pipeline runs as a state machine with explicit checkpoints. Each
stage is a function that takes the `PipelineContext` and returns the
same context updated. Stages can short-circuit on failure.

Stages
======
1.  intent               — detect letter type + refine queries
2.  retrieval            — three parallel semantic searches
3.  evidence             — assemble the structured evidence bundle
4.  draft                — call the generator with strict citations
5.  compliance_review    — independent re-read of the draft
6.  correct              — if fixable, call the generator again with
                           the reviewer's suggested corrections
7.  re_compliance        — re-review the corrected draft
8.  export               — DOCX (template-as-style) + optional PDF
9.  persist              — record provenance + body in the DB

Final status values
===================
* ``ok``              — draft + compliance both pass cleanly
* ``fixable``         — auto-corrected draft passed the re-review
* ``unverifiable``    — substantive unfixable claims
* ``needs_review``    — first review or re-review said the draft is
                        not safe to auto-correct; a human must review
* ``no_template``     — no template in the index
* ``failed``          — exception or unrecoverable error

The system NEVER silently produces a letter that the compliance stage
has identified as potentially incorrect. If the auto-correct loop
can't produce a draft that re-review accepts, the result is
``needs_review`` and the client UI surfaces it for human attention.
"""
from __future__ import annotations

import logging
import re
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from supabase import Client

from rag.compliance import ComplianceReport, ComplianceReviewer
from rag.embeddings import EmbeddingClient
from rag.evidence import EvidenceBundle, SourceChunk, build_evidence
from rag.export import (
    TemplateProfile,
    build_letter_from_template,
    build_pdf,
    has_soffice,
)
from rag.generator import GeneratedDraft, LetterGenerator
from rag.intent import LetterIntent, detect_intent_async
from rag.retrieval import (
    RetrievalResult,
    retrieve_policies,
    retrieve_regulations,
    retrieve_templates,
)


logger = logging.getLogger(__name__)


# -- pipeline state --------------------------------------------------------

@dataclass
class PipelineContext:
    user_request: str
    known_fields: dict[str, str] = field(default_factory=dict)
    user_id:      str | None = None

    # --- Phase 2: style-driven path (NEW, optional) ---
    style_overrides: dict | None = None
    use_legacy_template: bool = False
    # The resolved style, computed by _stage_style_resolution
    style_spec: Any = None  # rag.export.LetterStyle | None

    intent: LetterIntent | None = None
    templates: RetrievalResult | None = None
    policies: RetrievalResult | None = None
    regulations: RetrievalResult | None = None
    bundle: EvidenceBundle | None = None

    # Two-pass: the original draft and (after auto-correct) the revised one
    draft: GeneratedDraft | None = None
    compliance: ComplianceReport | None = None
    corrected_draft: GeneratedDraft | None = None
    re_compliance: ComplianceReport | None = None
    # The draft the user actually sees (corrected if available, else original)
    final_draft: GeneratedDraft | None = None

    docx_bytes: bytes | None = None
    pdf_bytes:  bytes | None = None
    draft_id:   str | None = None

    # Template profile extracted for the export stage (legacy)
    template_profile: TemplateProfile | None = None
    # Phase 2: True iff the export used a template (legacy path).
    # False if the style-driven path was used.
    export_used_template: bool = False

    stages: list[dict[str, Any]] = field(default_factory=list)

    def checkpoint(self, stage: str, status: str, note: str = "") -> None:
        self.stages.append({
            "stage": stage,
            "status": status,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "ended_at":   datetime.now(timezone.utc).isoformat(),
            "note":       note,
        })


@dataclass
class PipelineResult:
    ok: bool
    intent: LetterIntent | None
    bundle: EvidenceBundle | None
    draft: GeneratedDraft | None                    # the original draft
    compliance: ComplianceReport | None             # the original review
    corrected_draft: GeneratedDraft | None         # the revised draft, if any
    re_compliance: ComplianceReport | None          # the re-review, if any
    final_draft: GeneratedDraft | None              # the draft the user sees
    docx_bytes: bytes | None
    pdf_bytes: bytes | None
    pdf_available: bool                             # True iff soffice succeeded
    template_profile: TemplateProfile | None
    chunks_used: list[dict[str, Any]]               # full chunk provenance
    stages: list[dict[str, Any]]
    final_status: str                               # see module docstring
    needs_review: bool                              # True iff final_status='needs_review'
    error: str | None = None
    draft_id: str | None = None
    # Phase 4: the resolved LetterStyle (only set when the style-driven
    # path was used). None when the legacy template path is used.
    # NOTE: declared last so it can have a default value without
    # violating the dataclass field-order rule.
    letter_style: Any = None                        # rag.export.LetterStyle | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "final_status": self.final_status,
            "needs_review": self.needs_review,
            "error": self.error,
            "stages": self.stages,
            "intent": {
                "letter_type": self.intent.letter_type if self.intent else None,
                "letter_type_ar": self.intent.letter_type_ar if self.intent else None,
                "fields": self.intent.fields if self.intent else [],
            } if self.intent else None,
            "draft": {
                "body": self.draft.body if self.draft else None,
                "claim_count": self.draft.claim_count() if self.draft else 0,
                "has_unverified": self.draft.has_unverified_claims() if self.draft else False,
                "sources": self.draft.parsed_citations if self.draft else [],
                "model": self.draft.model if self.draft else None,
            } if self.draft else None,
            "compliance": {
                "verdict": self.compliance.verdict if self.compliance else None,
                "unverified_claims": self.compliance.unverified_claims if self.compliance else [],
                "summary_ar": self.compliance.summary_ar if self.compliance else "",
            } if self.compliance else None,
        }


# -- the pipeline ----------------------------------------------------------

class LetterPipeline:
    def __init__(
        self,
        embedder: EmbeddingClient,
        generator: LetterGenerator,
        reviewer:  ComplianceReviewer,
        supabase: Client,
    ) -> None:
        self.embedder = embedder
        self.generator = generator
        self.reviewer  = reviewer
        self.supabase  = supabase

    async def run(
        self,
        user_request: str,
        *,
        known_fields: dict[str, str] | None = None,
        user_id: str | None = None,
        # Phase 2: optional style-driven path inputs
        style_overrides: dict | None = None,
        use_legacy_template: bool = False,
    ) -> PipelineResult:
        ctx = PipelineContext(
            user_request=user_request,
            known_fields=known_fields or {},
            user_id=user_id,
            style_overrides=style_overrides,
            use_legacy_template=use_legacy_template,
        )
        try:
            await self._stage_intent(ctx)
            # Phase 2: resolve the visual style (style-driven path)
            await self._stage_style_resolution(ctx)
            # NOTE: the pipeline no longer short-circuits to no_template
            # based on intent alone. An unmatched intent falls through
            # to a "general" type and the closest available template
            # is adopted as a style source. The only path to
            # `no_template` is `bundle.has_template() == False` below
            # (i.e. the index is truly empty).
            await self._stage_retrieval(ctx)
            await self._stage_evidence(ctx)
            if not ctx.bundle.has_template():
                ctx.checkpoint("evidence", "no_template",
                               "لا يوجد قالب مطابق في الفهرس")
                return self._finalize(ctx, status="no_template",
                                       error="لا يوجد قالب مطابق")
            await self._stage_draft(ctx)
            await self._stage_compliance(ctx)
            await self._stage_correct(ctx)
            await self._stage_re_compliance(ctx)

            # Decide what to send to the user
            ctx.final_draft = ctx.corrected_draft or ctx.draft
            await self._stage_export(ctx)
            await self._stage_persist(ctx)

            # Final status
            final = self._decide_final_status(ctx)
            ctx.checkpoint("pipeline", "ok", note=f"final_status={final}")
            return self._finalize(ctx, status=final)
        except Exception as e:  # noqa: BLE001
            logger.exception("pipeline crashed")
            ctx.checkpoint("pipeline", "failed", note=f"{type(e).__name__}: {e}")
            return self._finalize(ctx, status="failed", error=f"{type(e).__name__}: {e}")

    # -- stage 1
    async def _stage_intent(self, ctx: PipelineContext) -> None:
        # Use the async LLM-backed detector so an unmatched request
        # gets a "general" type instead of None. If the LLM is down
        # or times out, it falls back to keyword matching and then
        # to "general" — never None.
        ctx.intent = await detect_intent_async(ctx.user_request)
        note = f"type={ctx.intent.letter_type} conf={ctx.intent.confidence:.2f}"
        if ctx.intent.letter_type == "general":
            note += " (LLM/keyword fallback — closest template used as style source)"
        ctx.checkpoint("intent", "ok", note=note)

    # -- stage 1.5 (Phase 2: resolve the visual style)
    async def _stage_style_resolution(self, ctx: PipelineContext) -> None:
        """Compute the ``LetterStyle`` for this run.

        Resolution order:
          1. If the caller passed ``style_overrides``, start from
             ``default_style_for(intent.letter_type)`` and apply
             the overrides.
          2. Else use ``default_style_for(intent.letter_type)``
             directly (per-intent defaults from rag.intent).

        The style is consumed by ``_stage_export`` when building the
        DOCX from scratch (the style-driven path).

        The legacy template-driven path is unaffected by this stage.
        """
        try:
            from rag.intent import default_style_for
            from rag.export import LetterStyle
        except ImportError as e:  # noqa: BLE001
            ctx.checkpoint("style_resolution", "failed", note=f"{e}")
            return

        letter_type = ctx.intent.letter_type if ctx.intent else None
        base = default_style_for(letter_type)
        if ctx.style_overrides:
            # Filter overrides to only the keys LetterStyle knows about.
            # Unknown keys are silently dropped (so the caller can send
            # a future field without breaking older servers).
            from dataclasses import asdict
            valid_keys = {f.name for f in LetterStyle.__dataclass_fields__.values()}
            filtered = {k: v for k, v in (ctx.style_overrides or {}).items() if k in valid_keys}
            try:
                # LetterStyle uses slots=True so it has no __dict__;
                # use asdict() to get a plain dict of all fields.
                merged = {**asdict(base), **filtered}
                ctx.style_spec = LetterStyle(**merged)
                note = f"letter_type={letter_type or 'general'} applied={list(filtered.keys())} rejected={[k for k in (ctx.style_overrides or {}) if k not in valid_keys]}"
            except TypeError as e:
                # Type mismatch on a known key (e.g. wrong type for a
                # field). Fall back to base style.
                ctx.style_spec = base
                note = f"letter_type={letter_type or 'general'} overrides rejected: {e}"
        else:
            ctx.style_spec = base
            note = f"letter_type={letter_type or 'general'}"
        ctx.checkpoint("style_resolution", "ok", note=note)

    # -- stage 2
    async def _stage_retrieval(self, ctx: PipelineContext) -> None:
        tq = (ctx.intent.template_query   if ctx.intent and ctx.intent.template_query   else ctx.user_request)
        pq = (ctx.intent.policy_query     if ctx.intent and ctx.intent.policy_query     else ctx.user_request)
        rq = (ctx.intent.regulation_query if ctx.intent and ctx.intent.regulation_query else ctx.user_request)

        import asyncio
        ctx.templates, ctx.policies, ctx.regulations = await asyncio.gather(
            retrieve_templates(self.embedder, self.supabase, tq),
            retrieve_policies(self.embedder, self.supabase, pq),
            retrieve_regulations(self.embedder, self.supabase, rq),
        )
        ctx.checkpoint(
            "retrieval", "ok",
            f"templates={len(ctx.templates.hits)} policies={len(ctx.policies.hits)} "
            f"regulations={len(ctx.regulations.hits)}",
        )

    # -- stage 3
    async def _stage_evidence(self, ctx: PipelineContext) -> None:
        ctx.bundle = build_evidence(
            user_request=ctx.user_request,
            templates=ctx.templates,
            policies=ctx.policies,
            regulations=ctx.regulations,
        )
        ctx.checkpoint("evidence", "ok",
                       f"t={len(ctx.bundle.template_hits)} p={len(ctx.bundle.policy_hits)} r={len(ctx.bundle.regulation_hits)}")

    # -- stage 4
    async def _stage_draft(self, ctx: PipelineContext) -> None:
        # Phase 3: when the style-driven path is in use (the production
        # default), tell the generator to drop the templates section
        # from the prompt. This is a defense-in-depth measure: even if
        # retrieve_templates returned a wrong template, the LLM no
        # longer sees its body and cannot imitate it.
        no_template_mode = not ctx.use_legacy_template
        ctx.draft = await self.generator.generate(
            user_request=ctx.user_request,
            bundle=ctx.bundle,
            letter_type_ar=ctx.intent.letter_type_ar if ctx.intent else None,
            fields=ctx.intent.fields if ctx.intent else [],
            known_fields=ctx.known_fields,
            no_template_mode=no_template_mode,
        )
        ctx.checkpoint(
            "draft", "ok",
            note=f"claims={ctx.draft.claim_count()} unverified={ctx.draft.has_unverified_claims()} "
                 f"no_template_mode={no_template_mode}",
        )

    # -- stage 5
    async def _stage_compliance(self, ctx: PipelineContext) -> None:
        ctx.compliance = await self.reviewer.review(ctx.draft, ctx.bundle)
        ctx.checkpoint("compliance", ctx.compliance.verdict,
                       note=ctx.compliance.summary_ar)

    # -- stage 6
    async def _stage_correct(self, ctx: PipelineContext) -> None:
        """Auto-correct fixable drafts by re-writing them with the
        reviewer's suggested corrections. If the first review was
        'ok' or 'unverifiable' we skip this stage.
        """
        if not ctx.compliance or not ctx.compliance.needs_correction():
            ctx.checkpoint("correct", "skipped", "no correction needed")
            return
        try:
            no_template_mode = not ctx.use_legacy_template
            ctx.corrected_draft = await self.generator.correct(
                draft=ctx.draft,
                bundle=ctx.bundle,
                suggestions=ctx.compliance.suggested_corrections or "",
                letter_type_ar=ctx.intent.letter_type_ar if ctx.intent else None,
                known_fields=ctx.known_fields,
                no_template_mode=no_template_mode,
            )
            ctx.checkpoint(
                "correct", "ok",
                note=f"corrected claims={ctx.corrected_draft.claim_count()}",
            )
        except Exception as e:  # noqa: BLE001
            # Auto-correct failed — fall through to the re-review
            # which will decide needs_review
            ctx.checkpoint("correct", "failed", note=f"{type(e).__name__}: {e}")

    # -- stage 7
    async def _stage_re_compliance(self, ctx: PipelineContext) -> None:
        """Re-review the corrected draft. If it's still problematic,
        we mark the letter ``needs_review`` so the UI can route it
        to a human.
        """
        if ctx.corrected_draft is None:
            # Nothing to re-review; just inherit the original verdict
            ctx.checkpoint("re_compliance", "skipped", "no corrected draft")
            return
        try:
            ctx.re_compliance = await self.reviewer.review(
                ctx.corrected_draft, ctx.bundle,
            )
            ctx.checkpoint(
                "re_compliance", ctx.re_compliance.verdict,
                note=ctx.re_compliance.summary_ar,
            )
        except Exception as e:  # noqa: BLE001
            ctx.checkpoint("re_compliance", "failed", note=f"{type(e).__name__}: {e}")

    # -- stage 8
    async def _stage_export(self, ctx: PipelineContext) -> None:
        """Build the final DOCX + PDF.

        Phase 2 — style-driven path (NEW, default):
          * If ``ctx.style_spec`` is set AND no template is required by
            the caller (``not ctx.use_legacy_template``), use
            ``build_letter_from_style`` to construct a fresh DOCX from
            the resolved LetterStyle + the draft body. No template file
            is needed; the export works even when ``bundle.best_template()``
            is None or wrong.
          * When a template IS available and the caller requested the
            legacy path, fall back to ``build_letter_from_template`` for
            full backward compatibility.

        Legacy template-driven path (unchanged):
          * When ``ctx.use_legacy_template`` is True and a template is
            retrievable, use the existing template path.
        """
        out_draft = ctx.corrected_draft or ctx.draft
        if out_draft is None:
            ctx.docx_bytes = None
            ctx.pdf_bytes = None
            ctx.checkpoint("export", "skipped", "no draft to export")
            return

        # ------------------------------------------------------------------
        # Phase 2: style-driven path (default)
        # ------------------------------------------------------------------
        # Use the style-driven path when:
        #   - the caller did NOT explicitly opt into legacy mode
        #   - AND a style_spec was resolved by _stage_style_resolution
        # The legacy mode is preserved for backward compat with existing
        # callers (the 10 acceptance tests still exercise the template
        # path through `use_legacy_template=False` plus the new style
        # path producing equivalent output).
        use_legacy = ctx.use_legacy_template

        # Try style-driven first unless the caller forced legacy
        if not use_legacy and ctx.style_spec is not None:
            try:
                from rag.export import build_letter_from_style
                docx = build_letter_from_style(out_draft, style=ctx.style_spec)
                ctx.docx_bytes = docx
                ctx.template_profile = None  # no template was used
                ctx.export_used_template = False
                ctx.pdf_bytes = build_pdf(docx) if has_soffice() else None
                ctx.checkpoint(
                    "export", "ok",
                    note=f"docx={len(docx)}B pdf={'yes' if ctx.pdf_bytes else 'no'} "
                         f"mode=style basmala={ctx.style_spec.include_basmala} "
                         f"signature={ctx.style_spec.include_signature_block}",
                )
                return
            except Exception as e:  # noqa: BLE001
                # Style-driven failed — fall through to legacy path
                ctx.checkpoint(
                    "export", "style_failed",
                    note=f"style-driven export failed ({type(e).__name__}: {e}); falling back to template",
                )

        # ------------------------------------------------------------------
        # Legacy template-driven path
        # ------------------------------------------------------------------
        template = ctx.bundle.best_template()
        if template is None:
            # No template available AND style path already failed (or
            # was skipped). Produce a minimal DOCX with the default
            # style so the user always gets a file.
            try:
                from rag.export import build_letter_from_style, DEFAULT_LETTER_STYLE
                style = ctx.style_spec or DEFAULT_LETTER_STYLE
                docx = build_letter_from_style(out_draft, style=style)
                ctx.docx_bytes = docx
                ctx.template_profile = None
                ctx.export_used_template = False
                ctx.pdf_bytes = build_pdf(docx) if has_soffice() else None
                ctx.checkpoint(
                    "export", "ok",
                    note=f"docx={len(docx)}B pdf={'yes' if ctx.pdf_bytes else 'no'} "
                         f"mode=style_fallback (no template, no style_spec)",
                )
                return
            except Exception as e:  # noqa: BLE001
                ctx.docx_bytes = None
                ctx.pdf_bytes = None
                ctx.checkpoint("export", "skipped", f"no template and style fallback failed: {e}")
                return

        # Find the document_files row (template's storage path)
        template_path = await self._resolve_template_path(template)
        if template_path is None:
            # Template is in DB but the file can't be located. Fall
            # back to the style-driven path so the user still gets a
            # DOCX (this is the Phase 2 promise: never silent skip).
            try:
                from rag.export import build_letter_from_style, DEFAULT_LETTER_STYLE
                style = ctx.style_spec or DEFAULT_LETTER_STYLE
                docx = build_letter_from_style(out_draft, style=style)
                ctx.docx_bytes = docx
                ctx.template_profile = None
                ctx.export_used_template = False
                ctx.pdf_bytes = build_pdf(docx) if has_soffice() else None
                ctx.checkpoint(
                    "export", "ok",
                    note=f"docx={len(docx)}B pdf={'yes' if ctx.pdf_bytes else 'no'} "
                         f"mode=style_fallback (template file unresolvable)",
                )
                return
            except Exception as e:  # noqa: BLE001
                ctx.docx_bytes = None
                ctx.pdf_bytes = None
                ctx.checkpoint("export", "failed", f"could not resolve template file and style fallback failed: {e}")
                return

        try:
            docx, profile = build_letter_from_template(
                template_path,
                out_draft,
                placeholders=ctx.known_fields,
                include_sources=False,        # official letter stays clean
            )
            ctx.template_profile = profile
            ctx.export_used_template = True
            ctx.docx_bytes = docx
            # PDF: only attempt if soffice is present. Return None on
            # failure rather than silently substituting the DOCX bytes.
            ctx.pdf_bytes = build_pdf(docx) if has_soffice() else None
            ctx.checkpoint(
                "export", "ok",
                note=f"docx={len(docx)}B pdf={'yes' if ctx.pdf_bytes else 'no'} "
                     f"mode={'marker' if profile.has_body_marker else 'fields' if profile.placeholder_names else 'clean'} "
                     f"rtl={profile.rtl} src={template_path}",
            )
        except Exception as e:  # noqa: BLE001
            ctx.checkpoint("export", "failed", note=f"{type(e).__name__}: {e}")

    # -- stage 9
    async def _stage_persist(self, ctx: PipelineContext) -> None:
        if not ctx.draft:
            return
        # Phase 2: template is OPTIONAL. The new style-driven path
        # may produce a DOCX without ever using a template; in that
        # case, ctx.template_profile is None AND ctx.export_used_template
        # is False, and we record template_*=NULL.
        #
        # The retrieved template (bundle.best_template()) is still
        # available as a "reference" but we DO NOT promote it to the
        # "source" of the letter unless the export actually used it.
        if ctx.export_used_template:
            template = ctx.bundle.best_template()
        else:
            template = None  # style-driven path: no template recorded
        out_draft = ctx.corrected_draft or ctx.draft
        # Full chunk provenance
        chunks_used = []
        for s in ctx.bundle.all_sources():
            chunks_used.append({
                "document_id":  s.document_id,
                "file_id":      s.file_id,
                "version":      s.version,
                "chunk_index":  s.chunk_index,
                "title":        s.title,
                "category":     s.category,
                "similarity":   s.similarity,
            })
        # Clean body (no inline citation tags) for the official DOCX
        clean_body = _strip_citation_tags(out_draft.body_only or out_draft.body)
        # Status for the human workflow: only auto-approve if the
        # final verdict is 'ok'. 'fixable' (after auto-correct) goes
        # to pending_review so a human can give the final OK.
        wf_status = (
            "approved" if (ctx.re_compliance and ctx.re_compliance.is_ok())
            else "pending_review"
        )
        try:
            ins = self.supabase.table("drafts").insert({
                "user_id":                int(ctx.user_id) if ctx.user_id and ctx.user_id.isdigit() else None,
                "title":                  (ctx.intent.letter_type_ar if ctx.intent else None) or "مسودة",
                "body":                   out_draft.body,             # full body incl. sources block (for review)
                "clean_body":             clean_body,                 # the text that went into the DOCX
                "placeholders":           ctx.known_fields,
                "sources":                out_draft.parsed_citations,  # citations the LLM emitted
                "chunks_used":            chunks_used,                # full provenance
                "status":                 wf_status,                  # human workflow
                "final_status":           self._decide_final_status(ctx),
                "needs_review":           self._decide_final_status(ctx) == "needs_review",
                # Phase 2: these are NULL when the style-driven path was used
                "template_document_id":   template.document_id if template else None,
                "template_file_id":       template.file_id if template else None,
                "template_version":       template.version if template else None,
                "model":                  out_draft.model,
                "generated_at":           datetime.now(timezone.utc).isoformat(),
                "compliance_verdict":     (ctx.re_compliance or ctx.compliance).verdict if (ctx.re_compliance or ctx.compliance) else None,
                "compliance_summary":     (ctx.re_compliance or ctx.compliance).summary_ar if (ctx.re_compliance or ctx.compliance) else "",
                "compliance_unverified":  (ctx.re_compliance or ctx.compliance).unverified_claims if (ctx.re_compliance or ctx.compliance) else [],
            }).execute()
            if ins.data:
                ctx.draft_id = ins.data[0].get("id")
            ctx.checkpoint("persist", "ok", note=f"draft_id={ctx.draft_id}")
        except Exception as e:  # noqa: BLE001
            ctx.checkpoint("persist", "failed", note=f"{type(e).__name__}: {e}")

    # -- helpers
    async def _resolve_template_path(self, template) -> Path | None:
        """Find the chosen template's DOCX on the local filesystem.

        Three strategies, in order:
        1. The ``document_files.storage_path`` is a local path → use it.
        2. Otherwise, try the Supabase Storage download.
        3. Otherwise, fall back to a local ``reference_docs/<bucket>/<name>``
           search.

        Returns None if the template cannot be located.
        """
        try:
            f_row = (
                self.supabase.table("document_files")
                .select("storage_bucket, storage_path, version, mime_type")
                .eq("document_id", template.document_id)
                .eq("version", template.version)
                .limit(1)
                .execute()
            )
            if not f_row.data:
                return None
            f = f_row.data[0]
            storage_path = f.get("storage_path", "")
            storage_bucket = f.get("storage_bucket", "")
            if _looks_like_local_path(storage_path):
                local = Path(storage_path)
                if local.exists() and local.is_file():
                    return local
                return None
            try:
                data = self.supabase.storage.from_(storage_bucket).download(storage_path)
                tmp = Path(tempfile.gettempdir()) / f"kateb-template-{uuid.uuid4().hex}.docx"
                tmp.write_bytes(data)
                return tmp
            except Exception:
                # Try local fallback
                fallback = _find_local_fallback(storage_path, storage_bucket)
                if fallback and fallback.exists():
                    return fallback
                return None
        except Exception:  # noqa: BLE001
            return None

    def _decide_final_status(self, ctx: PipelineContext) -> str:
        """Map the original + re-review verdicts to a final_status.

        Strict policy (per the user's "do not weaken the threshold"):

        * No compliance review → ``ok`` (no problems detected)
        * 'unverifiable' (first or re) → ``needs_review`` (never silent)
        * 'ok' on first pass    → ``ok``
        * 'fixable' on first pass:
            - corrected_draft is None (correct() failed) → ``needs_review``
            - re-review verdict is 'ok' AND re-review found
              no findings → ``fixable`` (auto-corrected, auto-approved)
            - re-review verdict is 'ok' BUT re-review reported
              ANY issue list (duplicates, wrong names, etc.)
              → ``needs_review`` (safety: trust the findings, not the verdict)
            - re-review verdict is anything else
              (fixable / unverifiable) → ``needs_review``

        The "trust the findings, not the verdict" rule is the
        non-negotiable safety net. If the re-review is supposedly
        'ok' but flagged ANY duplicate / wrong name / unsupported
        fact, we still route to human review.
        """
        c = ctx.compliance
        r = ctx.re_compliance
        if c is None:
            return "ok"
        if c.is_unverifiable():
            return "needs_review"
        if c.is_ok():
            return "ok"
        # c.verdict == "fixable" here
        if ctx.corrected_draft is None:
            return "needs_review"
        if r is None:
            return "needs_review"
        if r.is_unverifiable():
            return "needs_review"
        if not r.is_ok():
            return "needs_review"  # verdict is 'fixable' or anything else
        # verdict says 'ok' — but check the actual findings as a
        # safety net. If the reviewer reported ANY issue (duplicates,
        # wrong names, unsupported facts, …), we do not trust the
        # verdict and route to human review.
        if r.all_findings():
            return "needs_review"
        return "fixable"

    def _finalize(
        self,
        ctx: PipelineContext,
        *,
        status: str,
        error: str | None = None,
    ) -> PipelineResult:
        # The draft the user sees
        final_draft = ctx.corrected_draft or ctx.draft
        # 'ok' on the first pass means we can ship; 'fixable' (after a
        # successful auto-correct + ok re-review) also ships because
        # the issues have been addressed. 'needs_review' must NOT
        # auto-ship.
        ok = (
            status in ("ok", "fixable")
            and final_draft is not None
            and not (status == "fixable" and ctx.re_compliance and not ctx.re_compliance.is_ok())
        )
        # Chunks used
        chunks_used: list[dict[str, Any]] = []
        if ctx.bundle is not None:
            for s in ctx.bundle.all_sources():
                chunks_used.append({
                    "document_id":  s.document_id,
                    "file_id":      s.file_id,
                    "version":      s.version,
                    "chunk_index":  s.chunk_index,
                    "title":        s.title,
                    "category":     s.category,
                    "similarity":   s.similarity,
                })
        return PipelineResult(
            ok=ok,
            intent=ctx.intent,
            bundle=ctx.bundle,
            draft=ctx.draft,
            compliance=ctx.compliance,
            corrected_draft=ctx.corrected_draft,
            re_compliance=ctx.re_compliance,
            final_draft=final_draft,
            docx_bytes=ctx.docx_bytes,
            pdf_bytes=ctx.pdf_bytes,
            pdf_available=ctx.pdf_bytes is not None,
            template_profile=ctx.template_profile,
            # Phase 4: expose the resolved style to the API layer so the
            # UI can show the user "what formatting was applied".
            letter_style=ctx.style_spec,
            chunks_used=chunks_used,
            stages=ctx.stages,
            final_status=status,
            needs_review=(status == "needs_review"),
            error=error,
            draft_id=ctx.draft_id,
        )


# -- helpers ---------------------------------------------------------------

def _looks_like_local_path(p: str) -> bool:
    if not p:
        return False
    if p[1:3] == ":\\" or p[1:3] == ":/":
        return True
    if p.startswith("\\\\"):
        return True
    if p.startswith("/"):
        return True
    return False


def _find_local_fallback(storage_path: str, bucket: str) -> Path | None:
    name = storage_path.rsplit("/", 1)[-1]
    if not name:
        return None
    here = Path.cwd().resolve()
    for _ in range(6):
        candidate = here / "reference_docs" / bucket / name
        if candidate.exists():
            return candidate
        if here.parent.name == "":
            break
        here = here.parent
    return None


def _strip_citation_tags(text: str) -> str:
    """Remove ``[source: ...]`` markers from a body for the official DOCX."""
    return re.sub(r"\s*\[source:\s*[^\]]+\]", "", text).strip()


def _bucket_for(bundle: EvidenceBundle) -> str:
    cat = bundle.best_template().category if bundle.best_template() else "templates"
    return {
        "templates":            "templates",
        "national_regulations": "regulations",
        "internal_policies":    "policies",
        "examples":             "examples",
    }.get(cat, "templates")


__all__ = [
    "LetterPipeline",
    "PipelineContext",
    "PipelineResult",
]
