import React, { useEffect, useRef, useState } from 'react'
import { api, type WorkspaceHookReview } from '../api'
import { useI18n } from '../i18n'
import { HookReviewRequests } from './HookReviewRequests'
import { HookAdminReview } from './HookAdminReview'

type View = { sid: string; loading: boolean; data?: WorkspaceHookReview; error?: string }

/** Read-only inspection only. Never implies approval or execution prevention. */
export function HookReview({ sid, isAdmin = false }: { sid: string; isAdmin?: boolean }) {
  const { t } = useI18n()
  const sequence = useRef(0)
  const [view, setView] = useState<View | null>(null)
  useEffect(() => {
    sequence.current++
    setView(null)
    return () => { sequence.current++ }
  }, [sid])

  async function inspect() {
    if (!sid) return
    const request = ++sequence.current
    setView({sid, loading:true})
    try {
      const data = await api.reviewWorkspaceHooks(sid)
      if (sequence.current === request) setView({sid, loading:false, data})
    } catch (error) {
      if (sequence.current === request) setView({sid, loading:false, error:String(error)})
    }
  }
  const current = view?.sid === sid ? view : null
  return <section className="hook-review" aria-label={t('Xem hook workspace', 'Workspace hook review')}>
    <button type="button" aria-label={t('Xem hook workspace', 'Review workspace hooks')} disabled={!sid || current?.loading} onClick={inspect}>
      {current?.loading ? t('Đang kiểm tra hook…', 'Checking hooks…') : t('Xem hook workspace', 'Review workspace hooks')}
    </button>
    <p role="status">{t('Kiểm tra duyệt chỉ áp dụng cho lượt chat thông thường (không phải demo) và thao tác nén thủ công; các đường khác vẫn có thể chạy hook. Màn hình này không duyệt hay chặn hook.', 'Hook trust is not enforced everywhere: normal non-demo chat and manual compaction check approval; other paths may still run hooks. This screen does not approve or block hooks.')}</p>
    {current?.error && <p role="alert">{t('Không thể xem hook:', 'Could not review hooks:')} {current.error}</p>}
    <HookReviewRequests sid={sid} preview={current?.data ?? null} />
    {isAdmin && <HookAdminReview />}
    {current?.data && <div>
      <p>{t('Đây là ảnh chụp tại thời điểm xem; cấu hình hoặc script có thể thay đổi.', 'This is a snapshot; configuration or referenced scripts may change.')}</p>
      <p>{t('Workspace tại lúc kiểm tra', 'Workspace when checked')}: <code>{typeof current.data.workspace === 'string' ? JSON.stringify(current.data.workspace) : t('(không xác nhận được)', '(unavailable)')}</code></p>
      <p>{t('Trạng thái', 'State')}: {current.data.state}</p>
      {current.data.overrides_global_hooks && <p role="alert">{t('Cấu hình này ghi đè toàn bộ hook vòng đời toàn cục.', 'This configuration overrides global lifecycle hooks.')}</p>}
      {current.data.state === 'invalid' && <p role="alert">{t('Không đọc hoặc xác minh được cấu hình; không coi workspace là an toàn.', 'The configuration could not be read or validated; do not assume the workspace is safe.')}</p>}
      {current.data.digest && <p>SHA-256: <code>{current.data.digest}</code></p>}
      <pre className="hook-review-config">{JSON.stringify({hooks:current.data.hooks, on_file_change:current.data.on_file_change}, null, 2)}</pre>
      <p>{t('Hook có thể chạy lệnh trên máy chủ; xem script được gọi trước khi tin cậy.', 'Hooks can run commands on the host; review referenced scripts before trusting them.')}</p>
    </div>}
  </section>
}
