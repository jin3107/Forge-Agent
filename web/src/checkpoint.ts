import type { CheckpointStatus } from './types'
import type { Translate } from './i18n'

/** Saving can succeed even when a checkpoint is disabled or failed. */
export function saveCheckpointNotice(t: Translate): string {
  return t('Đã lưu và tạo checkpoint; undo có thể không khôi phục file bị loại trừ hoặc Git bỏ qua.',
           'Saved with a checkpoint; undo may not restore excluded or Git-ignored files.')
}

export function saveCheckpointWarning(status: CheckpointStatus | undefined, t: Translate): string | null {
  if (status === 'created') return null
  const reason = status === 'disabled'
    ? t('checkpoint đang tắt', 'checkpoint is disabled')
    : status === 'unavailable'
      ? t('không có Git', 'Git is unavailable')
      : status === 'failed'
        ? t('tạo checkpoint thất bại', 'checkpoint creation failed')
        : t('không xác nhận được checkpoint', 'checkpoint status was not confirmed')
  return t(`Đã lưu file nhưng ${reason}. Undo có thể không khôi phục trạng thái trước khi lưu.`,
           `File saved, but ${reason}. Undo may not restore the state before saving.`)
}

export function deleteCheckpointNotice(t: Translate): string {
  return t('Đã xóa và tạo checkpoint; undo có thể không khôi phục file bị loại trừ hoặc Git bỏ qua.',
           'Deleted with a checkpoint; undo may not restore excluded or Git-ignored files.')
}

export function deleteCheckpointWarning(status: CheckpointStatus | undefined, t: Translate): string | null {
  if (status === 'created') return null
  const reason = status === 'disabled'
    ? t('checkpoint đang tắt', 'checkpoint is disabled')
    : status === 'unavailable'
      ? t('không có Git', 'Git is unavailable')
      : status === 'failed'
        ? t('tạo checkpoint thất bại', 'checkpoint creation failed')
        : t('không xác nhận được checkpoint', 'checkpoint status was not confirmed')
  return t(`Đã xóa file nhưng ${reason}. Undo có thể không khôi phục trạng thái trước khi xóa.`,
           `File deleted, but ${reason}. Undo may not restore the state before deletion.`)
}

export function uploadCheckpointNotice(t: Translate): string {
  return t('Đã tải lên và tạo checkpoint; undo có thể không khôi phục file bị loại trừ hoặc Git bỏ qua.',
           'Uploaded with a checkpoint; undo may not restore excluded or Git-ignored files.')
}

export function uploadCheckpointWarning(status: CheckpointStatus | undefined, t: Translate): string | null {
  if (status === 'created') return null
  const reason = status === 'disabled'
    ? t('checkpoint đang tắt', 'checkpoint is disabled')
    : status === 'unavailable'
      ? t('không có Git', 'Git is unavailable')
      : status === 'failed'
        ? t('tạo checkpoint thất bại', 'checkpoint creation failed')
        : t('không xác nhận được checkpoint', 'checkpoint status was not confirmed')
  return t(`Đã tải file lên nhưng ${reason}. Undo có thể không khôi phục trạng thái trước khi tải lên.`,
           `Files uploaded, but ${reason}. Undo may not restore the state before upload.`)
}

export function createCheckpointNotice(t: Translate): string {
  return t('Đã tạo file và checkpoint; undo có thể không khôi phục file bị loại trừ hoặc Git bỏ qua.',
           'File created with a checkpoint; undo may not restore excluded or Git-ignored files.')
}

export function createCheckpointWarning(status: CheckpointStatus | undefined, t: Translate): string | null {
  if (status === 'created') return null
  const reason = status === 'disabled'
    ? t('checkpoint đang tắt', 'checkpoint is disabled')
    : status === 'unavailable'
      ? t('không có Git', 'Git is unavailable')
      : status === 'failed'
        ? t('tạo checkpoint thất bại', 'checkpoint creation failed')
        : t('không xác nhận được checkpoint', 'checkpoint status was not confirmed')
  return t(`Đã tạo file nhưng ${reason}. Undo có thể không khôi phục trạng thái trước khi tạo.`,
           `File created, but ${reason}. Undo may not restore the state before creation.`)
}
