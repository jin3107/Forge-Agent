import React, { useEffect, useRef, useState } from 'react'
import { api, type HookReviewRequestRow, type WorkspaceHookReview } from '../api'
import { useI18n } from '../i18n'

type View = {sid:string; rows:HookReviewRequestRow[]; message:string; error:string; loading:boolean}

/** Request sharing and revocation only; decisions do not enforce hook trust. */
export function HookReviewRequests({sid, preview}: {sid:string; preview:WorkspaceHookReview | null}) {
  const {t} = useI18n()
  const seq = useRef(0)
  const busy = useRef(false)
  const [view, setView] = useState<View>({sid,rows:[],message:'',error:'',loading:false})
  useEffect(() => {
    seq.current++; busy.current = false
    setView({sid,rows:[],message:'',error:'',loading:false})
    return () => { seq.current++ }
  }, [sid])
  const current = view.sid === sid ? view : {sid,rows:[],message:'',error:'',loading:false}
  const ready = !!sid && preview?.state === 'hooks' && typeof preview.workspace === 'string' && !!preview.workspace && typeof preview.digest === 'string' && /^[0-9a-f]{64}$/.test(preview.digest)

  async function run(action:()=>Promise<Partial<View>>, uncertain:string) {
    if (!sid || busy.current) return
    busy.current = true
    const n=++seq.current
    setView(v=>({...(v.sid === sid ? v : {sid,rows:[],message:'',error:'',loading:false}),loading:true,message:'',error:''}))
    try {
      const result=await action()
      if (seq.current === n) setView(v=>({...v,...result,loading:false}))
    } catch (error) {
      if (seq.current === n) setView(v=>({...v,loading:false,error:`${uncertain} ${String(error)}`}))
    } finally {
      if (seq.current === n) busy.current=false
    }
  }
  function requestReview() {
    if (!ready || !preview || busy.current) return
    const text=t(
      `Chia sẻ cấu hình hook và lệnh với TẤT CẢ admin? Nội dung có thể chứa thông tin nhạy cảm. Workspace: ${JSON.stringify(preview.workspace)}\nSHA-256: ${preview.digest}\nViệc duyệt chỉ được kiểm tra trong lượt chat thông thường (không phải demo) và thao tác nén thủ công; đường khác vẫn có thể chạy hook.`,
      `Share hook configuration and commands with ALL admins? They may contain sensitive data. Workspace: ${JSON.stringify(preview.workspace)}\nSHA-256: ${preview.digest}\nHook trust is not enforced everywhere; normal non-demo chat and manual compaction check approval, while other paths may still run hooks.`)
    if (!confirm(text)) return
    void run(async()=>{
      const r=await api.requestHookReview(sid,preview.workspace,preview.digest!)
      return {message:t(`Đã gửi yêu cầu ${r.request_id}; chỉ lượt chat thông thường (không phải demo) và thao tác nén thủ công, kiểm tra duyệt; đường khác vẫn có thể chạy hook.`, `Request ${r.request_id} submitted; hook trust is not enforced everywhere; normal non-demo chat and manual compaction check approval.`)}
    },t('Không xác nhận được kết quả; yêu cầu có thể đã được tạo. Hãy tải lại danh sách.', 'Could not confirm the result; the request may have been created. Reload the list.'))
  }
  function list() {
    return run(async()=>{
      const r=await api.listHookReviewRequests()
      return {rows:r.requests.filter(x=>x.session_id===sid),message:t('Danh sách là ảnh chụp chưa xác minh lại.', 'Request list is a snapshot and is not revalidated.')}
    },t('Không tải được danh sách.', 'Could not load request list.'))
  }
  function revoke(id:string) {
    if (!confirm(t('Thu hồi yêu cầu này? Không thể thu hồi nội dung đã gửi cho admin.', 'Revoke this request? Content already disclosed to admins cannot be recalled.'))) return
    return run(async()=>{await api.revokeHookReviewRequest(id);return {rows:current.rows.filter(x=>x.request_id!==id),message:t('Đã thu hồi yêu cầu.', 'Request revoked.')}},
      t('Không xác nhận được kết quả thu hồi; hãy tải lại danh sách.', 'Could not confirm revocation; reload the list.'))
  }
  return <div className="hook-review-requests">
    <button type="button" aria-label={t('Yêu cầu admin xem hook', 'Request admin hook review')} disabled={!ready || current.loading} onClick={requestReview}>{t('Yêu cầu admin xem hook', 'Request admin hook review')}</button>{' '}
    <button type="button" aria-label={t('Danh sách yêu cầu hook của phiên', 'List my hook review requests')} disabled={!sid || current.loading} onClick={list}>{t('Xem yêu cầu của phiên', 'List session requests')}</button>
    {current.message && <p role="status">{current.message}</p>}
    {current.error && <p role="alert">{current.error}</p>}
    {current.rows.map(row=><div key={row.request_id}>
      <p>{t('Yêu cầu', 'Request')} <code>{row.request_id}</code>: {row.status} — {t('chưa xác minh lại', 'not revalidated')}</p>
      <p>{t('Workspace', 'Workspace')}: <code>{JSON.stringify(row.workspace)}</code><br/>SHA-256: <code>{row.digest}</code></p>
      <button type="button" aria-label={t(`Thu hồi yêu cầu hook ${row.request_id}`, `Revoke hook review request ${row.request_id}`)} disabled={current.loading} onClick={()=>revoke(row.request_id)}>{t('Thu hồi', 'Revoke')}</button>
    </div>)}
  </div>
}
