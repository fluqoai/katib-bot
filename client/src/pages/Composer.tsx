import { useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import {
  Sparkles, ChevronDown, ChevronUp, ArrowLeft, Lightbulb,
} from 'lucide-react'
import type { LetterStyleOverrides } from '../lib/api'

const EXAMPLES = [
  'اكتب لي خطاب طلب شراكة مع وزارة الثقافة لتنظيم مهرجان أدبي',
  'اكتب لي خطاب شكر وتقدير لرئيس مجلس إدارة الجمعية على جهوده',
  'اكتب لي خطاب دعوة لحضور اجتماع الجمعية العمومية',
  'اكتب لي خطاب إشعار للوزارة بتشكّل مجلس إدارة جديد',
  'اكتب لي خطاب طلب ترخيص لإقامة فعالية خيرية',
]

const PRESET_FIELDS = [
  { name: 'recipient_name', label: 'اسم الشخص أو الجهة المُراسَلة' },
  { name: 'partner_org',    label: 'الجهة الشريكة (إن وُجدت)' },
  { name: 'project_name',   label: 'اسم المشروع / الموضوع' },
  { name: 'event_date',     label: 'تاريخ المناسبة' },
]

export type ComposerOptions = {
  use_legacy_template: boolean
  style_overrides: LetterStyleOverrides
}

export default function Composer({
  onSubmit,
}: {
  onSubmit: (request: string, fields: Record<string, string>, options: ComposerOptions) => void
}) {
  const [request, setRequest] = useState('')
  const [fields, setFields] = useState<Record<string, string>>({ entity_type: 'organization' })
  const [showFields, setShowFields] = useState(false)
  const [touched, setTouched] = useState(false)

  const valid = request.trim().length >= 5

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    if (!valid) {
      setTouched(true)
      return
    }
    onSubmit(request.trim(), fields, {
      use_legacy_template: false,
      style_overrides: {},
    })
  }

  function fillExample(ex: string) {
    setRequest(ex)
    setTouched(false)
  }

  return (
    <div className="space-y-8">
      {/* Hero */}
      <div className="text-center sm:text-right space-y-3">
        <div className="inline-flex items-center gap-2 rounded-full bg-sage-100 text-sage-800 px-4 py-1.5 text-xs font-semibold">
          <Sparkles className="w-3.5 h-3.5" />
          مساعد كتابة بالذكاء الاصطناعي
        </div>
        <h1 className="text-3xl sm:text-4xl font-extrabold text-ink-900 leading-tight">
          اكتب الخطاب الذي تحتاجه
        </h1>
        <p className="text-ink-500 text-base max-w-md sm:max-w-none mx-auto sm:mx-0">
          صف الخطاب الذي تريده بكلماتك، وسيقوم كاتب بصياغته بصيغة رسمية اعتماداً على لوائح الجمعية وسياساتها.
        </p>
      </div>

      {/* Composer card */}
      <form onSubmit={handleSubmit} className="card space-y-5">
        <div>
          <label htmlFor="request" className="block text-sm font-bold text-sage-800 mb-2">
            طلبك
          </label>
          <textarea
            id="request"
            value={request}
            onChange={(e) => setRequest(e.target.value)}
            rows={6}
            className="input-base resize-none leading-relaxed text-base"
            placeholder="مثال: اكتب لي خطاب طلب شراكة مع وزارة الثقافة لتنظيم مهرجان القراءة للجميع في محافظة المظيلف"
            autoFocus
          />
          {touched && !valid && (
            <p className="text-rose-600 text-xs mt-2">
              اكتب وصفاً للخطاب (5 أحرف على الأقل)
            </p>
          )}
          <div className="flex items-center justify-between mt-2 text-xs text-ink-300">
            <span>{request.length} حرف</span>
            <span className="text-ink-500">اللغة العربية</span>
          </div>
        </div>

        {/* Collapsible: known placeholders */}
        <div className="border-t border-sage-100 pt-4">
          <button
            type="button"
            onClick={() => setShowFields((s) => !s)}
            className="flex items-center gap-2 text-sage-700 text-sm font-semibold"
          >
            {showFields ? <ChevronUp className="w-4 h-4" /> : <ChevronDown className="w-4 h-4" />}
            أضف معلومات معروفة (اختياري)
          </button>
          <AnimatePresence>
            {showFields && (
              <motion.div
                initial={{ height: 0, opacity: 0 }}
                animate={{ height: 'auto', opacity: 1 }}
                exit={{ height: 0, opacity: 0 }}
                transition={{ duration: 0.2 }}
                className="overflow-hidden"
              >
                <div className="grid sm:grid-cols-2 gap-3 pt-4">
                  <div>
                    <label className="block text-xs font-semibold text-ink-500 mb-1.5">
                      نوع المُراسَل
                    </label>
                    <select
                      value={fields.entity_type}
                      onChange={(e) => setFields({ ...fields, entity_type: e.target.value })}
                      className="input-base py-2.5 text-sm"
                    >
                      <option value="organization">شركة / مؤسسة / جهة</option>
                      <option value="individual">شخص / فرد / مسؤول</option>
                    </select>
                  </div>
                  {PRESET_FIELDS.map((f) => (
                    <div key={f.name}>
                      <label className="block text-xs font-semibold text-ink-500 mb-1.5">
                        {f.label}
                      </label>
                      <input
                        type="text"
                        value={fields[f.name] || ''}
                        onChange={(e) => setFields({ ...fields, [f.name]: e.target.value })}
                        className="input-base py-2.5 text-sm"
                        placeholder="اتركه فارغاً لتعبئته لاحقاً"
                      />
                    </div>
                  ))}
                </div>
              </motion.div>
            )}
          </AnimatePresence>
        </div>

        <button type="submit" className="btn-primary w-full text-lg">
          إنشاء الخطاب
          <ArrowLeft className="w-5 h-5" />
        </button>
      </form>

      {/* Examples */}
      <div>
        <div className="flex items-center gap-2 mb-3 text-ink-500 text-sm font-semibold">
          <Lightbulb className="w-4 h-4 text-wood-600" />
          أمثلة جاهزة
        </div>
        <div className="grid sm:grid-cols-2 gap-2.5">
          {EXAMPLES.map((ex, i) => (
            <button
              key={i}
              onClick={() => fillExample(ex)}
              className="text-right rounded-2xl border border-sage-100 bg-white px-4 py-3
                         text-sm text-ink-700 shadow-soft transition
                         hover:border-sage-300 hover:bg-sage-50 active:scale-[0.98]"
            >
              {ex}
            </button>
          ))}
        </div>
      </div>
    </div>
  )
}
