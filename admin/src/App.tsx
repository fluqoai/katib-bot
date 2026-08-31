import { useEffect, useState } from 'react'
import { api, categoryLabel, statusLabel, type Category, type DocumentOut, type StatsOut } from './lib/api'

type TabId = 'templates' | 'national_regulations' | 'internal_policies'

const TABS: { id: TabId; ar: string; sub: string }[] = [
  { id: 'templates',           ar: 'قوالب الخطابات',  sub: 'قوالب جاهزة للخطابات الرسمية' },
  { id: 'national_regulations', ar: 'اللوائح الوطنية', sub: 'الأنظمة واللوائح المرجعية' },
  { id: 'internal_policies',   ar: 'السياسات الداخلية', sub: 'سياسات وإجراءات الجمعية' },
]

export default function App() {
  const [tab, setTab] = useState<TabId>('templates')
  const [docs, setDocs] = useState<DocumentOut[]>([])
  const [stats, setStats] = useState<StatsOut | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [showInactive, setShowInactive] = useState(false)

  async function refresh() {
    setLoading(true)
    setError(null)
    try {
      const [s, d] = await Promise.all([
        api.stats(),
        api.listDocuments({ category: tab, is_active: showInactive ? false : true }),
      ])
      setStats(s)
      setDocs(d)
    } catch (e: any) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { refresh() }, [tab, showInactive])
  // Lightweight polling for live progress
  useEffect(() => {
    const t = setInterval(refresh, 5000)
    return () => clearInterval(t)
  }, [tab, showInactive])

  return (
    <div className="min-h-screen flex flex-col">
      <header className="bg-sage-700 text-white shadow">
        <div className="max-w-6xl mx-auto px-6 py-4 flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-bold">كاتب — لوحة الإدارة</h1>
            <p className="text-sage-200 text-sm">إدارة قوالب ولوائح وسياسات النظام</p>
          </div>
          {stats && (
            <div className="flex gap-3 text-sm">
              <Stat label="وثائق" value={stats.documents} hint={`${stats.active_documents} نشط`} />
              <Stat label="مفهرس" value={stats.indexed_files} hint={`من ${stats.files}`} />
              <Stat label="معلّق" value={stats.pending_files} hint="ينتظر المعالجة" />
              {stats.failed_files > 0 && (
                <Stat label="فشل" value={stats.failed_files} hint="يحتاج إعادة" tone="rose" />
              )}
              {stats.needs_ocr_files > 0 && (
                <Stat label="يحتاج OCR" value={stats.needs_ocr_files} tone="amber" />
              )}
              {stats.needs_doc_conversion_files > 0 && (
                <Stat label="يحتاج .doc" value={stats.needs_doc_conversion_files} tone="amber" />
              )}
            </div>
          )}
        </div>
      </header>

      <nav className="bg-sage-50 border-b border-sage-200">
        <div className="max-w-6xl mx-auto px-6 flex">
          {TABS.map(t => (
            <button
              key={t.id}
              onClick={() => setTab(t.id)}
              className={`px-5 py-3 -mb-px border-b-2 font-medium transition-colors ${
                tab === t.id
                  ? 'border-sage-600 text-sage-800'
                  : 'border-transparent text-sage-600 hover:text-sage-800'
              }`}
            >
              {t.ar}
            </button>
          ))}
          <div className="flex-1" />
          <label className="flex items-center gap-2 text-sm text-sage-600 px-3 py-3">
            <input
              type="checkbox"
              checked={showInactive}
              onChange={e => setShowInactive(e.target.checked)}
              className="rounded text-sage-600 focus:ring-sage-500"
            />
            عرض غير النشطة
          </label>
        </div>
      </nav>

      <main className="max-w-6xl mx-auto px-6 py-8 flex-1 w-full">
        {error && (
          <div className="mb-4 p-4 bg-rose-50 border border-rose-200 text-rose-800 rounded">
            {error}
          </div>
        )}

        <UploadCard category={tab} onUploaded={refresh} />

        <div className="mt-8">
          <h2 className="text-lg font-semibold text-sage-800 mb-3">
            {TABS.find(t => t.id === tab)?.ar} ({docs.length})
          </h2>
          {loading && docs.length === 0 ? (
            <div className="text-sage-500">جاري التحميل…</div>
          ) : docs.length === 0 ? (
            <div className="p-8 text-center text-sage-500 bg-white border border-sage-200 rounded">
              لا توجد وثائق في هذا القسم بعد. ابدأ برفع ملف من الأعلى.
            </div>
          ) : (
            <ul className="space-y-3">
              {docs.map(d => (
                <DocumentRow key={d.id} doc={d} onChanged={refresh} />
              ))}
            </ul>
          )}
        </div>
      </main>

      <footer className="text-center text-xs text-sage-400 py-4">
        Kateb admin — Supabase + Python worker
      </footer>
    </div>
  )
}

