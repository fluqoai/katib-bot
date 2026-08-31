-- =============================================================================
-- Migration 006 — version-isolated chunks + atomic version swap
--
-- Before this migration, document_chunks was only linked to document_id.
-- That meant when a new version (v2) was uploaded, the v1 and v2 chunks
-- coexisted in the table with no way to tell them apart in retrieval.
-- The match_documents RPC was returning a mix of both.
--
-- After this migration:
--   1. Every chunk is bound to its specific (document_file_id, version).
--   2. There is a unique constraint on (document_file_id, chunk_index)
--      so the worker can't accidentally insert duplicates for the same
--      version.
--   3. match_documents filters by `dc.version = d.current_version`
--      so retrieval ONLY ever sees the current version's chunks.
--   4. Old versions' chunks stay in the table (for audit + reprocess
--      + future archival) but never appear in retrieval.
--   5. is_active = false on the document excludes ALL versions.
-- =============================================================================


-- -- 1. New columns -------------------------------------------------------

alter table document_chunks
    add column if not exists document_file_id uuid
        references document_files(id) on delete cascade,
    add column if not exists version          int;

create index if not exists document_chunks_file_id_idx
    on document_chunks (document_file_id);
create index if not exists document_chunks_version_idx
    on document_chunks (document_id, version);


-- -- 2. Backfill existing rows ------------------------------------------
-- Every existing chunk is assumed to belong to v1 of its document
-- (the legacy schema only had one version per document). We look up
-- the v1 document_files row for each document and use its id.

with v1 as (
    select distinct on (document_id) id, document_id, version
    from document_files
    where version = 1
    order by document_id, version
)
update document_chunks dc
   set document_file_id = v1.id,
       version           = 1
  from v1
 where dc.document_id = v1.document_id
   and dc.document_file_id is null;


-- -- 3. Unique constraint ------------------------------------------------
-- Prevents accidental duplicate inserts for the same (file, chunk_index).
-- A file can be reprocessed only after the admin API explicitly deletes
-- the existing chunks for that file_id.

alter table document_chunks
    drop constraint if exists document_chunks_file_chunk_uniq;

alter table document_chunks
    add constraint document_chunks_file_chunk_uniq
    unique (document_file_id, chunk_index);


-- -- 4. match_documents: filter by current_version -----------------------
-- The core safety property: retrieval can ONLY ever return chunks
-- whose version matches the document's current_version. Old version
-- chunks are invisible.
--
-- We DROP then CREATE because the return-type changed (we now return
-- `file_id` and `version` columns too, so callers can audit which
-- version produced each retrieved chunk).

drop function if exists match_documents(vector(1024), float, int, text);

create function match_documents(
    query_embedding  vector(1024),
    match_threshold  float default 0.70,
    match_count      int   default 5,
    filter_category  text  default null
)
returns table (
    id           uuid,
    document_id  uuid,
    file_id      uuid,
    version      int,
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
        dc.document_file_id,
        dc.version,
        dc.chunk_index,
        dc.content,
        d.category,
        d.title,
        d.source_uri,
        1 - (dc.embedding <=> query_embedding) as similarity
    from document_chunks dc
    join documents d on d.id = dc.document_id
    where d.is_active = true                            -- doc must be active
      and dc.version = d.current_version               -- ONLY current version
      and (filter_category is null or d.category = filter_category)
      and 1 - (dc.embedding <=> query_embedding) > match_threshold
    order by dc.embedding <=> query_embedding
    limit greatest(match_count, 1);
end;
$$;


-- -- 5. Backfill: also update the documents.current_version where it was
-- set to 99 by the earlier test runs.

update documents
   set current_version = 1
 where current_version > 50;


-- -- 6. Index: speed up the (version = current_version) filter ---------

create index if not exists document_chunks_doc_version_idx
    on document_chunks (document_id, version);
