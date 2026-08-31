# Architecture

## High-level

```
                 Telegram
                    │
                    ▼
       ┌────────────────────────┐
       │  bot/  (python-        │
       │   telegram-bot v21)    │
       │                        │
       │  • main.py             │
       │  • handlers/           │    ┌──────────────┐
       │  • states.py           │ ◄──┤  SessionStore│
       │  • config.py           │    │  (in-memory  │
       └──────────┬─────────────┘    │   v1)        │
                  │                  └──────────────┘
                  ▼
       ┌────────────────────────┐
       │  rag/                  │
       │                        │
       │  1. Embed the request  │
       │  2. Search 3 categories│
       │     in Supabase        │──────►  Supabase
       │  3. Build the prompt   │         (pgvector)
       │  4. Call OpenRouter    │         • documents
       │  5. Parse JSON resp.   │         • document_chunks
       │                        │         • chat_sessions
       └──────────┬─────────────┘         • chat_messages
                  │                       • drafts
                  ▼
       ┌────────────────────────┐
       │  scripts/add_doc.py    │         (developer-only)
       │  ────────────────────  │
       │  One-off per reference │
       │  document.             │
       │  The ONLY way the dev  │
       │  populates the corpus. │
       └────────────────────────┘

   (Optional path B: drive/sync.py walks a Drive tree and calls
    the same rag/ingest.ingest_document function. Off by default.)
```

## Components

### `bot/`

Telegram-side interface. Uses `python-telegram-bot` v21+ with native
asyncio. The Application holds references to the RAG components in
`bot_data` so handlers can reach them.

The client only ever sees this layer. Everything else is invisible.

**State machine** (`bot/states.py`):

```
IDLE ──(free text)──► WAITING_FOR_TYPE ──(pick)──► WAITING_FOR_FIELDS
                                                          │
                                                       (متابعة)
                                                          ▼
                                                  (draft produced)
                                                          │
                                                          ▼
                                                CONFIRMING_DRAFT
                                                  │     │     │
                                         approve  edit  regen  discard
```

v1 stores state in an in-memory dict. To survive restarts or run
multiple bot instances, swap in the `SupabaseSessionStore` sketched in
the same file.

### `rag/`

Four concerns, four files:

- **`embeddings.py`** — provider-agnostic embedding client (OpenAI or
  Google). Returns `dimension`-length vectors.
- **`search.py`** — wraps the `match_documents` RPC. Runs once per
  category (the skill requires three categories: templates, national
  regulations, internal policies).
- **`ingest.py`** — the single entry point for adding a document to
  the index. Used by both `scripts/add_doc.py` (recommended) and
  `drive/sync.py` (optional).
- **`generator.py`** — builds the prompt from the skill + the retrieved
  context, calls OpenRouter with `response_format: {type: json_object}`,
  parses the JSON response into a `GeneratedDraft`.

The skill procedure lives outside this repo (in
`~/.minimax/skills/arabic-official-correspondence/SKILL.md`) and is
read at runtime by `prompts.load_skill_body`. This means the bot stays
in sync with the skill — update the skill, the bot picks it up next
start.

### `scripts/`

The three developer-facing scripts. **Only you ever run these.**

- `add_doc.py` — add one document (file path or paste). One command per
  doc. Idempotent on `source_uri`.
- `list_docs.py` — pretty-print the current index.
- `delete_doc.py` — remove a doc by id or source_uri, with confirm.

### `drive/` (optional)

One-shot ingestion. Walks a Drive tree, then delegates the actual
storage to `rag/ingest.py`. Off by default. Use it if your reference
library is too big to manage one doc at a time, or if multiple authors
edit the same Drive folder.

### `db/schema.sql`

Five tables:

- `documents` — one row per source file, with `category` enum
- `document_chunks` — embeddings, HNSW-indexed for cosine search
- `chat_sessions` — per-Telegram-user conversation state
- `chat_messages` — append-only history
- `drafts` — produced letter drafts, with placeholder and source lists

Plus one RPC: `match_documents(query_embedding, threshold, count,
filter_category)`. That's the only entry point the RAG search needs.

## Why these choices

- **Supabase + pgvector** instead of a separate vector DB. One less
  moving part, RLS works the same way for vectors as for rows, and the
  same REST API serves both metadata and search.
- **OpenRouter** for the LLM gateway. Lets the user pick any model
  (OpenAI, Anthropic, Google) without code changes — just env var.
- **Skill file as a runtime dependency** instead of inlining the
  procedure. The skill is editable in one place, the bot is just a
  consumer.
- **One ingestion entry point** (`rag/ingest.ingest_document`). The
  Drive sync and the one-off `add_doc.py` both call into it, so the
  store logic lives in exactly one place. New ingestion paths (email
  upload, web admin, etc.) plug in by calling the same function.
- **In-memory sessions in v1.** A single-user bot in dev doesn't need
  distributed state. The Supabase schema is already there for v2.
- **JSON-only LLM output.** Easier to parse reliably than free-form
  prose. The model is told in the system prompt to wrap the response
  in a strict schema.

## What's not in v1

- Authentication on the bot (only the optional `TELEGRAM_ALLOWED_CHAT_IDS`)
- Webhook deployment (only polling)
- Multi-language support (Arabic only)
- PDF / DOCX export of the draft (output is Markdown in the chat)
- Persistence of conversation history beyond one session
- A web admin UI for managing the document index
- Google Drive integration (the code is there, but it's an opt-in path)
