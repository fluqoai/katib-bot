# Supabase Edge Functions

## storage-webhook

Triggered when a file is uploaded to one of the Kateb storage buckets
(`templates`, `policies`, `regulations`, `examples`). The function:

1. Parses the path as `<doc_id>/v<n>/<filename>`.
2. Upserts the `documents` row (idempotent on `id`).
3. Upserts the `document_files` row (idempotent on `(document_id, version)`).
4. Inserts a `processing_jobs` row with `status='pending'`.

The long-running Python worker (`scripts/worker.py`) picks the job up
and does the actual chunking + embedding.

## Deploy

```bash
# Install the Supabase CLI (one-time)
#   https://supabase.com/docs/guides/cli
supabase login
supabase link --project-ref lpwbswgixbuhmfizutqm
supabase functions deploy storage-webhook --no-verify-jwt
```

After deploy, configure a Storage webhook in the dashboard:
- URL: the function URL from the deploy output
- Events: `INSERT` on `storage.objects`
- Filter: `bucket_id` IN (`templates`, `policies`, `regulations`, `examples`)

## Local testing

```bash
supabase functions serve storage-webhook --no-verify-jwt
# Then in another shell:
curl -X POST http://localhost:54321/functions/v1/storage-webhook \
  -H "Content-Type: application/json" \
  -d '{"type":"INSERT","table":"objects","record":{"id":"x","bucket_id":"templates","name":"abc/v1/file.pdf","metadata":{"size":1234,"mimetype":"application/pdf"}}}'
```

## Required env

Set in the Supabase dashboard (Edge Function → Secrets):

- `SUPABASE_URL`         — e.g. `https://lpwbswgixbuhmfizutqm.supabase.co`
- `SUPABASE_SERVICE_ROLE_KEY` — the long JWT service-role key
