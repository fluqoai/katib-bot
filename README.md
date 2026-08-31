# كاتب (Kateb) — Arabic Writing Assistant

A Telegram bot that helps a non-technical end user draft formal Arabic
correspondence in their own voice. The bot reads reference documents that
**you (the developer)** have ingested into a Supabase index, then
generates drafts in فصحى with `{{placeholder}}` markers for any value the
user did not provide.

> **The client only ever talks to the Telegram bot.** They never see
> Supabase, embeddings, or any code. You handle the technical setup
> once, then add new reference documents on demand via a single
> command.

## How the end user experiences it

```
User (Telegram)            Bot (python-telegram-bot)            Backend
─────────────              ────────────────────────             ───────
"اكتب لي خطاب             /write handler                       Supabase
 طلب شراكة                  ↓                                  (pgvector
 مع وزارة الثقافة"         embed(query)                          search)
                            search 3 categories ──────────────►   ↓
                            build prompt (skill + context)        chunks
                            OpenRouter call
                            parse JSON → draft
                            send draft + checklist
                                                        OpenRouter
                                                        (LLM)
```

## Two ways to populate the index

| | **A. One-off per doc (recommended)** | **B. Google Drive sync (optional)** |
|---|---|---|
| Developer time | Seconds per doc | One-time setup + a Drive folder |
| For the client | Invisible | Invisible |
| Google Cloud needed? | No | Yes (service account) |
| How docs are added | `python scripts/add_doc.py --title ... --category ... --file ...` | `python scripts/ingest_drive.py --folder <id>` |
| Best for | Small reference library that changes rarely | A growing, multi-author corpus |

The current default assumes **path A**. The Drive code is in `drive/`
and `scripts/ingest_drive.py` and stays as an opt-in for later.

## Project layout

```
kateb/
├── README.md
├── .env.example                    ← only the four credential groups
├── .gitignore
├── pyproject.toml
├── requirements.txt
│
├── db/schema.sql                   ← 5 tables + match_documents RPC
│
├── rag/                            ← Retrieval-Augmented Generation
│   ├── embeddings.py               ← OpenAI or Google embedder
│   ├── search.py                   ← pgvector similarity, 3 categories
│   ├── ingest.py                   ← single ingestion entry point
│   ├── prompts.py                  ← reads the skill at runtime
│   ├── generator.py                ← OpenRouter call + JSON parser
│   └── types.py
│
├── bot/                            ← Telegram interface
│   ├── main.py
│   ├── config.py
│   ├── states.py
│   ├── keyboards.py
│   └── handlers/
│       ├── start.py                ← /start, /help, /cancel
│       ├── write.py                ← the main writing flow
│       └── draft_persist.py
│
├── drive/                          ← OPTIONAL: bulk sync from Drive
│   ├── client.py
│   └── sync.py
│
├── scripts/
│   ├── init_db.py                  ← apply schema.sql
│   ├── add_doc.py                  ← ★ one-off per doc (recommended)
│   ├── list_docs.py                ← see what's in the index
│   ├── delete_doc.py               ← remove one
│   ├── run_bot.py
│   └── ingest_drive.py             ← OPTIONAL: bulk Drive sync
│
├── tests/
└── docs/
    ├── setup.md
    └── architecture.md
```

## Quick start (the four-step path)

1. **Fill in `.env`** — copy `.env.example` to `.env` and set:
   - `SUPABASE_URL` + `SUPABASE_SECRET_KEY` (new sb_secret_ model)
   - `TELEGRAM_BOT_TOKEN` (from @BotFather)
   - `OPENROUTER_API_KEY` + an embedding key (`OPENAI_API_KEY` is the
     easiest; `text-embedding-3-small` is the default)
2. **Apply the schema** — open the Supabase SQL editor for your new
   project, paste `db/schema.sql`, click *Run*. (Or, once
   `scripts/init_db.py` knows your connection, run it.)
3. **Add your first reference document**:
   ```powershell
   python scripts\add_doc.py --title "نموذج طلب شراكة" --category templates --file samples\partnership.md
   ```
   Add at least one per category (`templates`, `national_regulations`,
   `internal_policies`) so the RAG has something to retrieve. The
   `--category` argument is required.
4. **Run the bot**:
   ```powershell
   python scripts\run_bot.py
   ```

Hand the bot token to the client. They open Telegram, send `/start`, and
chat. They will never need to touch `.env`, Supabase, or any of this
code.

## What the bot does

- `/start` — greeting + main menu (ابدأ كتابة / بحث / مساعدة)
- Free-text message — treated as a draft request:
  1. Embed the request
  2. Similarity search across `templates`, `national_regulations`,
     `internal_policies` (the skill's three required categories)
  3. Ask the user for the missing field values
  4. Generate a فصحى draft with `{{placeholder}}` markers for any value
     the user did not provide
  5. Return the draft + a checklist of what to verify before sending
- `/search <query>` — direct search of the document index, no generation
- `/cancel` — abandon the current draft flow

## What this is NOT

- Not a content generator that fabricates specifics. Every numeric value,
  date, or identifier the user does not explicitly provide is left as a
  `{{placeholder}}` so nothing fake slips into an official letter.
- Not a "send" bot. Output is always a draft. The user reviews, edits,
  and sends the letter from their own mail client.

## See also

- `docs/setup.md` — full walkthrough including Drive-sync path
- `docs/architecture.md` — design choices and tradeoffs
