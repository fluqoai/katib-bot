// Supabase Edge Function: storage-webhook
// Triggered when a file is uploaded to one of the kateb-* buckets
// (templates, policies, regulations, examples). The function creates a
// `document_files` row + a `processing_jobs` row so the long-running
// Python worker can pick it up.
//
// Deploy:
//   supabase functions deploy storage-webhook --no-verify-jwt
// Configure a Storage webhook in the dashboard pointing at this URL,
// with the events: bucket.name = "templates" OR "policies" OR
// "regulations" OR "examples", event = "INSERT".
//
// Auth: this function uses the SERVICE_ROLE_KEY (env) — it runs server-side
// and is NOT exposed to the public.

import { createClient } from "https://esm.sh/@supabase/supabase-js@2.45.0";

const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
const SERVICE_ROLE = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;

const VALID_BUCKETS = new Set(["templates", "policies", "regulations", "examples"]);
const BUCKET_TO_CATEGORY: Record<string, string> = {
  templates: "templates",
  policies: "internal_policies",
  regulations: "national_regulations",
  examples: "examples",
};

interface StorageWebhookPayload {
  type: "INSERT" | "UPDATE" | "DELETE";
  table: string;
  record: {
    id: string;
    bucket_id: string;
    name: string;          // path within the bucket (e.g. "abc/v1/file.pdf")
    metadata?: { size?: number; mimetype?: string };
  };
}

Deno.serve(async (req) => {
  if (req.method !== "POST") {
    return new Response("method not allowed", { status: 405 });
  }
  let payload: StorageWebhookPayload;
  try {
    payload = await req.json();
  } catch {
    return new Response("invalid JSON", { status: 400 });
  }

  if (payload.type !== "INSERT" || payload.table !== "objects") {
    return new Response(JSON.stringify({ skipped: true }), {
      headers: { "content-type": "application/json" },
    });
  }

  const rec = payload.record;
  if (!VALID_BUCKETS.has(rec.bucket_id)) {
    return new Response(JSON.stringify({ skipped: "unknown bucket" }), {
      headers: { "content-type": "application/json" },
    });
  }

  // Path convention we use everywhere: <doc_id>/v<n>/<filename>
  // We use the first path segment as the document id.
  const parts = rec.name.split("/").filter(Boolean);
  if (parts.length < 3) {
    return new Response(JSON.stringify({ skipped: "bad path" }), {
      status: 400, headers: { "content-type": "application/json" },
    });
  }
  const [docId, versionPart, ...rest] = parts;
  const filename = rest.join("/");
  const m = versionPart.match(/^v(\d+)$/i);
  if (!m) {
    return new Response(JSON.stringify({ skipped: "bad version segment" }), {
      status: 400, headers: { "content-type": "application/json" },
    });
  }
  const version = parseInt(m[1], 10);

  const sb = createClient(SUPABASE_URL, SERVICE_ROLE);

  // 1) Ensure the document row exists (idempotent — re-uses if doc_id
  //    already points at a row).
  const category = BUCKET_TO_CATEGORY[rec.bucket_id];
  const docRow = await sb.from("documents").upsert({
    id: docId,
    title: filename.replace(/\.[^.]+$/, ""),
    category,
    bucket: rec.bucket_id,
    is_active: true,
    content: "",
    source_uri: `storage://${rec.bucket_id}/${rec.name}`,
  }, { onConflict: "id" }).select().single();

  if (docRow.error) {
    return new Response(JSON.stringify({ error: docRow.error.message }), {
      status: 500, headers: { "content-type": "application/json" },
    });
  }

  // 2) Create the document_files row (idempotent on (document_id, version))
  const fileRow = await sb.from("document_files").upsert({
    document_id: docId,
    version,
    storage_bucket: rec.bucket_id,
    storage_path: rec.name,
    mime_type: rec.metadata?.mimetype ?? null,
    size_bytes: rec.metadata?.size ?? null,
    original_filename: filename,
    status: "pending",
    processing_progress: {
      total_chunks: 0,
      processed_chunks: 0,
      failed_chunks: 0,
      current_batch: 0,
      retry_count: 0,
      last_error: null,
      started_at: null,
      updated_at: new Date().toISOString(),
    },
  }, { onConflict: "document_id,version" }).select().single();

  if (fileRow.error) {
    return new Response(JSON.stringify({ error: fileRow.error.message }), {
      status: 500, headers: { "content-type": "application/json" },
    });
  }

  // 3) Enqueue a processing job
  const job = await sb.from("processing_jobs").insert({
    document_file_id: fileRow.data.id,
    status: "pending",
    priority: 100,
  }).select().single();

  if (job.error) {
    return new Response(JSON.stringify({ error: job.error.message }), {
      status: 500, headers: { "content-type": "application/json" },
    });
  }

  return new Response(JSON.stringify({
    ok: true,
    document_id: docId,
    file_id: fileRow.data.id,
    job_id: job.data.id,
  }), {
    headers: { "content-type": "application/json" },
  });
});
