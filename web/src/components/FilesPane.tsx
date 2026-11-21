import React, { useRef, useState } from 'react'
import { api } from '../api'
import type { FileNode } from '../types'
import { useI18n } from '../i18n'
import { HookReview } from './HookReview'

function TreeRow(props: {
  node: FileNode
  depth: number
  path: string
  selected: string | null
  onOpen: (p: string) => void
}) {
  const { node, depth, path, selected, onOpen } = props
  const [open, setOpen] = useState(depth < 2)
  if (node.type === 'dir') {
    return (
      <div>
        <button type="button" className="tree-row dir" style={{ paddingLeft: 8 + depth * 14 }}
          aria-expanded={open} onClick={() => setOpen(!open)}>
          <span className="chev-sm">{open ? '▾' : '▸'}</span> 📁 {node.name}
        </button>
        {open && (node.children ?? []).map((c) => (
          <TreeRow key={c.name + (c.path ?? '')} node={c} depth={depth + 1}
            path={path ? `${path}/${c.name}` : c.name} selected={selected} onOpen={onOpen} />
        ))}
      </div>
    )
  }
  const full = path
  return (
    <button type="button" className={`tree-row file ${selected === full ? 'active' : ''}`}
      style={{ paddingLeft: 8 + depth * 14 + 14 }} onClick={() => onOpen(full)}>
      📄 {node.name}
    </button>
  )
}

function Terminal({ sid }: { sid: string }) {
  const { t } = useI18n()
  const [cmd, setCmd] = useState('')
  const [out, setOut] = useState('')
  const [busy, setBusy] = useState(false)
  const histRef = useRef<string[]>([])
  const histIdx = useRef(-1)
  const outRef = useRef<HTMLPreElement>(null)

  async function run() {
    const c = cmd.trim()
    if (!c || busy) return
    setCmd('')
    histRef.current.unshift(c)
    histIdx.current = -1
    setBusy(true)
    setOut((o) => `${o}$ ${c}\n`)
    try {
      const r = await api.exec(sid, c)
      const text = r.blocked ? `⛔ ${r.output}` : (r.output || '(no output)')
      setOut((o) => `${o}${text}\n`)
    } catch (e) {
      setOut((o) => `${o}⚠ ${String(e)}\n`)
    } finally {
      setBusy(false)
      requestAnimationFrame(() => { if (outRef.current) outRef.current.scrollTop = outRef.current.scrollHeight })
    }
  }

  return (
    <div className="term">
      <div className="term-head">TERMINAL</div>
      <pre className="term-out" ref={outRef}>{out || t('(gõ lệnh để chạy trong workspace — ví dụ: dir / ls)', '(type a command to run in the workspace — e.g. dir / ls)')} </pre>
      <div className="term-row">
        <span className="term-ps1">$</span>
        <input
          value={cmd}
          aria-label={t('Lệnh terminal', 'Terminal command')}
          disabled={busy}
          placeholder={busy ? t('đang chạy…', 'running…') : t('lệnh…', 'command…')}
          onChange={(e) => setCmd(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') { e.preventDefault(); run() }
            else if (e.key === 'ArrowUp' && histRef.current.length) {
              histIdx.current = Math.min(histIdx.current + 1, histRef.current.length - 1)
              setCmd(histRef.current[histIdx.current])
            } else if (e.key === 'ArrowDown') {
              histIdx.current = Math.max(histIdx.current - 1, -1)
              setCmd(histIdx.current >= 0 ? histRef.current[histIdx.current] : '')
            }
          }}
        />
      </div>
    </div>
  )
}

