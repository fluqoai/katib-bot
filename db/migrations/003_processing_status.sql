-- =============================================================================
-- Migration 003 — document processing status & diagnostics
--
-- Adds per-document fields so the UI / validation script can see what
-- actually happened to each file: how many chars were extracted, how many
-- chunks/embeddings were stored, what the file's last status was, and a
-- content hash for dedup.
--
-- Idempotent: safe to re-run.
-- =============================================================================

alter table documents
    add column if not exists status              text    not null default 'pending'
        check (status in ('pending', 'processing', 'indexed', 'failed', 'needs_ocr')),
    add column if not exists error_message       text,
    add column if not exists extracted_char_count int    not null default 0,
    add column if not exists chunk_count         int     not null default 0,
    add column if not exists embedding_count     int     not null default 0,
    add column if not exists last_processed_at   timestamptz,
    add column if not exists content_hash       text,
    add column if not exists mime_type          text,
    add column if not exists source_path        text;

-- Index for status-based filters
create index if not exists documents_status_idx
    on documents (status);

-- Index for content hash dedup
create unique index if not exists documents_content_hash_uniq
    on documents (content_hash)
    where content_hash is not null;

-- Backfill: mark the already-indexed docs as 'indexed' so the validation
-- script has a starting point. (Re-indexing will overwrite these.)
update documents
   set status           = 'indexed',
       last_processed_at = coalesce(last_processed_at, updated_at, created_at)
 where chunk_count = 0
   and status      = 'pending';

-- (We don't touch the others — they keep 'pending' until re-indexed.)
