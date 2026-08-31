-- =============================================================================
-- Migration 004 — extend status taxonomy with needs_doc_conversion
--
-- Migration 003 introduced the processing status column but only allowed
-- ('pending', 'processing', 'indexed', 'failed', 'needs_ocr'). The .doc
-- loader can return 'needs_doc_conversion' (file is an old binary .doc that
-- needs LibreOffice), so the constraint needs to be widened.
--
-- Idempotent: safe to re-run.
-- =============================================================================

-- Drop the old constraint if it exists, add the wider one
alter table documents drop constraint if exists documents_status_check;

alter table documents
    add constraint documents_status_check
    check (status in (
        'pending',
        'processing',
        'indexed',
        'failed',
        'needs_ocr',
        'needs_doc_conversion'
    ));
