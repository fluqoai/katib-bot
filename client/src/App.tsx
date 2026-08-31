import { useEffect, useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { Feather, Settings } from 'lucide-react'
import Composer, { type ComposerOptions } from './pages/Composer'
import Processing from './pages/Processing'
import Result from './pages/Result'
import AdminHome from './pages/admin/AdminHome'
import CategoryPage from './pages/admin/CategoryPage'
import { getHealth, type GenerateResult, type LetterStyleOverrides } from './lib/api'

type View =
  | { name: 'composer' }
  | {
      name: 'processing'
      request: string
      fields: Record<string, string>
      useLegacyTemplate: boolean
      styleOverrides: LetterStyleOverrides
    }
  | {
      name: 'result'
      result: GenerateResult
      request: string
      fields: Record<string, string>
    }
  | { name: 'admin' }
  | { name: 'admin-category'; category: string }

export default function App() {
  const [view, setView] = useState<View>({ name: 'composer' })
  const [serverOk, setServerOk] = useState<boolean | null>(null)

  useEffect(() => {
    getHealth()
      .then(() => setServerOk(true))
      .catch(() => setServerOk(false))
  }, [])

  function startProcessing(
    request: string,
    fields: Record<string, string>,
    options: ComposerOptions,
  ) {
    setView({
      name: 'processing',
      request,
      fields,
      useLegacyTemplate: options.use_legacy_template,
      styleOverrides: options.style_overrides,
    })
  }
  function finishProcessing(result: GenerateResult, request: string, fields: Record<string, string>) {
    setView({ name: 'result', result, request, fields })
  }
  function startOver() {
    setView({ name: 'composer' })
  }

  return (
    <div className="min-h-screen flex flex-col">
      <TopBar
        onLogoClick={startOver}
        onAdminClick={() => setView({ name: 'admin' })}
        inAdmin={view.name === 'admin' || view.name === 'admin-category'}
        serverOk={serverOk}
      />

      <main className="flex-1 max-w-3xl w-full mx-auto px-5 sm:px-8 pb-28 pt-6 sm:pt-10">
        <AnimatePresence mode="wait">
          {view.name === 'composer' && (
            <motion.div
              key="composer"
              initial={{ opacity: 0, y: 12 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -12 }}
              transition={{ duration: 0.3 }}
            >
              <Composer onSubmit={startProcessing} />
            </motion.div>
          )}

          {view.name === 'processing' && (
            <motion.div
              key="processing"
              initial={{ opacity: 0, y: 12 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -12 }}
              transition={{ duration: 0.3 }}
            >
              <Processing
                request={view.request}
                fields={view.fields}
                useLegacyTemplate={view.useLegacyTemplate}
                styleOverrides={view.styleOverrides}
                onDone={(r) => finishProcessing(r, view.request, view.fields)}
                onCancel={startOver}
              />
            </motion.div>
          )}

          {view.name === 'result' && (
            <motion.div
              key="result"
              initial={{ opacity: 0, y: 12 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -12 }}
              transition={{ duration: 0.3 }}
            >
              <Result
                result={view.result}
                request={view.request}
                fields={view.fields}
                onStartOver={startOver}
              />
            </motion.div>
          )}

          {view.name === 'admin' && (
            <motion.div
              key="admin"
              initial={{ opacity: 0, y: 12 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -12 }}
              transition={{ duration: 0.3 }}
            >
              <AdminHome
                onOpenCategory={(c) => setView({ name: 'admin-category', category: c })}
                onBack={startOver}
              />
            </motion.div>
          )}

          {view.name === 'admin-category' && (
            <motion.div
              key={`admin-${view.category}`}
              initial={{ opacity: 0, y: 12 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -12 }}
              transition={{ duration: 0.3 }}
            >
              <CategoryPage
                category={view.category as any}
                onBack={() => setView({ name: 'admin' })}
              />
            </motion.div>
          )}
        </AnimatePresence>
      </main>
    </div>
  )
}

function TopBar({
  onLogoClick,
  onAdminClick,
  inAdmin,
  serverOk,
}: {
  onLogoClick: () => void
  onAdminClick: () => void
  inAdmin: boolean
  serverOk: boolean | null
}) {
  return (
    <header className="sticky top-0 z-30 bg-linen/80 backdrop-blur-md border-b border-sage-100">
      <div className="max-w-3xl mx-auto px-5 sm:px-8 h-16 flex items-center justify-between">
        <button
          onClick={onLogoClick}
          className="flex items-center gap-2.5 active:scale-95 transition"
          aria-label="الصفحة الرئيسية"
        >
          <div className="w-9 h-9 rounded-xl bg-sage-600 text-white flex items-center justify-center shadow-soft">
            <Feather className="w-5 h-5" strokeWidth={2.4} />
          </div>
          <div className="text-right">
            <div className="text-lg font-extrabold text-sage-900 leading-none">كاتب</div>
            <div className="text-[11px] text-sage-600 mt-0.5">مساعد كتابة الخطابات</div>
          </div>
        </button>

        <div className="flex items-center gap-2">
          {serverOk === false && (
            <span className="pill pill-rose">
              <span className="w-1.5 h-1.5 rounded-full bg-rose-500" />
              غير متصل
            </span>
          )}
          {serverOk === true && (
            <span className="pill pill-sage hidden sm:inline-flex">
              <span className="w-1.5 h-1.5 rounded-full bg-sage-500" />
              متصل
            </span>
          )}
          <button
            onClick={onAdminClick}
            className={`flex items-center gap-1.5 rounded-xl px-3 py-2 text-sm font-semibold transition
              ${inAdmin
                ? 'bg-sage-100 text-sage-800'
                : 'text-sage-700 hover:bg-sage-100'}`}
          >
            <Settings className="w-4 h-4" />
            <span className="hidden sm:inline">الإدارة</span>
          </button>
        </div>
      </div>
    </header>
  )
}
