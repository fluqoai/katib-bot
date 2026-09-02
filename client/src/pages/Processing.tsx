import { useEffect, useRef, useState } from 'react'
import { motion } from 'framer-motion'
import { Check, Loader2, ScanSearch, Scale, PenLine, ShieldCheck, FileDown, AlertTriangle, X } from 'lucide-react'
import { generateLetter, type GenerateResult, type LetterStyleOverrides } from '../lib/api'

type Step = {
  id: string
  label: string
  icon: React.ElementType
  // approximate duration this step is expected to take (ms)
  estimate: number
}

// The 5 human-friendly steps. These do NOT map 1:1 to backend stages
// (the backend runs 10 stages now with the style_resolution stage)
// — they are coarse-grained UI milestones.
const STEPS: Step[] = [
  { id: 'intent',     label: 'تحليل نوع الخطاب',         icon: ScanSearch,   estimate: 4000  },
  { id: 'policies',   label: 'البحث في السياسات واللوائح', icon: Scale,      estimate: 4000  },
  { id: 'draft',      label: 'صياغة الخطاب',              icon: PenLine,      estimate: 30000 },
  { id: 'review',     label: 'مراجعة الخطاب',             icon: ShieldCheck,  estimate: 30000 },
  { id: 'export',     label: 'تجهيز الملف',               icon: FileDown,     estimate: 5000  },
]

export default function Processing({
  request,
  fields,
  useLegacyTemplate,
  styleOverrides,
  onDone,
  onCancel,
}: {
  request: string
  fields: Record<string, string>
  useLegacyTemplate?: boolean
  styleOverrides?: LetterStyleOverrides
  onDone: (r: GenerateResult) => void
  onCancel: () => void
}) {
  const [activeStep, setActiveStep] = useState(0) // 0..STEPS.length
  const [error, setError] = useState<string | null>(null)
  const startedRef = useRef<number>(Date.now())

  useEffect(() => {
    let cancelled = false
    let timerIds: number[] = []

    // Walk the visible steps at a pace that matches typical backend latency.
    // The backend is one big LLM call, so we just animate the steps forward
    // at the estimate intervals. The final step stays in 'in_progress' until
    // the request resolves.
    let acc = 0
    STEPS.forEach((s, i) => {
      acc += s.estimate
      const t = window.setTimeout(() => {
        if (cancelled) return
        setActiveStep(i + 1) // mark previous as done; this step is in progress
      }, acc)
      timerIds.push(t)
    })

    // Fire the actual generation (Phase 4: pass through the new options)
    generateLetter({
      request,
      fields,
      // Persist the exact DOCX created during this generation. The Result
      // screen downloads this file instead of running the AI pipeline again.
      upload_outputs: true,
      use_legacy_template: useLegacyTemplate,
      style_overrides: styleOverrides,
    })
      .then((r: GenerateResult) => {
        if (cancelled) return
        timerIds.forEach((t) => clearTimeout(t))
        setActiveStep(STEPS.length)
        // brief pause to let the user see the final "done" state
        window.setTimeout(() => onDone(r), 350)
      })
      .catch((e: unknown) => {
        if (cancelled) return
        timerIds.forEach((t) => clearTimeout(t))
        const message = e instanceof Error ? e.message : String(e)
        setError(message)
      })

    return () => {
      cancelled = true
      timerIds.forEach((t) => clearTimeout(t))
    }
  }, [request, fields, useLegacyTemplate, styleOverrides, onDone])

  const elapsed = Math.floor((Date.now() - startedRef.current) / 1000)

  if (error) {
    return (
      <div className="card space-y-4">
        <div className="flex items-start gap-3">
          <div className="w-10 h-10 rounded-full bg-rose-100 text-rose-700 flex items-center justify-center flex-shrink-0">
            <AlertTriangle className="w-5 h-5" />
          </div>
          <div className="flex-1">
            <h2 className="font-bold text-ink-900 text-lg">تعذّر إكمال الطلب</h2>
            <p className="text-sm text-ink-500 mt-1 leading-relaxed">
              {error}
            </p>
            <p className="text-xs text-ink-300 mt-3">
              إذا استمرّ هذا الخطأ، يُرجى التواصل مع المسؤول.
            </p>
          </div>
        </div>
        <div className="flex gap-2">
          <button className="btn-primary flex-1" onClick={onCancel}>
            <X className="w-4 h-4" />
            العودة
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="space-y-6">
      {/* Summary card */}
      <div className="card-quiet">
        <div className="text-xs text-ink-500 mb-1">جاري إنشاء خطاب لـ</div>
        <div className="text-ink-900 font-semibold leading-relaxed text-sm sm:text-base line-clamp-3">
          {request}
        </div>
      </div>

      {/* Steps */}
      <div className="card space-y-1">
        {STEPS.map((s, i) => {
          const Icon = s.icon
          const isDone = i < activeStep
          const isActive = i === activeStep
          return (
            <motion.div
              key={s.id}
              initial={{ opacity: 0, x: 12 }}
              animate={{ opacity: 1, x: 0 }}
              transition={{ duration: 0.25, delay: i * 0.05 }}
              className={`flex items-center gap-3.5 py-3 px-1 ${i < STEPS.length - 1 ? 'border-b border-sage-50' : ''}`}
            >
              <div
                className={`w-10 h-10 rounded-2xl flex items-center justify-center flex-shrink-0 transition-colors
                  ${isDone
                    ? 'bg-sage-600 text-white'
                    : isActive
                    ? 'bg-sage-100 text-sage-700'
                    : 'bg-linen text-ink-300'}`}
              >
                {isDone ? (
                  <Check className="w-5 h-5" strokeWidth={2.6} />
                ) : isActive ? (
                  <Loader2 className="w-5 h-5 animate-spin" />
                ) : (
                  <Icon className="w-5 h-5" />
                )}
              </div>
              <div className="flex-1 min-w-0">
                <div
                  className={`font-semibold text-sm sm:text-base
                    ${isDone ? 'text-sage-800' : isActive ? 'text-ink-900' : 'text-ink-300'}`}
                >
                  {s.label}
                </div>
                {isActive && (
                  <motion.div
                    initial={{ width: 0 }}
                    animate={{ width: '100%' }}
                    transition={{ duration: s.estimate / 1000, ease: 'linear' }}
                    className="h-1 bg-sage-200 rounded-full mt-1.5 overflow-hidden"
                  >
                    <div className="h-full bg-sage-600 rounded-full" style={{ width: '100%' }} />
                  </motion.div>
                )}
                {isDone && (
                  <div className="text-xs text-sage-600 mt-0.5">تم</div>
                )}
              </div>
            </motion.div>
          )
        })}

        <div className="pt-3 mt-1 border-t border-sage-50 flex items-center justify-between text-xs text-ink-500">
          <span>الوقت المنقضي: {elapsed} ثانية</span>
          <span>الخطوات: {activeStep} من {STEPS.length}</span>
        </div>
      </div>

      <button onClick={onCancel} className="btn-ghost w-full text-ink-500 hover:bg-rose-50 hover:text-rose-700">
        إلغاء والعودة
      </button>
    </div>
  )
}
