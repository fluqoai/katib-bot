-- =============================================================================
-- Migration 005 — Supabase Storage + Resumable Processing Pipeline
--
-- This migration moves the system from "local-folder ingestion" to a
-- production-grade pipeline:
--
--   admin/web  →  Supabase Storage (3 buckets)  →  Edge Function trigger
--                                              →  processing_jobs queue
--                                              →  Python worker (resumable)
--                                              →  document_chunks (active only)
--
-- New tables:
--   document_files      — one row per uploaded file VERSION
--   processing_jobs     — the queue the worker polls
--
-- New columns on documents:
--   current_version     — pointer to the active document_files row
--   is_active           — soft-delete flag (inactive docs never appear in search)
--   uploaded_by         — admin identifier (email or username)
--   processing_progress — JSONB snapshot of where the worker is
--   bucket              — denormalised bucket name (templates/policies/regulations)
--
-- The `match_documents` RPC is updated to filter by is_active = true so
-- inactive documents are NEVER returned in semantic search.
-- =============================================================================


-- -- 1. documents: new columns ---------------------------------------------

alter table documents
    add column if not exists current_version     int     not null default 1,
    add column if not exists is_active           boolean not null default true,
    add column if not exists uploaded_by         text,
    add column if not exists bucket              text
        check (bucket in ('templates', 'policies', 'regulations', 'examples', null)),
    add column if not exists processing_progress jsonb   not null default '{}'::jsonb;

create index if not exists documents_is_active_idx
    on documents (is_active) where is_active = true;

create index if not exists documents_bucket_idx
    on documents (bucket);


-- -- 2. document_files: one row per uploaded file version -------------------

create table if not exists document_files (
    id                  uuid primary key default gen_random_uuid(),
    document_id         uuid not null references documents(id) on delete cascade,
    version             int  not null,
    storage_bucket      text not null
        check (storage_bucket in ('templates', 'policies', 'regulations', 'examples')),
    storage_path        text not null,         -- e.g. "<doc_id>/v3/filename.pdf"
    mime_type           text,
    size_bytes          bigint,
    sha256              text,
    original_filename   text not null,
    uploaded_at         timestamptz not null default now(),
    uploaded_by         text,

    -- Processing state — mirror of the legacy fields on documents, but
    -- scoped to THIS version. A new version starts at "pending" and
    -- leaves the document's current version pointer alone until it
    -- finishes successfully.
    status              text not null default 'pending'
        check (status in ('pending', 'processing', 'indexed', 'failed',
                          'needs_ocr', 'needs_doc_conversion')),
    error_message       text,
    processing_progress jsonb  not null default '{}'::jsonb,
    extracted_char_count int   not null default 0,
    chunk_count         int   not null default 0,
    embedding_count     int   not null default 0,
    last_processed_at   timestamptz,

    -- One row per (document, version)
    unique (document_id, version)
);

create index if not exists document_files_document_id_idx
    on document_files (document_id, version desc);
create index if not exists document_files_status_idx
    on document_files (status);
create index if not exists document_files_sha256_idx
    on document_files (sha256);


-- -- 3. processing_jobs: the queue the Python worker polls -----------------

create table if not exists processing_jobs (
    id                uuid primary key default gen_random_uuid(),
    document_file_id  uuid not null references document_files(id) on delete cascade,
    status            text not null default 'pending'
        check (status in ('pending', 'processing', 'done', 'failed', 'cancelled')),
    priority          int  not null default 100,
    attempts          int  not null default 0,
    max_attempts      int  not null default 5,
    scheduled_at      timestamptz not null default now(),
    started_at        timestamptz,
    completed_at      timestamptz,
    next_retry_at     timestamptz,
    error_message     text,
    -- Soft lock so two workers (or two restarts) don't double-process
    claimed_by        text,
    claimed_at        timestamptz
);

create index if not exists processing_jobs_pending_idx
    on processing_jobs (status, priority desc, scheduled_at)
    where status in ('pending', 'processing');

create index if not exists processing_jobs_file_idx
    on processing_jobs (document_file_id);


-- -- 4. match_documents: filter to is_active = true -------------------------

create or replace function match_documents(
    query_embedding  vector(1024),
    match_threshold  float default 0.70,
    match_count      int   default 5,
    filter_category  text  default null
)
returns table (
    id           uuid,
    document_id  uuid,
    chunk_index  int,
    content      text,
    category     text,
    title        text,
    source_uri   text,
    similarity   float
)
language plpgsql stable
as $$
begin
    return query
    select
        dc.id,
        dc.document_id,
        dc.chunk_index,
        dc.content,
        d.category,
        d.title,
        d.source_uri,
        1 - (dc.embedding <=> query_embedding) as similarity
    from document_chunks dc
    join documents d on d.id = dc.document_id
    where d.is_active = true                                 -- NEW: only active
      and (filter_category is null or d.category = filter_category)
      and 1 - (dc.embedding <=> query_embedding) > match_threshold
    order by dc.embedding <=> query_embedding
    limit greatest(match_count, 1);
end;
$$;


-- -- 5. Backfill: existing rows are active v1 -------------------------------

update documents
   set is_active       = true,
       processing_progress = '{"backfilled": true, "version": 1}'::jsonb
 where is_active is null
    or processing_progress = '{}'::jsonb;

-- Stamp the bucket on existing rows so the UI can find them
update documents
   set bucket = case category
       when 'templates'           then 'templates'
       when 'national_regulations' then 'regulations'
       when 'internal_policies'   then 'policies'
       when 'examples'            then 'examples'
       else 'examples'
   end
 where bucket is null;

-- For each existing document, create a v1 document_files entry pointing
-- back at its source_path (the local Windows file path). The Python
-- worker that migrates these to Supabase Storage can re-use this row.
insert into document_files
    (document_id, version, storage_bucket, storage_path, mime_type,
     size_bytes, original_filename, status, chunk_count, embedding_count,
     last_processed_at, uploaded_at, processing_progress)
select
    d.id,
    coalesce(d.current_version, 1),
    coalesce(d.bucket, 'examples'),
    -- The legacy code stored the absolute Windows path in source_path.
    -- For migrated rows we keep the path verbatim; the worker uses it
    -- as a fallback when Storage is unavailable.
    coalesce(d.source_path, d.source_uri, 'unknown'),
    d.mime_type,
    null,                                  -- size unknown at backfill
    d.title,
    d.status,
    coalesce(d.chunk_count, 0),
    coalesce(d.embedding_count, 0),
    d.last_processed_at,
    now(),
    jsonb_build_object(
        'backfilled', true,
        'source',    'legacy-local',
        'note',      'migrated from pre-storage ingestion; re-run worker to upload to Supabase Storage'
    )
from documents d
on conflict (document_id, version) do nothing;


-- -- 6. RLS on the new tables ----------------------------------------------
-- The Python worker and Edge Function run with the service-role key
-- which bypasses RLS. End-user access (from the dashboard) goes through
-- RLS with policies we set up later when auth is added. For now we lock
-- everything to service-role.

alter table document_files    enable row level security;
alter table processing_jobs   enable row level security;