export function FilesPane(props: {
  sid: string
  isAdmin?: boolean
  tree: FileNode[]
  selected: string | null
  content: string | null
  binary: boolean
  missingVersion: boolean
  mixedNewlines: boolean
  dirty: boolean
  saving: boolean
  deleting: boolean
  deleted: boolean
  locked: boolean
  onOpen: (p: string) => void
  onChange: (c: string) => void
  onSave: () => void
  onDelete: () => void
  onRefresh: () => void
  onNewFile: () => void
  onUpload: (fs: FileList | null) => void
  onExport: () => void
  onClose?: () => void
}) {
  const { t } = useI18n()
  const uploadRef = useRef<HTMLInputElement>(null)
  return (
    <aside className="files-pane" aria-label="Workspace">
      <div className="files-head">
        <span>WORKSPACE</span>
        <div className="files-actions">
          <button title={t('Tải tối đa 20 file; file trùng tên sẽ bị bỏ qua', 'Upload up to 20 files; existing names are skipped')} aria-label={t('Tải file lên workspace', 'Upload files to workspace')} onClick={() => uploadRef.current?.click()}>⬆</button>
          <input ref={uploadRef} type="file" multiple style={{ display: 'none' }} aria-label={t('Chọn file tải lên', 'Choose files to upload')}
            onChange={(e) => { props.onUpload(e.target.files); e.currentTarget.value = '' }} />
          <button title={t('Xuất workspace (.zip)', 'Export workspace (.zip)')} aria-label={t('Xuất workspace', 'Export workspace')} onClick={props.onExport}>⤓</button>
          <button title={t('File mới', 'New file')} aria-label={t('File mới', 'New file')} onClick={props.onNewFile}>＋</button>
          <button title={t('Làm mới', 'Refresh')} aria-label={t('Làm mới workspace', 'Refresh workspace')} onClick={props.onRefresh}>⟳</button>
          {props.selected && <button title={props.missingVersion ? t('Cần phiên bản file để xóa an toàn', 'A file version is required for safer deletion') : t('Xóa file', 'Delete file')} aria-label={t('Xóa file', 'Delete file')} disabled={props.missingVersion || props.locked || props.saving || props.deleting} onClick={props.onDelete}>🗑</button>}
          <button className="files-close" title={t('Đóng panel Workspace', 'Close Workspace panel')} aria-label={t('Đóng panel Workspace', 'Close Workspace panel')} onClick={props.onClose}>✕</button>
        </div>
      </div>
      <HookReview key={props.sid} sid={props.sid} isAdmin={props.isAdmin} />
      <div className="files-tree">
        {props.tree.length === 0
          ? <div className="empty">{t('Workspace trống.', 'Workspace empty.')}<br />{t('Nhờ agent tạo file nhé!', 'Ask the agent to create a file!')}</div>
          : props.tree.map((n) => (
            <TreeRow key={n.name} node={n} depth={0} path={n.name} selected={props.selected} onOpen={props.onOpen} />
          ))}
      </div>
      {props.selected !== null && (
        <div className="editor">
          <div className="editor-head">
            <code>{props.selected}</code>
            <button
              className={`btn-save ${props.dirty ? 'dirty' : ''}`}
              onClick={props.onSave}
              disabled={props.binary || props.mixedNewlines || props.missingVersion || !props.dirty || props.saving || props.deleting || props.locked}
            >{props.binary || props.mixedNewlines || props.missingVersion ? t('Chỉ đọc', 'Read only') : props.locked ? t('Vui lòng chờ…', 'Please wait…') : props.saving ? t('Đang lưu…', 'Saving…') : props.dirty ? t('● Lưu', '● Save') : t('Đã lưu', 'Saved')}</button>
          </div>
          {props.deleting && <div role="status">{t('Đang xóa file; tạm thời không thể lưu hoặc xóa file khác.', 'Deletion in progress; saving and other deletions are temporarily unavailable.')}</div>}
          {props.mixedNewlines && <div role="status">{t('File có kiểu xuống dòng pha trộn; chỉ xem để tránh thay đổi dữ liệu.', 'Mixed line endings: read-only to avoid changing file data.')}</div>}
          {props.deleted ? <div role="status">{t('File đã bị xóa; hãy sao chép bản sửa trước khi rời editor.', 'File was deleted; copy any draft before leaving the editor.')}</div> : props.missingVersion && <div role="status">{t('Không có phiên bản file hợp lệ; chỉ xem để tránh ghi đè thay đổi. Hãy kiểm tra server và mở lại.', 'No valid file version is available; read-only to avoid overwriting changes. Check the server and reopen.')}</div>}
          <textarea
            className="editor-area"
            aria-label={t(`Nội dung ${props.selected}`, `Contents of ${props.selected}`)}
            spellCheck={false}
            readOnly={props.binary || props.mixedNewlines || props.missingVersion || props.locked}
            value={props.content ?? ''}
            onChange={(e) => { if (!props.binary && !props.mixedNewlines && !props.missingVersion && !props.locked) props.onChange(e.target.value) }}
            placeholder={t('(file trống)', '(empty file)')}
          />
        </div>
      )}
      <Terminal sid={props.sid} />
    </aside>
  )
}
