import { useRef, useState } from 'react'
import { api, type SessionDetail } from '../lib/api'

/** 节目封面：上传 / 更换 / 移除。导出 MP3 / M4A 时写入文件。 */
export function CoverPicker({
  session,
  onChange,
  disabled,
}: {
  session: SessionDetail
  onChange: (session: SessionDetail) => void
  disabled: boolean
}) {
  const inputRef = useRef<HTMLInputElement | null>(null)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const run = async (operation: () => Promise<SessionDetail>) => {
    setSaving(true); setError(null)
    try { onChange(await operation()) } catch (e) { setError(e instanceof Error ? e.message : '封面保存失败，请重试') } finally { setSaving(false) }
  }

  const busy = disabled || saving
  const version = session.coverVersion
  return (
    <div className="cover-picker">
      <div className="cover-picker__thumb" aria-hidden={!version}>
        {version ? <img src={api.coverUrl(session.id, version)} alt="节目封面" /> : <span>无封面</span>}
      </div>
      <div className="cover-picker__body">
        <div className="export-group__row">
          <button className="btn" type="button" disabled={busy} onClick={() => inputRef.current?.click()}>
            {saving ? '处理中…' : version ? '更换封面' : '上传封面'}
          </button>
          {version && <button className="btn btn--link" type="button" disabled={busy} onClick={() => void run(() => api.deleteCover(session.id))}>移除</button>}
        </div>
        <p className="card__hint">JPEG / PNG / WebP，5MB 以内；建议正方形。会写入导出的 MP3 / M4A。</p>
        {error && <p className="card__hint" role="alert" style={{ color: 'var(--sig-danger)' }}>{error}</p>}
      </div>
      <input ref={inputRef} type="file" accept="image/jpeg,image/png,image/webp" hidden aria-label="选择封面图片"
        onChange={(event) => {
          const file = event.target.files?.[0]
          event.target.value = ''
          if (file) void run(() => api.uploadCover(session.id, file))
        }} />
    </div>
  )
}
