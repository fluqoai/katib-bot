import { useMemo, useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import {
  Check, Download, FileText, ChevronDown, ChevronUp, AlertTriangle,
  Library, RotateCcw, Sparkles, LayoutTemplate, ScrollText,
  BookOpenCheck, Calendar, UserCircle2, PenLine,
} from 'lucide-react'
import {
  downloadDocx,
  type GenerateResult, type ChunkUsed, type LetterStyle, type LetterStyleOverrides,
} from '../lib/api'

export default function Result({
  result,
  request,
  fields,
  onStartOver,
}: {
  result: GenerateResult
  request: string
  fields: Record<string, string>
  onStartOver: () => void
}) {
  const [downloading, setDownloading] = useState<null | 'docx'>(null)
  const [downloadError, setDownloadError] = useState<string | null>(null)
  const [showReferences, setShowReferences] = useState(false)
  const [showStyle, setShowStyle] = useState(false)

  const finalDraft = result.final_draft || result.corrected_draft || result.draft
  const cleanBody = useMemo(() => {
    if (!finalDraft) return ''
    return stripCitationTags(finalDraft.body_only || finalDraft.body)
  }, [finalDraft])

  const needsReview = result.needs_review
  const okStatus = result.final_status === 'ok' || result.final_status === 'fixable'
  const verdictLabel = statusLabel(result.final_status)
  const verdictColor = verdictColorClass(result.final_status)

  async function handleDownload() {
    setDownloading('docx')
    setDownloadError(null)
    try {
      let blob: Blob
      if (result.docx_base64) {
        const binary = window.atob(result.docx_base64)
        const bytes = new Uint8Array(binary.length)
        for (let index = 0; index < binary.length; index += 1) {
          bytes[index] = binary.charCodeAt(index)
        }
        blob = new Blob([bytes], {
          type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        })
      } else if (result.docx_url) {
        const response = await fetch(result.docx_url)
        if (!response.ok) throw new Error(`HTTP ${response.status}`)
        blob = await response.blob()
      } else {
        // Compatibility fallback when generated-file storage is unavailable.
        blob = await downloadDocx({ request, fields })
      }
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      const fname = (finalDraft?.body_only || request).slice(0, 40).replace(/[^\w\u0600-\u06FF]+/g, '_') + '.docx'
      a.download = fname
      a.click()
      URL.revokeObjectURL(url)
    } catch (e: any) {
      setDownloadError(e?.message || 'فشل التحميل')
    } finally {
      setDownloading(null)
    }
  }

  return (
    <div className="space-y-5">
      {/* Status banner */}
      <motion.div
        initial={{ opacity: 0, y: 8 }}
        animate={{ opacity: 1, y: 0 }}
        className={`rounded-3xl p-5 ${verdictColor.bg} ${verdictColor.border} border-2`}
      >
        <div className="flex items-start gap-3">
          <div className={`w-11 h-11 rounded-2xl flex items-center justify-center flex-shrink-0 ${verdictColor.iconBg} text-white`}>
            {needsReview
              ? <AlertTriangle className="w-5 h-5" />
              : <Check className="w-5 h-5" strokeWidth={2.6} />}
          </div>
          <div className="flex-1 min-w-0">
            <div className={`font-extrabold text-lg ${verdictColor.title}`}>
              {verdictLabel}
            </div>
            <p className={`text-sm leading-relaxed mt-1 ${verdictColor.body}`}>
              {needsReview
                ? 'هذا الخطاب يحتاج مراجعة من شخص مختص قبل الإرسال، حتى نتأكد من دقته.'
                : okStatus
                ? 'الخطاب جاهز. يمكنك تحميله بصيغة Word.'
                : 'لم نتمكن من إكمال الخطاب. حاول مرة أخرى بصياغة مختلفة.'}
            </p>
            {/* Phase 4: format badge (style-driven vs legacy template) */}
            <div className="mt-3">
              {result.template_profile ? (
                <span className="inline-flex items-center gap-1.5 rounded-full bg-amber-100 text-amber-800 px-3 py-1 text-xs font-semibold">
                  <ScrollText className="w-3.5 h-3.5" />
                  القالب الرسمي المعتمد للجمعية
                </span>
              ) : (
                <span className="inline-flex items-center gap-1.5 rounded-full bg-sage-100 text-sage-800 px-3 py-1 text-xs font-semibold">
                  <LayoutTemplate className="w-3.5 h-3.5" />
                  تنسيق رسمي قياسي
                </span>
              )}
            </div>
          </div>
        </div>
      </motion.div>

      {/* Letter preview */}
      <div className="card">
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-2">
            <FileText className="w-5 h-5 text-sage-700" />
            <h2 className="font-extrabold text-ink-900">معاينة الخطاب</h2>
          </div>
          {finalDraft && (
            <span className="text-xs text-ink-300">
              {cleanBody.split('\n').filter((l) => l.trim()).length} سطر
            </span>
          )}
        </div>
        {cleanBody ? (
          <LetterPreview body={cleanBody} />
        ) : (
          <p className="text-ink-300 text-sm">— لا يوجد محتوى —</p>
        )}
      </div>

      {/* Phase 4: formatting details (LetterStyle) — only when style-driven */}
      {result.letter_style && (
        <div className="card-quiet">
          <button
            onClick={() => setShowStyle((v) => !v)}
            className="w-full flex items-center justify-between text-right"
          >
            <div className="flex items-center gap-2">
              <LayoutTemplate className="w-4 h-4 text-sage-700" />
              <span className="font-semibold text-sage-800 text-sm">تفاصيل التنسيق المطبَّق</span>
            </div>
            {showStyle
              ? <ChevronUp className="w-4 h-4 text-ink-500" />
              : <ChevronDown className="w-4 h-4 text-ink-500" />}
          </button>
          <AnimatePresence>
            {showStyle && (
              <motion.div
                initial={{ height: 0, opacity: 0 }}
                animate={{ height: 'auto', opacity: 1 }}
                exit={{ height: 0, opacity: 0 }}
                transition={{ duration: 0.2 }}
                className="overflow-hidden"
              >
                <StyleDetails style={result.letter_style} />
              </motion.div>
            )}
          </AnimatePresence>
        </div>
      )}

      {/* Download button — Word only (MVP) */}
      <div>
        <button
          onClick={handleDownload}
          disabled={downloading !== null}
          className="btn-primary w-full justify-center"
        >
          {downloading === 'docx' ? (
            <span className="inline-block w-4 h-4 border-2 border-white border-t-transparent rounded-full animate-spin" />
          ) : (
            <Download className="w-5 h-5" />
          )}
          تحميل Word
        </button>
      </div>

      {downloadError && (
        <div className="rounded-2xl bg-rose-50 border border-rose-200 px-4 py-3 text-sm text-rose-700">
          {downloadError}
        </div>
      )}

      {/* References / sources used (collapsible) */}
      <div className="card-quiet">
        <button
          onClick={() => setShowReferences((v) => !v)}
          className="w-full flex items-center justify-between text-right"
        >
          <div className="flex items-center gap-2">
            <Library className="w-4 h-4 text-sage-700" />
            <span className="font-semibold text-sage-800 text-sm">المراجع واللوائح المستخدمة</span>
            {result.chunks_used.length > 0 && (
              <span className="pill pill-sage text-[11px]">{result.chunks_used.length}</span>
            )}
          </div>
          {showReferences
            ? <ChevronUp className="w-4 h-4 text-ink-500" />
            : <ChevronDown className="w-4 h-4 text-ink-500" />}
        </button>
        <AnimatePresence>
          {showReferences && (
            <motion.div
              initial={{ height: 0, opacity: 0 }}
              animate={{ height: 'auto', opacity: 1 }}
              exit={{ height: 0, opacity: 0 }}
              transition={{ duration: 0.2 }}
              className="overflow-hidden"
            >
              <ReferencesList chunks={result.chunks_used} />
            </motion.div>
          )}
        </AnimatePresence>
      </div>

      {/* Start over */}
      <button onClick={onStartOver} className="btn-ghost w-full justify-center text-sage-700 mt-2">
        <RotateCcw className="w-4 h-4" />
        كتابة خطاب جديد
      </button>
    </div>
  )
}

function LetterPreview({ body }: { body: string }) {
  // Lightly format: bold lines starting with **...**, list items with - or •,
  // headings with ##, and quoted blocks with >.
  const lines = body.split('\n')
  return (
    <article className="font-arabic text-[15.5px] leading-loose text-ink-900 whitespace-pre-wrap break-words">
      {lines.map((line, i) => {
        const t = line.trim()
        if (!t) return <div key={i} className="h-2" />
        if (t.startsWith('## ')) {
          return <h3 key={i} className="text-base font-extrabold text-sage-800 mt-4 mb-1">{t.slice(3)}</h3>
        }
        if (t.startsWith('# ')) {
          return <h2 key={i} className="text-lg font-extrabold text-sage-900 mt-4 mb-1">{t.slice(2)}</h2>
        }
        if (t.startsWith('> ')) {
          return (
            <blockquote key={i} className="border-r-4 border-wood-400 bg-wood-50/60 px-3 py-2 my-2 text-ink-700 rounded-md">
              {t.slice(2)}
            </blockquote>
          )
        }
        if (t.startsWith('- ') || t.startsWith('• ')) {
          return <div key={i} className="flex gap-2"><span className="text-sage-500">•</span><span>{t.replace(/^[-•]\s*/, '')}</span></div>
        }
        // detect **...** inline bold
        const parts = renderInlineBold(t)
        return <p key={i} className="my-1">{parts}</p>
      })}
    </article>
  )
}

function renderInlineBold(text: string): React.ReactNode[] {
  const out: React.ReactNode[] = []
  const re = /\*\*(.+?)\*\*/g
  let last = 0
  let m: RegExpExecArray | null
  let key = 0
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index))
    out.push(<strong key={key++} className="font-extrabold text-ink-900">{m[1]}</strong>)
    last = m.index + m[0].length
  }
  if (last < text.length) out.push(text.slice(last))
  return out
}