function Stat({ label, value, hint, tone }: {
  label: string; value: number; hint?: string;
  tone?: 'rose' | 'amber' | 'default'
}) {
  const cls = tone === 'rose'
    ? 'bg-rose-50 text-rose-800 border-rose-200'
    : tone === 'amber'
    ? 'bg-amber-50 text-amber-800 border-amber-200'
    : 'bg-sage-50 text-sage-800 border-sage-200'
  return (
    <div className={`px-3 py-1.5 rounded border ${cls}`}>
      <div className="text-xs opacity-75">{label}</div>
      <div className="font-bold leading-tight">{value}</div>
      {hint && <div className="text-[10px] opacity-60">{hint}</div>}
    </div>
  )
}

function UploadCard({ category, onUploaded }: {
  category: TabId; onUploaded: () => void
}) {
  const [title, setTitle] = useState('')
  const [file, setFile] = useState<File | null>(null)
  const [uploading, setUploading] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [progress, setProgress] = useState<string | null>(null)
  const [drag, setDrag] = useState(false)

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    setErr(null)
    if (!file) { setErr('اختر ملفًا أولاً'); return }
    if (!title.trim()) { setErr('اكتب عنوانًا للوثيقة'); return }
    setUploading(true)
    setProgress('جاري قراءة الملف…')
    try {
      const fd = new FormData()
      fd.append('file', file)
      fd.append('title', title.trim())
      fd.append('category', category)
      fd.append('uploaded_by', 'admin')
      setProgress('جاري الفهرسة…')
      const r = await api.upload(fd)
      setProgress(`تمت إضافة "${r.title}" (الإصدار ${r.current_version})`)
      setTitle('')
      setFile(null)
      onUploaded()
      setTimeout(() => setProgress(null), 4000)
    } catch (e: any) {
      setErr(e.message)
    } finally {
      setUploading(false)
    }
  }

  return (
    <form
      onSubmit={submit}
      onDragOver={e => { e.preventDefault(); setDrag(true) }}
      onDragLeave={() => setDrag(false)}
      onDrop={e => {
        e.preventDefault()
        setDrag(false)
        const f = e.dataTransfer.files?.[0]
        if (f) setFile(f)
      }}
      className={`bg-white border-2 rounded-lg p-5 transition-colors ${
        drag ? 'border-sage-500 bg-sage-50' : 'border-sage-200'
      }`}
    >
      <h2 className="text-lg font-semibold text-sage-800 mb-3">
        رفع ملف جديد إلى «{categoryLabel(category)}»
      </h2>
      <div className="grid grid-cols-1 md:grid-cols-[2fr_3fr_auto] gap-3 items-end">
        <label className="block">
          <span className="block text-sm text-sage-700 mb-1">العنوان</span>
          <input
            value={title}
            onChange={e => setTitle(e.target.value)}
            placeholder="مثال: لائحة المشتريات"
            className="w-full px-3 py-2 border border-sage-200 rounded focus:border-sage-500 focus:ring-1 focus:ring-sage-500 outline-none"
            disabled={uploading}
          />
        </label>
        <label className="block">
          <span className="block text-sm text-sage-700 mb-1">الملف (PDF / DOCX / DOC)</span>
          <div className="px-3 py-2 border border-sage-200 rounded bg-stone-50 min-h-[42px] flex items-center text-sm">
            {file ? (
              <span className="text-sage-800 truncate">
                {file.name} <span className="text-sage-500">({(file.size / 1024).toFixed(1)} KB)</span>
              </span>
            ) : (
              <span className="text-sage-500">اسحب الملف هنا أو استخدم الزر…</span>
            )}
          </div>
          <input
            type="file"
            accept=".pdf,.docx,.doc,.txt,.md,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document,application/msword"
            onChange={e => setFile(e.target.files?.[0] || null)}
            className="hidden"
            disabled={uploading}
          />
          <button
            type="button"
            onClick={() => (document.querySelector('input[type=file]') as HTMLInputElement | null)?.click()}
            className="mt-1 text-xs text-sage-600 hover:text-sage-800 underline"
            disabled={uploading}
          >
            اختر ملفًا
          </button>
        </label>
        <button
          type="submit"
          disabled={uploading}
          className="px-5 py-2 bg-sage-600 text-white rounded font-medium hover:bg-sage-700 disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {uploading ? '…جاري الرفع' : 'رفع'}
        </button>
      </div>
      {(err || progress) && (
        <div className={`mt-3 text-sm ${err ? 'text-rose-700' : 'text-sage-700'}`}>
          {err || progress}
        </div>
      )}
    </form>
  )
}

