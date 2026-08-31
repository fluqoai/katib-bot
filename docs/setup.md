# Setup walkthrough

This document walks you through taking the Kateb project from a fresh
checkout to a running Telegram bot, end to end. Estimated time: 30–60
minutes the first time; 5 minutes per new document added afterwards.

## 0. Prerequisites

- Windows 10/11 with PowerShell
- Python 3.11 or newer
- A Supabase project (NEW, not your Soulvd one)
- A Telegram bot token (from [@BotFather](https://t.me/BotFather))
- An OpenRouter API key (https://openrouter.ai)
- An OpenAI API key (for embeddings) **or** a Google Generative AI key

You do **not** need a Google Cloud project, a service account, or any
Google Drive setup. That's the optional path B.

## 1. Fill in `.env`

Copy `.env.example` to `.env` and set these four groups. Everything else
is optional.

### 1.1 Telegram

```powershell
# Talk to @BotFather, send /newbot, follow the prompts, paste the token.
TELEGRAM_BOT_TOKEN=110201543:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw
# Optional allow-list. Leave empty for open access.
TELEGRAM_ALLOWED_CHAT_IDS=
```

To find your chat ID, message [@userinfobot](https://t.me/userinfobot).

### 1.2 Supabase (new project, new auth model)

Create a new Supabase project at https://supabase.com/dashboard (do **not**
reuse the Soulvd ref). Then:

```powershell
SUPABASE_URL=https://<your-new-ref>.supabase.co
SUPABASE_PROJECT_REF=<your-new-ref>
SUPABASE_SECRET_KEY=sb_secret_...    # Settings → API → "Secret key"
SUPABASE_PUBLISHABLE_KEY=sb_publishable_...   # not used by the bot
```

The new Supabase auth model has two server-side keys:

- **Secret key** (`sb_secret_...`) — what the bot uses, full server-side
  access, bypasses RLS.
- **Publishable key** (`sb_publishable_...`) — for client-side / browser
  use. Not needed for the bot.

If your project is still on the old JWT-style auth, set
`SUPABASE_SERVICE_ROLE_KEY` to the long `eyJ...` value instead.

### 1.3 OpenRouter (LLM)

```powershell
OPENROUTER_API_KEY=sk-or-v1-...
# Good defaults: openai/gpt-4o, anthropic/claude-3.5-sonnet, google/gemini-2.0-flash
OPENROUTER_MODEL=openai/gpt-4o
```

### 1.4 Embeddings

```powershell
EMBEDDING_PROVIDER=openai
OPENAI_API_KEY=sk-...
OPENAI_EMBEDDING_MODEL=text-embedding-3-small
```

> **Important:** the embedding model's output dimension must match
> `db/schema.sql`. Default is **1536** (text-embedding-3-small). If you
> use Google's text-embedding-004 (768), edit `db/schema.sql` to change
> `vector(1536)` to `vector(768)` in two places, and the same in
> `match_documents`.

## 2. Install dependencies

```powershell
cd C:\Users\khayrat\Desktop\MyProjects\kateb
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## 3. Apply the database schema

Open the Supabase SQL editor for your new project
(`https://supabase.com/dashboard/project/<ref>/sql/new`), paste the
contents of `db/schema.sql`, and click **Run**.

If you'd rather use a script (works only if you've created an `exec_sql`
RPC on your project), try `python scripts/init_db.py` — otherwise paste
manually as above.

## 4. Add your first reference documents

This is the **only** step your client will indirectly depend on. The
bot's quality is bounded by the quality of what's in the index, so add
real letters and regulations from your archive, not invented samples.

```powershell
# templates — actual letter templates your org has sent
python scripts\add_doc.py --title "نموذج طلب شراكة" --category templates --file samples\partnership.md

# national_regulations — relevant Saudi laws / regulations
python scripts\add_doc.py --title "نظام الجمعيات الأهلية" --category national_regulations --file samples\cso-law.md

# internal_policies — your org's own policies
python scripts\add_doc.py --title "لائحة الشراكات الداخلية" --category internal_policies --file samples\internal-partnerships.md
```

You should see:

```
✅ inserted: 'نموذج طلب شراكة'
   id      = ...
   source  = manual://نموذج-طلب-شراكة
   chunks  = 3
```

To see what's in the index at any time:

```powershell
python scripts\list_docs.py
```

To remove one (e.g. you uploaded a wrong file):

```powershell
python scripts\delete_doc.py --source-uri manual://نموذج-طلب-شراكة
```

For best RAG results, add **at least one document per category** the
skill requires: `templates`, `national_regulations`, `internal_policies`.
The bot will fall back to "no similar reference" mode if a category is
empty, but the output quality is much higher with real examples.

## 4b. Optional: Tesseract + LibreOffice for legacy file formats

The ingestion pipeline handles every common file format **gracefully**,
but two formats need system-level helpers. If they're not installed, the
pipeline will still run — it just records those files in the DB with a
non-`indexed` status (`needs_ocr` or `needs_doc_conversion`) so you can
see exactly which ones need attention.

### Tesseract (for scanned PDFs)

Some PDFs are image-only (no text layer). The PDF loader detects this
and falls back to OCR via Tesseract. **The Arabic language pack must
be installed** for Arabic scans to read correctly.

1. **Windows installer**: https://github.com/UB-Mannheim/tesseract/wiki
   - During install, tick **"Additional language data"** → **Arabic (ara)**.
2. After install, verify the binary is on `PATH`:
   ```powershell
   tesseract --version
   tesseract --list-langs   # should include "ara"
   ```
3. If Tesseract is in a non-standard location, the loader also checks
   `C:\Program Files\Tesseract-OCR\tesseract.exe` automatically.

### LibreOffice (for old `.doc` files)

Old Microsoft Word files (`.doc`, pre-2007 binary format) can't be read
by `python-docx`. The `.doc` loader auto-converts them via headless
LibreOffice in a **temp directory** — the original file is never
modified.

1. **Windows installer**: https://www.libreoffice.org/download/download-libreoffice/
2. After install, `soffice.exe` is usually at
   `C:\Program Files\LibreOffice\program\soffice.exe`. The loader
   checks `PATH` and the common install location automatically.
3. Verify:
   ```powershell
   soffice --version
   ```

### What happens without these tools

The ingestion script does **not** crash — it logs a warning per file
and records the status in Supabase. The validation report
(`INDEX_STATUS.txt`) and the in-DB `status` column will show:

| File type | Without tool        | With tool                |
|-----------|---------------------|--------------------------|
| Scanned PDF  | `needs_ocr`     | `indexed` (via Tesseract)|
| Old `.doc`   | `needs_doc_conversion` | `indexed` (via LibreOffice) |

You can install the tools later, then re-run:
```powershell
python scripts\ingest_local.py reference_docs
```
The previously-failed files will be re-evaluated and their status updated
automatically.

## 5. Run the bot

```powershell
python scripts\run_bot.py
```

You should see `Kateb starting (env=dev)` in the console. Open Telegram,
find your bot, and send `/start`. The bot should reply with the welcome
message and the main menu.

## 6. Sanity test

Send the bot:

> اكتب لي خطاب طلب شراكة مع وزارة الثقافة

The bot should:

1. Show a keyboard to pick the type ("طلب شراكة" → tap it)
2. Search the index, show matching references
3. Ask for missing fields
4. After you fill some in (or send "متابعة"), return a draft in
   Markdown with `# مسودة — للمراجعة` at the top, `{{…}}` placeholders
   for values you didn't provide, and a checklist at the end.

## 7. Adding more documents over time

Just run `add_doc.py` again. The script is idempotent on `source_uri`,
so re-running with the same title will update rather than duplicate.

If your reference library grows large (>50 docs), consider switching to
the optional Drive sync path — see the next section.

---

## Optional path B: Google Drive sync

Skip this section if you don't use Drive. The one-off `add_doc.py` path
is enough for a small reference library.

If you want to bulk-sync from a shared Drive folder (e.g. for a growing
multi-author corpus), set up the Drive integration:

1. **Google Cloud**:
   - Create or pick a project at https://console.cloud.google.com
   - Enable the **Google Drive API**
   - Create a **Service Account** (IAM & Admin → Service Accounts)
   - Create a **JSON key** and save it to
     `kateb/secrets/service-account.json` (gitignored)
   - Open the Drive folder you want to ingest, click **Share**, paste
     the service account's email as a **Viewer**

2. **Update `.env`**:
   ```powershell
   GOOGLE_SERVICE_ACCOUNT_JSON=./secrets/service-account.json
   GOOGLE_DRIVE_ROOT_FOLDER_ID=1AbCdEf...   # from the Drive URL
   ```

3. **Run the sync**:
   ```powershell
   python scripts\ingest_drive.py --folder <DRIVE_FOLDER_ID>
   ```

The script will walk the tree, infer categories from folder names
(`templates/`, `لوائح/`, `سياسات/`, `أمثلة/` are recognized), and
upsert each file. The logic in `drive/sync.py` calls into
`rag/ingest.py` — same store as `add_doc.py`, so the two paths are
compatible.

## Going to production

The v1 setup is single-process, in-memory session storage, and uses
`run_polling` (good enough for a single-user bot). For a public bot:

1. Replace the in-memory `SessionStore` with a `SupabaseSessionStore`
   using the `chat_sessions` table (sketch in `bot/states.py`).
2. Switch the bot to webhooks: see `python-telegram-bot`'s
   `Application.run_webhook()`.
3. Add a reverse proxy (Caddy / nginx) in front.
4. Add a `Procfile` + Docker image for deploy.
5. Wire `drafts.status` to update when the user actually sends the
   letter from their own client.
