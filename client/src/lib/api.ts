/**
 * Tiny typed wrapper around the Kateb REST API.
 *
 * Endpoints used:
 *   GET  /api/admin/health
 *   POST /api/admin/upload             (multipart)
 *   GET  /api/admin/documents?category=...
 *   POST /api/admin/documents/{id}/deactivate
 *   POST /api/admin/documents/{id}/activate
 *   POST /api/admin/documents/{id}/reprocess
 *   POST /api/admin/documents/{id}     (DELETE body)
 *   GET  /api/admin/stats
 *   POST /api/letters/generate/status
 *   POST /api/letters/generate
 *   POST /api/letters/generate/docx
 *   POST /api/letters/generate/pdf
 */

const ADMIN_TOKEN =
  (import.meta as any).env?.VITE_ADMIN_TOKEN || 'dev'

// Long-running letter generation must go directly to the production API.
// Routing it through the Vercel rewrite causes the proxy to time out before
// the AI pipeline and DOCX export finish.
const API_BASE =
  (import.meta as any).env?.VITE_API_BASE_URL ||
  ((import.meta as any).env?.PROD ? 'https://api.katibai.xyz' : '')

function apiUrl(path: string): string {
  return `${API_BASE}${path}`
}

function headers(extra: Record<string, string> = {}): Record<string, string> {
  return { 'X-Admin-Token': ADMIN_TOKEN, ...extra }
}

async function jsonFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(apiUrl(path), {
    ...init,
    headers: headers(
      init.body && !(init.body instanceof FormData)
        ? { 'Content-Type': 'application/json' }
        : {},
    ),
  })
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    throw new Error(`HTTP ${res.status} ${res.statusText}: ${text.slice(0, 200)}`)
  }
  return (await res.json()) as T
}

// ---------------- Letters ----------------

export type LetterIntent = {
  letter_type: string
  letter_type_ar: string
  fields: { name: string; label: string }[]
  confidence: number
}

export type Citation = {
  label: string
  title: string
  chunk_index: string
  similarity: string
}

export type DraftShape = {
  body: string
  body_only: string
  sources_block: string
  claim_count: number
  has_unverified: boolean
  citations: Citation[]
  model: string
}

export type ComplianceShape = {
  verdict: 'ok' | 'fixable' | 'unverifiable'
  duplicate_segments: string[]
  incomplete_sentences: string[]
  unintended_placeholders: string[]
  unverified_claims: string[]
  wrong_names: string[]
  unsupported_facts: string[]
  unprofessional_phrasing: string[]
  contradictions: string[]
  summary_ar: string
  suggested_corrections: string
}

export type ChunkUsed = {
  document_id: string
  file_id: string
  version: number
  chunk_index: number
  title: string
  category: string
  similarity: number
}

export type TemplateProfile = {
  has_body_marker: boolean
  placeholder_names: string[]
  rtl: boolean
  paragraph_count: number
  section_count: number
}

// --- Phase 4: per-intent LetterStyle (the new style-driven default) ---
// Mirrors rag.export.LetterStyle. When the style-driven path is used
// (use_legacy_template=False), the backend returns the resolved style
// here so the UI can show the user "what formatting was applied".
export type LetterStyle = {
  rtl: boolean
  font_name: string
  font_name_arabic: string
  font_size_pt: number
  paragraph_spacing_pt: number
  line_spacing: number
  page_size: 'A4' | 'Letter'
  margin_top_cm: number
  margin_bottom_cm: number
  margin_left_cm: number
  margin_right_cm: number
  include_basmala: boolean
  include_date_row: boolean
  include_recipient_block: boolean
  include_signature_block: boolean
  include_closing_phrase: boolean
  closing_phrase: string
  signature_title: string
  signature_name_placeholder: string
  basmala_text: string
  greeting_text: string
  body_alignment: 'right' | 'center' | 'left' | 'justified'
  use_arabic_numerals: boolean
}

// Per-call overrides for LetterStyle. Only the keys the user wants to
// change should be present. Unknown keys are rejected by the backend.
export type LetterStyleOverrides = Partial<LetterStyle>

export type GenerateResult = {
  ok: boolean
  final_status: 'ok' | 'fixable' | 'unverifiable' | 'needs_review' | 'no_template' | 'failed'
  needs_review: boolean
  error: string | null
  draft_id: string | null
  pdf_available: boolean
  intent: LetterIntent | null
  draft: DraftShape | null
  compliance: ComplianceShape | null
  corrected_draft: DraftShape | null
  re_compliance: ComplianceShape | null
  final_draft: DraftShape | null
  chunks_used: ChunkUsed[]
  template_profile: TemplateProfile | null
  // Phase 4: the resolved LetterStyle (only present on the style-driven
  // path). Null when the legacy template path is used.
  letter_style: LetterStyle | null
  docx_url: string | null
  pdf_url: string | null
  docx_base64: string | null
  stages: { stage: string; status: string; note: string }[]
}

export async function getHealth(): Promise<{ status: string }> {
  return jsonFetch('/api/admin/health')
}