function DocumentRow({ doc, onChanged }: { doc: DocumentOut; onChanged: () => void }) {
  const [busy, setBusy] = useState(false)
  const [showVersions, setShowVersions] = useState(false)
  const [files, setFiles] = useState<any[] | null>(null)
  const sl = statusLabel(doc.latest_status)

  async function withBusy(fn: () => Promise<any>) {
    setBusy(true)
    try { await fn(); onChanged() }
    catch (e: any) { alert(e.message) }
    finally { setBusy(false) }
  }

  const pct = (() => {
    const p = doc.latest_progress
    if (!p || !p.total_chunks || !p.processed_chunks) return 0
    return Math.min(100, Math.round((p.processed_chunks / p.total_chunks) * 100))
  })()

  return (
    <li className={`bg-white border rounded-lg p-4 ${doc.is_active ? 'border-sage-200' : 'border-stone-200 opacity-70'}`}>
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 flex-wrap">
            <h3 className="font-semibold text-sage-900 truncate">{doc.title}</h3>
            <span className={`text-xs px-2 py-0.5 rounded border ${sl.cls}`}>{sl.ar}</span>
            {!doc.is_active && (
              <span className="text-xs px-2 py-0.5 rounded border bg-stone-100 text-stone-600 border-stone-200">
                غير نشط
              </span>
            )}
            <span className="text-xs text-sage-500">الإصدار {doc.current_version}</span>
          </div>
          <div className="text-xs text-sage-500 mt-0.5 truncate">
            {doc.uploaded_by ? `بواسطة ${doc.uploaded_by}` : '—'} ·{' '}
            {doc.latest_chunk_count} قطعة ·{' '}
            {doc.latest_embedding_count} تضمين
            {doc.latest_progress?.total_chunks ? ` / ${doc.latest_progress.total_chunks} متوقعة` : ''}
          </div>
          {doc.latest_progress?.total_chunks ? (
            <div className="mt-2 h-1.5 bg-sage-100 rounded overflow-hidden">
              <div
                className={`h-full ${pct >= 100 ? 'bg-sage-500' : 'bg-amber-400'} transition-all`}
                style={{ width: `${pct}%` }}
              />
            </div>
          ) : null}
          {doc.latest_error && (
            <div className="mt-2 text-xs text-rose-700 line-clamp-2">
              {doc.latest_error}
            </div>
          )}
        </div>
        <div className="flex flex-col gap-1 items-end shrink-0">
          <div className="flex gap-1">
            <button
              disabled={busy}
              onClick={() => withBusy(() => api.reprocess(doc.id))}
              className="text-xs px-2 py-1 bg-stone-50 border border-stone-200 rounded hover:bg-stone-100 disabled:opacity-50"
              title="إعادة المعالجة"
            >
              إعادة
            </button>
            {doc.is_active ? (
              <button
                disabled={busy}
                onClick={() => withBusy(() => api.deactivate(doc.id))}
                className="text-xs px-2 py-1 bg-amber-50 border border-amber-200 text-amber-800 rounded hover:bg-amber-100 disabled:opacity-50"
                title="إيقاف من نتائج البحث"
              >
                إيقاف
              </button>
            ) : (
              <button
                disabled={busy}
                onClick={() => withBusy(() => api.activate(doc.id))}
                className="text-xs px-2 py-1 bg-sage-50 border border-sage-200 text-sage-800 rounded hover:bg-sage-100 disabled:opacity-50"
                title="إعادة التفعيل"
              >
                تفعيل
              </button>
            )}
            <button
              disabled={busy}
              onClick={() => {
                if (confirm(`حذف "${doc.title}" نهائيًا؟ لا يمكن التراجع.`)) {
                  withBusy(() => api.hardDelete(doc.id))
                }
              }}
              className="text-xs px-2 py-1 bg-rose-50 border border-rose-200 text-rose-800 rounded hover:bg-rose-100 disabled:opacity-50"
              title="حذف نهائي"
            >
              حذف
            </button>
          </div>
          <button
            onClick={async () => {
              if (!showVersions && !files) {
                setFiles(await api.listFiles(doc.id))
              }
              setShowVersions(s => !s)
            }}
            className="text-xs text-sage-600 hover:text-sage-800 underline"
          >
            {showVersions ? 'إخفاء الإصدارات' : 'عرض الإصدارات'}
          </button>
        </div>
      </div>
      {showVersions && files && (
        <div className="mt-3 border-t border-sage-100 pt-3">
          {files.length === 0 ? (
            <div className="text-xs text-sage-500">لا توجد إصدارات مسجلة.</div>
          ) : (
            <ul className="text-xs space-y-1">
              {files.map(f => {
                const s = statusLabel(f.status)
                return (
                  <li key={f.id} className="flex justify-between items-center">
                    <span className="truncate">
                      v{f.version} · {f.original_filename}
                      {f.size_bytes ? ` · ${(f.size_bytes / 1024).toFixed(1)} KB` : ''}
                    </span>
                    <span className={`px-1.5 py-0.5 rounded border ${s.cls}`}>{s.ar}</span>
                  </li>
                )
              })}
            </ul>
          )}
        </div>
      )}
    </li>
  )
}
