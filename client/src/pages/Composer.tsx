import { useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import {
  Sparkles, ChevronDown, ChevronUp, ArrowLeft, Lightbulb, SlidersHorizontal,
  BookOpenCheck, Info, Calendar, ScrollText, UserCircle2, PenLine,
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
  { name: 'recipient_name', label: 'الجهة المُراسَلة' },
  { name: 'partner_org',    label: 'الجهة الشريكة (إن وُجدت)' },
  { name: 'project_name',   label: 'اسم المشروع / الموضوع' },
  { name: 'event_date',     label: 'تاريخ المناسبة' },
]

// Each "section" of the letter skeleton the user can opt out of.
// The defaults are populated by rag.intent.default_style_for() and
// can be flipped via style_overrides.
type SectionKey = 'include_basmala' | 'include_date_row' | 'include_recipient_block' | 'include_signature_block'
const STYLE_SECTIONS: { key: SectionKey; label: string; icon: React.ElementType; hint: string }[] = [
  { key: 'include_basmala',          label: 'بسملة',              icon: BookOpenCheck,  hint: 'بسم الله الرحمن الرحيم في أعلى الخطاب' },
  { key: 'include_date_row',         label: 'صف التاريخ',         icon: Calendar,       hint: 'التاريخ بالميلادي والهجري في أول الخطاب' },
  { key: 'include_recipient_block',  label: 'سطر المُراسَل',      icon: UserCircle2,    hint: 'سعادة / (...) المحترم' },
  { key: 'include_signature_block',  label: 'كتلة التوقيع',       icon: PenLine,        hint: 'اسم المُوقِّع + صفتة في أسفل الخطاب' },
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
  const [fields, setFields] = useState<Record<string, string>>({})
  const [showFields, setShowFields] = useState(false)
  const [showAdvanced, setShowAdvanced] = useState(false)
  const [useLegacy, setUseLegacy] = useState(false)
  const [sections, setSections] = useState<Record<SectionKey, boolean>>({
    include_basmala: true,
    include_date_row: true,
    include_recipient_block: true,
    include_signature_block: true,
  })
  const [touched, setTouched] = useState(false)

  const valid = request.trim().length >= 5

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    if (!valid) {
      setTouched(true)
      return
    }
    // Build style_overrides from the section toggles. Only include the
    // keys the user actually flipped off (so we don't override defaults
    // they didn't touch).
    const overrides: LetterStyleOverrides = {}
    ;(Object.keys(sections) as SectionKey[]).forEach((k) => {
      // Default in default_style_for() is True for all four; we only
      // send an override when the user has flipped it OFF.
      if (!sections[k]) overrides[k] = false
    })
    onSubmit(request.trim(), fields, {
      use_legacy_template: useLegacy,
      style_overrides: overrides,
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

        {/* Phase 4: Advanced settings — formatting + legacy toggle */}
        <div className="border-t border-sage-100 pt-4">
          <button
            type="button"
            onClick={() => setShowAdvanced((s) => !s)}
            className="flex items-center gap-2 text-sage-700 text-sm font-semibold"
          >
            {showAdvanced ? <ChevronUp className="w-4 h-4" /> : <ChevronDown className="w-4 h-4" />}
            <SlidersHorizontal className="w-4 h-4" />
            إعدادات متقدمة
            <span className="text-[10px] text-ink-300 font-normal mr-1">(التنسيق · القوالب)</span>
          </button>
          <AnimatePresence>
            {showAdvanced && (
              <motion.div
                initial={{ height: 0, opacity: 0 }}
                animate={{ height: 'auto', opacity: 1 }}
                exit={{ height: 0, opacity: 0 }}
                transition={{ duration: 0.25 }}
                className="overflow-hidden"
              >
                <div className="pt-4 space-y-4">
                  {/* Info banner explaining the default */}
                  <div className="rounded-2xl bg-sage-50 border border-sage-100 px-4 py-3 flex items-start gap-2.5">
                    <Info className="w-4 h-4 text-sage-700 flex-shrink-0 mt-0.5" />
                    <p className="text-xs text-sage-800 leading-relaxed">
                      افتراضياً يستخدم النظام تنسيقاً رسمياً قياسياً مستقلاً عن أي قالب.
                      يمكنك تعديل عناصر التنسيق أدناه، أو تفعيل القوالب القديمة لتطابق قالباً محفوظاً في النظام.
                    </p>
                  </div>

                  {/* Section toggles */}
                  <div>
                    <div className="text-xs font-bold text-ink-500 mb-2.5">عناصر الخطاب</div>
                    <div className="grid sm:grid-cols-2 gap-2">
                      {STYLE_SECTIONS.map((s) => {
                        const Icon = s.icon
                        const on = sections[s.key]
                        return (
                          <button
                            type="button"
                            key={s.key}
                            onClick={() => setSections((prev) => ({ ...prev, [s.key]: !on }))}
                            className={`text-right rounded-xl border px-3 py-2.5 flex items-center gap-2.5 transition
                              ${on
                                ? 'border-sage-200 bg-sage-50/60 hover:border-sage-300'
                                : 'border-ink-100 bg-white hover:border-ink-200'}`}
                          >
                            <div className={`w-8 h-8 rounded-lg flex items-center justify-center flex-shrink-0
                              ${on ? 'bg-sage-600 text-white' : 'bg-ink-100 text-ink-400'}`}>
                              <Icon className="w-4 h-4" />
                            </div>
                            <div className="flex-1 min-w-0">
                              <div className={`text-sm font-semibold ${on ? 'text-ink-900' : 'text-ink-400'}`}>
                                {s.label}
                              </div>
                              <div className={`text-[11px] leading-tight mt-0.5 ${on ? 'text-ink-500' : 'text-ink-300'}`}>
                                {s.hint}
                              </div>
                            </div>
                            <div className={`w-9 h-5 rounded-full flex-shrink-0 transition-colors
                              ${on ? 'bg-sage-600' : 'bg-ink-200'}`}>
                              <div className={`w-4 h-4 rounded-full bg-white shadow-sm mt-0.5 transition-transform
                                ${on ? 'translate-x-4.5' : 'translate-x-0.5'}`}
                                style={{ transform: on ? 'translateX(18px)' : 'translateX(2px)' }}
                              />
                            </div>
                          </button>
                        )
                      })}
                    </div>
                  </div>

                  {/* Legacy template toggle */}
                  <div className="border-t border-sage-100 pt-4">
                    <div className="text-xs font-bold text-ink-500 mb-2">القوالب القديمة</div>
                    <button
                      type="button"
                      onClick={() => setUseLegacy((v) => !v)}
                      className={`w-full text-right rounded-2xl border px-4 py-3 flex items-center gap-3 transition
                        ${useLegacy
                          ? 'border-amber-200 bg-amber-50'
                          : 'border-sage-100 bg-white hover:border-sage-200'}`}
                    >
                      <div className={`w-10 h-10 rounded-xl flex items-center justify-center flex-shrink-0
                        ${useLegacy ? 'bg-amber-500 text-white' : 'bg-sage-100 text-sage-600'}`}>
                        <ScrollText className="w-5 h-5" />
                      </div>
                      <div className="flex-1 min-w-0">
                        <div className={`text-sm font-bold ${useLegacy ? 'text-amber-900' : 'text-ink-900'}`}>
                          استخدام قالب قديم محفوظ
                        </div>
                        <div className={`text-xs mt-0.5 leading-snug ${useLegacy ? 'text-amber-800' : 'text-ink-500'}`}>
                          {useLegacy
                            ? 'سيُستخدم أقرب قالب من قوالب النظام لتوليد الخطاب. هذا الخيار قديم وقد ينتج خطابات بهيكل قالب خاطئ.'
                            : 'افتراضياً معطّل. يعتمد النظام على لوائح وسياسات الجمعية فقط.'}
                        </div>
                      </div>
                      <div className={`w-10 h-6 rounded-full flex-shrink-0 transition-colors
                        ${useLegacy ? 'bg-amber-500' : 'bg-ink-200'}`}>
                        <div className={`w-4 h-4 rounded-full bg-white shadow-sm mt-1 transition-transform`}
                          style={{ transform: useLegacy ? 'translateX(20px)' : 'translateX(2px)' }}
                        />
                      </div>
                    </button>
                  </div>
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