function ReferencesList({ chunks }: { chunks: ChunkUsed[] }) {
  if (chunks.length === 0) {
    return <p className="text-sm text-ink-300 mt-3">لا توجد مراجع.</p>
  }
  // Group by document_id
  const byDoc = new Map<string, { title: string; category: string; count: number }>()
  for (const c of chunks) {
    const k = c.document_id
    const cur = byDoc.get(k)
    if (cur) cur.count += 1
    else byDoc.set(k, { title: c.title, category: c.category, count: 1 })
  }
  const groups = Array.from(byDoc.entries())
  return (
    <div className="mt-3 space-y-2">
      {groups.map(([id, info]) => (
        <div key={id} className="flex items-center gap-3 bg-white rounded-xl px-3 py-2.5 border border-sage-100">
          <div className={`w-9 h-9 rounded-lg flex items-center justify-center flex-shrink-0
            ${info.category === 'templates' ? 'bg-wood-100 text-wood-700' :
              info.category === 'internal_policies' ? 'bg-sage-100 text-sage-700' :
              'bg-slate-100 text-slate-700'}`}>
            <FileText className="w-4 h-4" />
          </div>
          <div className="flex-1 min-w-0">
            <div className="font-semibold text-sm text-ink-900 truncate">{info.title}</div>
            <div className="text-xs text-ink-500">
              {info.category === 'templates' ? 'قالب' :
               info.category === 'internal_policies' ? 'سياسة داخلية' :
               info.category === 'national_regulations' ? 'لائحة وطنية' :
               info.category}
              {' · '}
              {info.count} {info.count === 1 ? 'فقرة' : 'فقرات'}
            </div>
          </div>
        </div>
      ))}
    </div>
  )
}

