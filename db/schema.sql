-- =============================================================================
-- Kateb — Supabase schema
-- Apply with: psql "<SUPABASE_DB_URL>" -f db/schema.sql
-- or via `python scripts/init_db.py` (uses the REST management API).
-- =============================================================================

-- Required extensions
create extension if not exists "uuid-ossp";
create extension if not exists "pgcrypto";
create extension if not exists vector;

-- ----------------------------------------------------------------------------
-- documents — one row per source file
-- ----------------------------------------------------------------------------
create table if not exists documents (
    id           uuid primary key default gen_random_uuid(),
    title        text not null,
    content      text not null,
    category     text not null
                 check (category in ('templates', 'national_regulations',
                                     'internal_policies', 'examples', 'other')),
    source_uri   text,             -- e.g. drive://folder/file.md
    mime_type    text,
    metadata     jsonb default '{}'::jsonb,
    created_at   timestamptz not null default now(),
    updated_at   timestamptz not null default now()
);

create index if not exists documents_category_idx
    on documents (category);

create index if not exists documents_metadata_gin_idx
    on documents using gin (metadata);

-- ----------------------------------------------------------------------------
-- document_chunks — one row per chunk of a document, with an embedding
--
-- Default vector dimension is 1024 (matches `liquid/lfm-2.5-embedding-350m:free`
-- via OpenRouter). If you switch to OpenAI (1536) or Google (768), change the
-- dimension here AND in the match_documents function below.
-- ----------------------------------------------------------------------------
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

-- HNSW index for fast cosine similarity search
create index if not exists document_chunks_embedding_hnsw_idx
    on document_chunks using hnsw (embedding vector_cosine_ops);

-- ----------------------------------------------------------------------------
-- chat_sessions — one row per active Telegram conversation flow
-- ----------------------------------------------------------------------------
create table if not exists chat_sessions (
    id                uuid primary key default gen_random_uuid(),
    telegram_user_id  bigint not null,
    telegram_chat_id  bigint not null,
    state             text   not null default 'idle',
    context           jsonb  not null default '{}'::jsonb,
    created_at        timestamptz not null default now(),
    updated_at        timestamptz not null default now(),
    closed_at         timestamptz
);

create unique index if not exists chat_sessions_active_user_idx
    on chat_sessions (telegram_user_id)
    where closed_at is null;

create index if not exists chat_sessions_chat_id_idx
    on chat_sessions (telegram_chat_id);

-- ----------------------------------------------------------------------------
-- chat_messages — append-only history of a session
-- ----------------------------------------------------------------------------
create table if not exists chat_messages (
    id           uuid primary key default gen_random_uuid(),
    session_id   uuid not null references chat_sessions (id) on delete cascade,
    role         text not null check (role in ('user', 'assistant', 'system', 'tool')),
    content      text not null,
    metadata     jsonb default '{}'::jsonb,
    created_at   timestamptz not null default now()
);

create index if not exists chat_messages_session_id_idx
    on chat_messages (session_id, created_at);

-- ----------------------------------------------------------------------------
-- drafts — produced letter drafts (for history + later export)
-- ----------------------------------------------------------------------------
create table if not exists drafts (
    id           uuid primary key default gen_random_uuid(),
    session_id   uuid references chat_sessions (id) on delete set null,
    user_id      bigint,                 -- telegram user id
    title        text,
    body         text not null,
    placeholders jsonb default '[]'::jsonb,  -- list of {field, value?, required}
    sources      jsonb default '[]'::jsonb,  -- list of {document_id, contribution}
    status       text not null default 'pending_review'
                 check (status in ('pending_review', 'approved', 'sent', 'rejected')),
    created_at   timestamptz not null default now(),
    updated_at   timestamptz not null default now()
);

create index if not exists drafts_user_id_idx on drafts (user_id, created_at desc);

-- ----------------------------------------------------------------------------
-- RPC: match_documents — vector similarity search over chunks
-- ----------------------------------------------------------------------------
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

comment on function match_documents is
'Cosine-similarity search over document_chunks. Returns rows with similarity
 strictly above match_threshold, ordered by descending similarity.';

-- ----------------------------------------------------------------------------
-- updated_at trigger — keep updated_at fresh on documents + chat_sessions
-- ----------------------------------------------------------------------------
create or replace function set_updated_at() returns trigger
language plpgsql as $$
begin
    new.updated_at := now();
    return new;
end;
$$;

drop trigger if exists documents_set_updated_at on documents;
create trigger documents_set_updated_at
    before update on documents
    for each row execute function set_updated_at();

drop trigger if exists chat_sessions_set_updated_at on chat_sessions;
create trigger chat_sessions_set_updated_at
    before update on chat_sessions
    for each row execute function set_updated_at();

drop trigger if exists drafts_set_updated_at on drafts;
create trigger drafts_set_updated_at
    before update on drafts
    for each row execute function set_updated_at();

-- ----------------------------------------------------------------------------
-- Row Level Security (RLS) — locked down by default; service role bypasses.
-- Enable only if you build a client-side app later.
-- ----------------------------------------------------------------------------
alter table documents         enable row level security;
alter table document_chunks   enable row level security;
alter table chat_sessions     enable row level security;
alter table chat_messages     enable row level security;
alter table drafts            enable row level security;
