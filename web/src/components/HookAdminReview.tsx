import React, {useEffect, useRef, useState} from 'react'
import {api, type AdminHookReview, type HookReviewRequestRow} from '../api'
import {useI18n} from '../i18n'

/** Admin-only control; server independently enforces authorization. */
export function HookAdminReview() {
  const {t}=useI18n()
  const alive=useRef(true), busy=useRef(false)
  const [loading,setLoading]=useState(false)
  const [rows,setRows]=useState<HookReviewRequestRow[]>([])
  const [detail,setDetail]=useState<AdminHookReview|null>(null)
  const [message,setMessage]=useState('')
  const [error,setError]=useState('')
  useEffect(()=>{alive.current=true;return ()=>{alive.current=false}},[])
  async function run(action:()=>Promise<void>, failure:string) {
    if (busy.current) return
    busy.current=true; setLoading(true); setError(''); setMessage('')
    try {await action()} catch(e) {if(alive.current){setDetail(null);setError(`${failure} ${String(e)}`)}}
    finally {busy.current=false;if(alive.current)setLoading(false)}
  }
  function list() {return run(async()=>{
    setDetail(null)
    const result=await api.listHookReviewRequests()
    if(alive.current){setRows(result.requests);setMessage(t('Danh sách chưa được xác minh lại.', 'List is not revalidated.'))}
  },t('Không tải được yêu cầu.', 'Could not load requests.'))}
  function review(id:string) {return run(async()=>{
    setDetail(null)
    const result=await api.getHookReviewRequest(id)
    if(result.request_id!==id || typeof result.workspace!=='string' || !result.workspace || !/^[0-9a-f]{64}$/.test(result.digest) || !['pending','approved','denied'].includes(result.status)) throw new Error('Invalid review response')
    if(alive.current)setDetail(result)
  },t('Không thể xem yêu cầu; hãy tải lại.', 'Could not review request; reload the list.'))}
  function decide(choice:'approve'|'deny') {
    if(!detail || detail.status!=='pending' || busy.current)return
    const message=t(`Ghi nhận ${choice} cho workspace ${JSON.stringify(detail.workspace)}\nSHA-256: ${detail.digest}?\nDuyệt chỉ được kiểm tra trong lượt chat thông thường (không phải demo) và thao tác nén thủ công; đường khác vẫn có thể chạy hook.`,
      `Record ${choice} for workspace ${JSON.stringify(detail.workspace)}\nSHA-256: ${detail.digest}?\nHook trust is not enforced everywhere; normal non-demo chat and manual compaction check approval, while other paths may still run hooks.`)
    if(!confirm(message))return
    return run(async()=>{
      const result=await api.decideHookReviewRequest(detail.request_id,choice,detail.workspace,detail.digest)
      if(result.request_id!==detail.request_id || result.status!==(choice==='approve'?'approved':'denied')) throw new Error('Invalid decision response')
      if(alive.current){setDetail({...detail,status:result.status});setMessage(t('Đã ghi nhận quyết định; chỉ lượt chat thông thường (không phải demo) và thao tác nén thủ công, kiểm tra duyệt.', 'Decision recorded; hook trust is not enforced everywhere. Normal non-demo chat and manual compaction check approval.'))}
    },t('Không xác nhận được quyết định; có thể đã được ghi. Hãy tải lại yêu cầu.', 'Could not confirm decision; it may have been recorded. Reload the request.'))
  }
  return <section className="hook-admin-review" aria-label={t('Admin xem yêu cầu hook','Admin hook requests')}>
    <button type="button" aria-label={t('Danh sách yêu cầu hook cho admin','List admin hook requests')} disabled={loading} onClick={list}>{t('Admin: xem yêu cầu hook','Admin: list hook requests')}</button>
    <p role="status">{t('Duyệt được kiểm tra trong lượt chat thông thường (không phải demo) và thao tác nén thủ công; các đường khác vẫn có thể chạy hook.', 'Hook trust is not enforced everywhere: normal non-demo chat and manual compaction check approval; other paths may run hooks.')}</p>
    {message && <p role="status">{message}</p>}{error && <p role="alert">{error}</p>}
    {rows.map(row=><div key={row.request_id}>
      <p><code>{row.request_id}</code> — {row.status} — {t('chưa xác minh lại','not revalidated')}</p>
      <button type="button" aria-label={t(`Xem yêu cầu hook ${row.request_id}`,`Review hook request ${row.request_id}`)} disabled={loading} onClick={()=>review(row.request_id)}>{t('Xem cấu hình','Review configuration')}</button>
    </div>)}
    {detail && <div>
      <p>{t('Chủ phiên','Owner')}: <code>{detail.owner_id}</code>; {t('phiên','session')}: <code>{detail.session_id}</code></p>
      <p>Workspace: <code>{JSON.stringify(detail.workspace)}</code><br/>SHA-256: <code>{detail.digest}</code></p>
      <p>{t('Trạng thái','Status')}: {detail.status}. {t('Ảnh chụp có thể cũ; script được gọi có thể thay đổi.', 'Snapshot may become stale; referenced scripts may change.')}</p>
      {detail.overrides_global_hooks && <p role="alert">{t('Cấu hình ghi đè hook vòng đời toàn cục.', 'Configuration overrides global lifecycle hooks.')}</p>}
      <pre className="hook-review-config">{JSON.stringify({hooks:detail.hooks,on_file_change:detail.on_file_change},null,2)}</pre>
      <p>{t('Lệnh có thể chạy trên máy chủ và truy cập dữ liệu hoặc mạng.', 'Commands may run on the host and access files or the network.')}</p>
      <button type="button" aria-label={t('Duyệt yêu cầu hook','Approve hook request')} disabled={loading || detail.status!=='pending'} onClick={()=>decide('approve')}>{t('Ghi nhận duyệt','Record approval')}</button>{' '}
      <button type="button" aria-label={t('Từ chối yêu cầu hook','Deny hook request')} disabled={loading || detail.status!=='pending'} onClick={()=>decide('deny')}>{t('Ghi nhận từ chối','Record denial')}</button>
    </div>}
  </section>
}