export async function generateLetterStatus(body: {
  request: string
  fields?: Record<string, string>
}): Promise<{
  pdf_available: boolean
  intent: LetterIntent & { template_query: string; policy_query: string; regulation_query: string }
  templates: { id: string; title: string; category: string; similarity: number }[]
  policies: { id: string; title: string; category: string; similarity: number }[]
  regulations: { id: string; title: string; category: string; similarity: number }[]
}> {
  return jsonFetch('/api/letters/generate/status', {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

export async function generateLetter(body: {
  request: string
  fields?: Record<string, string>
  upload_outputs?: boolean
  // Phase 4: when True, force the legacy template-driven export path.
  // Default (false) is the style-driven path: the backend picks the
  // LetterStyle from the intent and builds a fresh DOCX, no template
  // file required.
  use_legacy_template?: boolean
  // Phase 4: per-request LetterStyle field overrides. Applied on top
  // of the per-intent default style. Ignored when use_legacy_template=True.
  style_overrides?: LetterStyleOverrides
}): Promise<GenerateResult> {
  return jsonFetch('/api/letters/generate', {
    method: 'POST',
    body: JSON.stringify({ upload_outputs: false, ...body }),
  })
}

export async function downloadDocx(body: {
  request: string
  fields?: Record<string, string>
  use_legacy_template?: boolean
  style_overrides?: LetterStyleOverrides
}): Promise<Blob> {
  const res = await fetch(apiUrl('/api/letters/generate/docx'), {
    method: 'POST',
    headers: headers({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    throw new Error(`HTTP ${res.status}: ${text.slice(0, 200)}`)
  }
  return res.blob()
}

export async function downloadPdf(body: {
  request: string
  fields?: Record<string, string>
}): Promise<Blob> {
  const res = await fetch(apiUrl('/api/letters/generate/pdf'), {
    method: 'POST',
    headers: headers({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    throw new Error(`HTTP ${res.status}: ${text.slice(0, 200)}`)
  }
  return res.blob()
}

// ---------------- Admin / documents ----------------

export type DocCategory = 'templates' | 'national_regulations' | 'internal_policies' | 'examples'

export type DocumentRow = {
  id: string
  title: string
  category: DocCategory
  bucket: string | null
  current_version: number
  is_active: boolean
  uploaded_by: string | null
  processing_status: string
  processing_progress: number
  processing_error: string | null
  char_count: number | null
  chunk_count: number | null
  created_at: string
  updated_at: string
  files: {
    id: string
    version: number
    storage_bucket: string
    storage_path: string
    mime_type: string
    char_count: number | null
    chunk_count: number | null
    created_at: string
  }[]
}

export type Stats = {
  total: number
  by_category: Record<string, number>
  by_status: Record<string, number>
}

export async function listDocuments(category?: DocCategory): Promise<DocumentRow[]> {
  const q = category ? `?category=${category}` : ''
  const r = await jsonFetch<{ documents: DocumentRow[] }>(`/api/admin/documents${q}`)
  return r.documents || []
}

export async function getStats(): Promise<Stats> {
  const r = await jsonFetch<{ stats: Stats }>('/api/admin/stats')
  return r.stats
}

export async function uploadDocument(file: File, category: DocCategory, title?: string): Promise<DocumentRow> {
  const fd = new FormData()
  fd.append('file', file)
  fd.append('category', category)
  // The upload endpoint requires `title` (FastAPI Form(...)). If the
  // caller didn't pass one explicitly, fall back to the file's name
  // (without extension) so the upload never 422s.
  const finalTitle = (title && title.trim()) || file.name.replace(/\.[^/.]+$/, '')
  fd.append('title', finalTitle)
  return jsonFetch<DocumentRow>('/api/admin/upload', { method: 'POST', body: fd })
}

export async function reprocessDocument(id: string): Promise<void> {
  await jsonFetch(`/api/admin/documents/${id}/reprocess`, { method: 'POST' })
}

export async function deactivateDocument(id: string): Promise<void> {
  await jsonFetch(`/api/admin/documents/${id}/deactivate`, { method: 'POST' })
}

export async function activateDocument(id: string): Promise<void> {
  await jsonFetch(`/api/admin/documents/${id}/activate`, { method: 'POST' })
}

export async function deleteDocument(id: string): Promise<void> {
  await jsonFetch(`/api/admin/documents/${id}`, { method: 'DELETE' })
}

// ---------------- Helpers ----------------

export const CATEGORY_AR: Record<DocCategory, string> = {
  templates:            'قوالب الخطابات',
  national_regulations: 'اللوائح الوطنية',
  internal_policies:    'السياسات الداخلية',
  examples:             'النماذج',
}

export const CATEGORY_DESC: Record<DocCategory, string> = {
  templates:            'نماذج جاهزة للخطابات الرسمية بأنواعها',
  national_regulations: 'اللوائح والأنظمة الصادرة من الجهات الرسمية',
  internal_policies:    'السياسات والإجراءات المعتمدة في الجمعية',
  examples:             'خطابات سابقة للاطلاع',
}
