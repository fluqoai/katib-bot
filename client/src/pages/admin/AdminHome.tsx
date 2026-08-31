import { useEffect, useState } from 'react'
import { motion } from 'framer-motion'
import { FileText, Scale, BookCheck, ChevronLeft, Loader2 } from 'lucide-react'
import { CATEGORY_AR, CATEGORY_DESC, getStats, type DocCategory, type Stats } from '../../lib/api'

const CARDS: { category: DocCategory; icon: React.ElementType; tint: string }[] = [
  { category: 'templates',            icon: FileText,  tint: 'from-wood-100 to-wood-50 text-wood-700' },
  { category: 'national_regulations', icon: Scale,     tint: 'from-sage-100 to-sage-50 text-sage-700' },
  { category: 'internal_policies',    icon: BookCheck, tint: 'from-slate-100 to-slate-50 text-slate-700' },
]

export default function AdminHome({
  onOpenCategory,
  onBack,
}: {
  onOpenCategory: (c: DocCategory) => void
  onBack: () => void
}) {
  const [stats, setStats] = useState<Stats | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    getStats().then(setStats).finally(() => setLoading(false))
  }, [])

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <button onClick={onBack} className="btn-ghost text-ink-500">
          <ChevronLeft className="w-4 h-4" />
          الرئيسية
        </button>
        <h1 className="text-2xl font-extrabold text-ink-900">الإدارة</h1>
        <div className="w-16" />
      </div>

      <p className="text-sm text-ink-500 leading-relaxed">
        هنا يمكنك إدارة ملفات الجمعية: رفع ملفات جديدة، أو حذف ملفات قديمة، أو إلغاء تفعيل ملف دون حذفه.
      </p>

      {loading ? (
        <div className="card flex items-center justify-center py-10">
          <Loader2 className="w-5 h-5 animate-spin text-sage-600" />
        </div>
      ) : (
        <div className="grid sm:grid-cols-3 gap-4">
          {CARDS.map(({ category, icon: Icon, tint }) => {
            const count = stats?.by_category?.[category] || 0
            return (
              <motion.button
                key={category}
                whileTap={{ scale: 0.98 }}
                onClick={() => onOpenCategory(category)}
                className="card text-right hover:shadow-soft-lg transition group"
              >
                <div className={`w-12 h-12 rounded-2xl bg-gradient-to-br ${tint}
                                flex items-center justify-center mb-3`}>
                  <Icon className="w-6 h-6" strokeWidth={1.8} />
                </div>
                <div className="font-extrabold text-ink-900 text-base">
                  {CATEGORY_AR[category]}
                </div>
                <div className="text-xs text-ink-500 mt-1 leading-relaxed">
                  {CATEGORY_DESC[category]}
                </div>
                <div className="mt-4 flex items-center justify-between">
                  <span className="pill pill-sage">
                    {count} {count === 1 ? 'ملف' : 'ملفات'}
                  </span>
                  <ChevronLeft className="w-4 h-4 text-ink-300 group-hover:text-sage-600 transition" />
                </div>
              </motion.button>
            )
          })}
        </div>
      )}

      {stats && (
        <div className="card-quiet">
          <div className="flex items-center justify-between text-sm">
            <span className="text-ink-500">إجمالي الملفات</span>
            <span className="font-extrabold text-ink-900">{stats.total}</span>
          </div>
        </div>
      )}
    </div>
  )
}
