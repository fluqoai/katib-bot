-- Migration 007: extend `drafts` for full letter-pipeline provenance.
--
-- The original `drafts` table (from schema.sql) was designed for the
-- chat-style draft flow. The letter pipeline produces richer provenance
-- (template, model, compliance verdict, chunk IDs, etc.) that the
-- admin UI surfaces as "المراجع التي اعتمد عليها الخطاب".
--
-- The official DOCX stays clean (no inline [source: ...] tags, no
-- ## المصادر section). Everything that links the letter back to
-- its sources lives in this row.

alter table drafts
    -- Pipeline outcome (independent of the human-review workflow status)
    add column if not exists final_status        text
        check (final_status in ('ok', 'fixable', 'unverifiable',
                                'needs_review', 'no_template', 'failed')),
    -- True iff the system could not safely auto-correct a fixable
    -- draft and a human must review it before sending.
    add column if not exists needs_review        boolean not null default false,
    -- The template the letter was based on (style source, not content)
    add column if not exists template_document_id uuid
        references documents (id) on delete set null,
    add column if not exists template_file_id     uuid
        references document_files (id) on delete set null,
    add column if not exists template_version     int,
    -- The model + timestamp
    add column if not exists model                text,
    add column if not exists generated_at         timestamptz not null default now(),
    -- Compliance review outcome (stored separately from `status`
    -- so the human workflow can co-exist with the AI verdict)
    add column if not exists compliance_verdict   text
        check (compliance_verdict in ('ok', 'fixable', 'unverifiable')),
    add column if not exists compliance_summary   text,
    add column if not exists compliance_unverified jsonb default '[]'::jsonb,
    -- Provenance: which chunks from which documents/files/versions
    -- fed into the generator. Distinct from `sources` which is the
    -- list of citations the LLM emitted; this is the full list of
    -- chunks that were available.
    add column if not exists chunks_used          jsonb default '[]'::jsonb,
    -- Body WITHOUT [source: ...] tags (the text that went into the
    -- official DOCX). `body` keeps the full version for review.
    add column if not exists clean_body           text;

-- Indexes for the admin UI: list drafts by user, ordered by recency
create index if not exists drafts_final_status_idx
    on drafts (final_status, generated_at desc);

create index if not exists drafts_template_idx
    on drafts (template_document_id, template_version);

-- Backfill clean_body for any existing drafts (best-effort: strip
-- inline citation tags from `body`). New rows will set it explicitly.
update drafts
   set clean_body = regexp_replace(body, '\s*\[source:\s*[^\]]+\]', '', 'g')
 where clean_body is null
   and body is not null
   and body like '%[source:%';
