import { useEffect, useRef, useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import {
  ChevronRight, Upload, FileText, Trash2, Power, RefreshCw, Loader2,
  Check, AlertCircle, CloudUpload, X, MoreVertical, Clock,
} from 'lucide-react'
import {
  CATEGORY_AR, CATEGORY_DESC,
  listDocuments, uploadDocument, reprocessDocument,
  deactivateDocument, activateDocument, deleteDocument,
  type DocCategory, type DocumentRow,
} from '../../lib/api'

export default function CategoryPage({
  category,
  onBack,
}: {
  category: DocCategory
  onBack: () => void
}) {
  const [docs, setDocs] = useState<DocumentRow[]>([])
  const [loading, setLoading] = useState(true)
  const [uploading, setUploading] = useState<{ name: string; progress: number } | null>(null)
  const [uploadError, setUploadError] = useState<string | null>(null)
  const [dragOver, setDragOver] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)
  const pollRef = useRef<number | null>(null)

  async function refresh() {
    setLoading(true)
    try {
      const list = await listDocuments(category)
      setDocs(list)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { refresh() }, [category])

  // Auto-poll while any doc is processing
  useEffect(() => {
    const anyProcessing = docs.some((d) => isProcessing(d))
    if (anyProcessing && !pollRef.current) {
      pollRef.current = window.setInterval(refresh, 3000)
    } else if (!anyProcessing && pollRef.current) {
      clearInterval(pollRef.current)
      pollRef.current = null
    }
    return () => {
      if (pollRef.current) clearInterval(pollRef.current)
      pollRef.current = null
    }
  }, [docs])

  async function handleFile(file: File) {
    setUploading({ name: file.name, progress: 0 })
    setUploadError(null)
    try {
      // Simulate progress (the actual upload is one HTTP request)
      const tick = window.setInterval(() => {
        setUploading((u) => (u ? { ...u, progress: Math.min(u.progress + 10, 90) } : u))
      }, 200)
      await uploadDocument(file, category)
      clearInterval(tick)
      setUploading({ name: file.name, progress: 100 })
      window.setTimeout(() => setUploading(null), 600)
      await refresh()
    } catch (e: any) {
      setUploadError(e?.message || 'فشل الرفع')
      setUploading(null)
    }
  }

  function onDrop(e: React.DragEvent) {
    e.preventDefault()
    setDragOver(false)
    const f = e.dataTransfer.files?.[0]
    if (f) handleFile(f)
  }
  function onPick(e: React.ChangeEvent<HTMLInputElement>) {
    const f = e.target.files?.[0]
    if (f) handleFile(f)
    e.target.value = ''
  }

  return (
    <div className="space-y-5">
      {/* Header */}
      <div className="flex items-center justify-between">
        <button onClick={onBack} className="btn-ghost text-ink-500">
          <ChevronRight className="w-4 h-4" />
          الإدارة
        </button>
        <h1 className="text-xl font-extrabold text-ink-900">{CATEGORY_AR[category]}</h1>
        <div className="w-20" />
      </div>

      <p className="text-sm text-ink-500 leading-relaxed">{CATEGORY_DESC[category]}</p>

      {/* Upload card */}
      <div
        onDragOver={(e) => { e.preventDefault(); setDragOver(true) }}
        onDragLeave={() => setDragOver(false)}
        onDrop={onDrop}
        className={`card border-2 border-dashed transition
          ${dragOver ? 'border-sage-500 bg-sage-50' : 'border-sage-200 bg-white'}`}
      >
        <div className="flex flex-col sm:flex-row items-center gap-4">
          <div className="w-14 h-14 rounded-2xl bg-sage-100 text-sage-700 flex items-center justify-center flex-shrink-0">
            <CloudUpload className="w-7 h-7" />
          </div>
          <div className="flex-1 text-center sm:text-right">
            <div className="font-extrabold text-ink-900">رفع ملف جديد</div>
            <div className="text-xs text-ink-500 mt-1">
              يرجى رفع الملف بصيغة Word (.docx)
            </div>
          </div>
          <button
            onClick={() => inputRef.current?.click()}
            disabled={uploading !== null}
            className="btn-primary"
          >
            {uploading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Upload className="w-4 h-4" />}
            اختر ملفاً
          </button>
          <input
            ref={inputRef}
            type="file"
            accept=".docx"
            className="hidden"
            onChange={onPick}
          />
        </div>

        <AnimatePresence>
          {uploading && (
            <motion.div
              initial={{ opacity: 0, height: 0 }}
              animate={{ opacity: 1, height: 'auto' }}
              exit={{ opacity: 0, height: 0 }}
              className="mt-4 border-t border-sage-100 pt-4"
            >
              <div className="flex items-center gap-2 mb-2">
                {uploading.progress >= 100
                  ? <Check className="w-4 h-4 text-sage-600" />
                  : <Loader2 className="w-4 h-4 text-sage-600 animate-spin" />}
                <div className="text-sm font-semibold text-ink-900 truncate flex-1">
                  {uploading.name}
                </div>
                <div className="text-xs text-ink-500 tabular-arabic">{uploading.progress}%</div>
              </div>
              <div className="h-1.5 bg-sage-100 rounded-full overflow-hidden">
                <div className="h-full bg-sage-600 transition-all" style={{ width: `${uploading.progress}%` }} />
              </div>
            </motion.div>
          )}
        </AnimatePresence>

        {uploadError && (
          <div className="mt-3 flex items-center gap-2 text-sm text-rose-700 bg-rose-50 border border-rose-200 rounded-xl px-3 py-2">
            <AlertCircle className="w-4 h-4 flex-shrink-0" />
            <span className="flex-1">{uploadError}</span>
            <button onClick={() => setUploadError(null)} className="text-ink-300 hover:text-ink-500">
              <X className="w-4 h-4" />
            </button>
          </div>
        )}
      </div>

      {/* Files list */}
      <div>
        <div className="flex items-center justify-between mb-3 px-1">
          <h2 className="font-extrabold text-ink-900">الملفات الموجودة</h2>
          <span className="text-xs text-ink-500">{docs.length} ملف</span>
        </div>

        {loading ? (
          <div className="card flex items-center justify-center py-8">
            <Loader2 className="w-5 h-5 animate-spin text-sage-600" />
          </div>
        ) : docs.length === 0 ? (
          <div className="card-quiet text-center text-sm text-ink-300 py-8">
            لا توجد ملفات بعد. ارفع ملفك الأول من الأعلى.
          </div>
        ) : (
          <div className="space-y-2">
            {docs.map((d) => (
              <DocRow
                key={d.id}
                d={d}
                onChanged={refresh}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

function isProcessing(d: DocumentRow): boolean {
  const s = (d.processing_status || '').toLowerCase()
  return s === 'pending' || s === 'processing' || s === 'queued'
}

function statusInfo(d: DocumentRow) {
  const s = (d.processing_status || '').toLowerCase()
  if (s === 'indexed' || s === 'ready') {
    return { label: 'جاهز', className: 'pill-sage', icon: <Check className="w-3 h-3" /> }
  }
  if (s === 'pending' || s === 'queued' || s === 'processing') {
    return { label: 'قيد المعالجة', className: 'pill-amber', icon: <Loader2 className="w-3 h-3 animate-spin" /> }
  }
  if (s === 'needs_ocr' || s === 'needs_doc_conversion') {
    return { label: 'يحتاج تحويل', className: 'pill-amber', icon: <Clock className="w-3 h-3" /> }
  }
  if (s === 'failed') {
    return { label: 'فشل', className: 'pill-rose', icon: <AlertCircle className="w-3 h-3" /> }
  }
  return { label: s || '—', className: 'pill-slate', icon: <Clock className="w-3 h-3" /> }
}

function DocRow({ d, onChanged }: { d: DocumentRow; onChanged: () => void | Promise<void> }) {
  const [busy, setBusy] = useState<null | 'reprocess' | 'toggle' | 'delete'>(null)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [menuOpen, setMenuOpen] = useState(false)

  const s = statusInfo(d)
  const showProgress = isProcessing(d)

  async function onReprocess() {
    setBusy('reprocess'); setMenuOpen(false)
    try { await reprocessDocument(d.id); await onChanged() } finally { setBusy(null) }
  }
  async function onToggle() {
    setBusy('toggle'); setMenuOpen(false)
    try {
      if (d.is_active) await deactivateDocument(d.id)
      else await activateDocument(d.id)
      await onChanged()
    } finally { setBusy(null) }
  }
  async function onDelete() {
    setBusy('delete')
    try { await deleteDocument(d.id); setConfirmDelete(false); await onChanged() } finally { setBusy(null) }
  }

  return (
    <motion.div
      layout
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      className="card !p-4"
    >
      <div className="flex items-start gap-3">
        <div className={`w-10 h-10 rounded-xl flex items-center justify-center flex-shrink-0
          ${d.is_active ? 'bg-sage-100 text-sage-700' : 'bg-linen text-ink-300'}`}>
          <FileText className="w-5 h-5" />
        </div>

        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <div className={`font-bold text-sm truncate ${d.is_active ? 'text-ink-900' : 'text-ink-500 line-through'}`}>
              {d.title}
            </div>
            <span className={`pill ${s.className}`}>
              {s.icon}
              {s.label}
            </span>
            {!d.is_active && <span className="pill pill-slate">غير مفعّل</span>}
          </div>

          <div className="text-xs text-ink-500 mt-1 flex flex-wrap items-center gap-x-3 gap-y-0.5">
            <span>الإصدار {d.current_version}</span>
            {d.chunk_count !== null && d.chunk_count !== undefined && (
              <span>· {d.chunk_count} فقرة</span>
            )}
            {d.char_count !== null && d.char_count !== undefined && (
              <span>· {Math.round(d.char_count / 100) / 10}k حرف</span>
            )}
            <span className="hidden sm:inline">· {new Date(d.created_at).toLocaleDateString('ar-EG')}</span>
          </div>

          {showProgress && (
            <div className="mt-2 h-1 bg-sage-100 rounded-full overflow-hidden">
              <div
                className="h-full bg-sage-500 transition-all"
                style={{ width: `${Math.max(5, Math.min(100, d.processing_progress || 5))}%` }}
              />
            </div>
          )}
          {d.processing_error && (
            <div className="mt-1.5 text-xs text-rose-600">{d.processing_error}</div>
          )}
        </div>

        {/* Menu */}
        <div className="relative">
          <button
            onClick={() => setMenuOpen((v) => !v)}
            className="w-9 h-9 rounded-lg hover:bg-sage-100 text-ink-500 flex items-center justify-center"
            aria-label="إجراءات"
          >
            <MoreVertical className="w-4 h-4" />
          </button>
          <AnimatePresence>
            {menuOpen && (
              <>
                <div className="fixed inset-0 z-10" onClick={() => setMenuOpen(false)} />
                <motion.div
                  initial={{ opacity: 0, y: -4 }}
                  animate={{ opacity: 1, y: 0 }}
                  exit={{ opacity: 0, y: -4 }}
                  className="absolute left-0 mt-1 w-44 bg-white rounded-xl shadow-soft-lg border border-sage-100 z-20 overflow-hidden"
                >
                  <button
                    onClick={onReprocess}
                    disabled={busy !== null}
                    className="w-full text-right px-3 py-2.5 text-sm hover:bg-sage-50 flex items-center gap-2"
                  >
                    <RefreshCw className="w-4 h-4 text-ink-500" />
                    إعادة المعالجة
                  </button>
                  <button
                    onClick={onToggle}
                    disabled={busy !== null}
                    className="w-full text-right px-3 py-2.5 text-sm hover:bg-sage-50 flex items-center gap-2"
                  >
                    <Power className="w-4 h-4 text-ink-500" />
                    {d.is_active ? 'إلغاء التفعيل' : 'تفعيل'}
                  </button>
                  <div className="border-t border-sage-100" />
                  <button
                    onClick={() => { setMenuOpen(false); setConfirmDelete(true) }}
                    disabled={busy !== null}
                    className="w-full text-right px-3 py-2.5 text-sm hover:bg-rose-50 text-rose-700 flex items-center gap-2"
                  >
                    <Trash2 className="w-4 h-4" />
                    حذف
                  </button>
                </motion.div>
              </>
            )}
          </AnimatePresence>
        </div>
      </div>

      <AnimatePresence>
        {confirmDelete && (
          <motion.div
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: 'auto' }}
            exit={{ opacity: 0, height: 0 }}
            className="mt-3 bg-rose-50 border border-rose-200 rounded-xl p-3 overflow-hidden"
          >
            <div className="text-sm text-rose-800 mb-2">
              هل أنت متأكد من حذف <strong>{d.title}</strong>؟ لا يمكن التراجع.
            </div>
            <div className="flex gap-2">
              <button
                onClick={onDelete}
                disabled={busy === 'delete'}
                className="flex-1 bg-rose-600 hover:bg-rose-700 text-white font-semibold rounded-lg py-2 text-sm"
              >
                {busy === 'delete' ? <Loader2 className="w-4 h-4 inline animate-spin" /> : 'نعم، احذف'}
              </button>
              <button
                onClick={() => setConfirmDelete(false)}
                className="flex-1 bg-white border border-rose-200 text-rose-700 font-semibold rounded-lg py-2 text-sm"
              >
                إلغاء
              </button>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  )
}
