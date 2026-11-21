export const EDITOR_MAX_UTF8_BYTES = 4 * 1024 * 1024

/** Check the bytes that will actually be sent, after line-ending restoration. */
export function fitsEditorSaveLimit(content: string): boolean {
  return content.length <= EDITOR_MAX_UTF8_BYTES && new TextEncoder().encode(content).byteLength <= EDITOR_MAX_UTF8_BYTES
}

export function validFileVersion(version: unknown): version is string {
  return typeof version === 'string' && /^[0-9a-f]{64}$/.test(version)
}

/** Binary previews contain a placeholder, not file contents; never save it. */
export function canSaveWorkspaceFile(sid: string | null, path: string | null, content: string | null, binary: boolean): boolean {
  return !!sid && path !== null && content !== null && !binary
}

export function localizeUploadSkip(item: string, t: (vi: string, en: string) => string): string {
  const reasons: [string, string][] = [
    [' (already exists)', ' (đã tồn tại)'],
    [' (over 20-file limit)', ' (vượt giới hạn 20 file)'],
  ]
  for (const [english, vietnamese] of reasons) {
    if (item.endsWith(english)) return item.slice(0, -english.length) + t(vietnamese, english)
  }
  return item
}

export type LineEndingStyle = 'lf' | 'crlf' | 'cr' | 'mixed'

/** Textareas normalize CRLF and CR to LF; keep the original uniform style. */
export function inspectLineEndings(text: string): {style: LineEndingStyle; normalized: string} {
  let mask = 0
  for (let i = 0; i < text.length; i++) {
    const c = text.charCodeAt(i)
    if (c === 13) {
      if (text.charCodeAt(i + 1) === 10) { mask |= 2; i++ }
      else mask |= 4
    } else if (c === 10) mask |= 1
    if (mask !== 0 && (mask & (mask - 1)) !== 0) break
  }
  const style: LineEndingStyle = mask === 2 ? 'crlf' : mask === 4 ? 'cr' : mask <= 1 ? 'lf' : 'mixed'
  return {style, normalized: text.replace(/\r\n|\r/g, '\n')}
}

export function restoreLineEndings(text: string, style: LineEndingStyle): string {
  if (style === 'mixed') throw new Error('Cannot safely save mixed line endings')
  const normalized = text.replace(/\r\n|\r/g, '\n')
  return style === 'crlf' ? normalized.replace(/\n/g, '\r\n')
    : style === 'cr' ? normalized.replace(/\n/g, '\r') : normalized
}
