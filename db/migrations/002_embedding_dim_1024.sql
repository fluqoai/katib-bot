-- =============================================================================
-- Migration 002 — switch embeddings to 1024-dim (Liquid LFM 2.5 via OpenRouter)
--
-- The original schema.sql used vector(1536) (OpenAI text-embedding-3-small).
-- If you apply the migration to use `liquid/lfm-2.5-embedding-350m:free` via
-- OpenRouter, the embedding dimension is 1024, not 1536.
--
-- Run this in the Supabase SQL editor IF you already applied schema.sql with
-- the 1536 default. If you haven't applied schema.sql yet, just edit the
-- file first to use vector(1024) and apply it.
--
-- This migration is destructive: it drops the existing document_chunks
-- table (which is fine if you haven't ingested anything yet).
-- =============================================================================

-- Drop the old chunks table and the RPC that references it
drop table if exists document_chunks cascade;
drop function if exists match_documents(vector, float, int, text) cascade;

-- Recreate with 1024-dim vector column
create table if not exists document_chunks (
    id           uuid primary key default gen_random_uuid(),
    document_id  uuid not null references documents (id) on delete cascade,
    chunk_index  int  not null,
    content      text not null,
    embedding    vector(1024) not null,
    metadata     jsonb default '{}'::jsonb,
    created_at   timestamptz not null default now()
);

create index if not exists document_chunks_document_id_idx
    on document_chunks (document_id);

create index if not exists document_chunks_embedding_hnsw_idx
    on document_chunks using hnsw (embedding vector_cosine_ops);

-- Recreate the RPC with the new dimension
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
    where (filter_category is null or d.category = filter_category)
      and 1 - (dc.embedding <=> query_embedding) > match_threshold
    order by dc.embedding <=> query_embedding
    limit greatest(match_count, 1);
end;
$$;