function stripCitationTags(s: string): string {
  return s.replace(/\s*\[source:\s*[^\]]+\]/g, '').trim()
}

function StyleDetails({ style }: { style: LetterStyle }) {
  // Render the resolved LetterStyle as a compact grid of "what was applied".
  const items: { icon: React.ElementType; label: string; value: string }[] = [
    { icon: BookOpenCheck, label: 'بسملة',          value: style.include_basmala ? 'مُفعَّلة' : 'غير مُفعَّلة' },
    { icon: Calendar,      label: 'صف التاريخ',    value: style.include_date_row ? 'مُفعَّل' : 'غير مُفعَّل' },
    { icon: UserCircle2,   label: 'سطر المُراسَل', value: style.include_recipient_block ? 'مُفعَّل' : 'غير مُفعَّل' },
    { icon: PenLine,       label: 'التوقيع',       value: style.include_signature_block ? 'مُفعَّل' : 'غير مُفعَّل' },
  ]
  return (
    <div className="mt-3 grid sm:grid-cols-2 gap-2">
      {items.map((it, i) => {
        const Icon = it.icon
        return (
          <div key={i} className="flex items-center gap-2.5 bg-white rounded-xl px-3 py-2.5 border border-sage-100">
            <div className={`w-8 h-8 rounded-lg flex items-center justify-center flex-shrink-0
              ${it.value.includes('غير') ? 'bg-ink-100 text-ink-400' : 'bg-sage-100 text-sage-700'}`}>
              <Icon className="w-4 h-4" />
            </div>
            <div className="flex-1 min-w-0">
              <div className="text-[11px] text-ink-500 leading-none">{it.label}</div>
              <div className={`text-sm font-semibold mt-0.5 ${it.value.includes('غير') ? 'text-ink-400' : 'text-ink-900'}`}>
                {it.value}
              </div>
            </div>
          </div>
        )
      })}
      <div className="sm:col-span-2 flex items-center gap-2.5 bg-white rounded-xl px-3 py-2.5 border border-sage-100">
        <div className="w-8 h-8 rounded-lg bg-sage-100 text-sage-700 flex items-center justify-center flex-shrink-0">
          <LayoutTemplate className="w-4 h-4" />
        </div>
        <div className="flex-1 min-w-0 grid grid-cols-2 sm:grid-cols-3 gap-x-3 gap-y-1">
          <div>
            <div className="text-[10px] text-ink-400 leading-none">الصفحة</div>
            <div className="text-xs font-semibold text-ink-700 mt-0.5">{style.page_size} · {style.margin_top_cm}/{style.margin_bottom_cm}/{style.margin_left_cm}/{style.margin_right_cm} سم</div>
          </div>
          <div>
            <div className="text-[10px] text-ink-400 leading-none">الخط</div>
            <div className="text-xs font-semibold text-ink-700 mt-0.5">{style.font_name} · {style.font_size_pt}pt</div>
          </div>
          <div>
            <div className="text-[10px] text-ink-400 leading-none">الاتجاه</div>
            <div className="text-xs font-semibold text-ink-700 mt-0.5">{style.rtl ? 'RTL' : 'LTR'} · {style.body_alignment}</div>
          </div>
        </div>
      </div>
    </div>
  )
}

function statusLabel(s: string): string {
  switch (s) {
    case 'ok':           return 'تم إنشاء الخطاب بنجاح'
    case 'fixable':      return 'تم إنشاء الخطاب بنجاح'
    case 'needs_review': return 'الخطاب جاهز لكن يحتاج مراجعة'
    case 'unverifiable': return 'تعذّر إنشاء الخطاب'
    case 'no_template':  return 'لا يوجد قالب مناسب'
    case 'failed':       return 'حدث خطأ'
    default:             return s
  }
}

function verdictColorClass(s: string) {
  if (s === 'ok' || s === 'fixable') {
    return {
      bg: 'bg-sage-50', border: 'border-sage-200', title: 'text-sage-900', body: 'text-sage-700',
      iconBg: 'bg-sage-600',
    }
  }
  // needs_review / unverifiable / failed / no_template
  return {
    bg: 'bg-amber-50', border: 'border-amber-200', title: 'text-amber-900', body: 'text-amber-800',
    iconBg: 'bg-amber-500',
  }
}
