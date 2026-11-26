import { useEffect, useState } from 'react'
import { api } from '../lib/api'
import { speakerName } from '../lib/format'
import type { AudioQualityReport } from '../types/api'
import { Card } from './Bits'

/**
 * 说话人：分析状态、起名字、重新分析。手动更正某段是谁说的在转写选中栏里（“说话人”按钮）。
 * 没装模型且从未分析过时不显示。
 */
export function SpeakerPanel({
  sessionId,
  report,
  onChange,
  disabled,
}: {
  sessionId: string
  report: AudioQualityReport | null
  onChange: (report: AudioQualityReport) => void
  disabled: boolean
}) {
  const [drafts, setDrafts] = useState<Record<string, string>>({})
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const names = report?.speakerNames ?? {}
  const namesKey = JSON.stringify(names)
  useEffect(() => { setDrafts(JSON.parse(namesKey) as Record<string, string>) }, [namesKey])

  if (!report) return null
  const status = report.speakerAnalysisStatus ?? 'unavailable'
  if (status === 'unavailable' && !report.speakerModelAvailable) return null

  const run = async (operation: () => Promise<AudioQualityReport>) => {
    setSaving(true); setError(null)
    try { onChange(await operation()) } catch (e) { setError(e instanceof Error ? e.message : '保存失败，请重试') } finally { setSaving(false) }
  }
  const saveName = (speaker: number) => {
    const key = String(speaker)
    const value = (drafts[key] ?? '').trim()
    if (value === (names[key] ?? '')) return
    void run(() => api.renameSpeakers(sessionId, { ...names, [key]: value }))
  }
  const reanalyze = () => {
    if (status === 'done' && !window.confirm('重新分析会覆盖手动更正过的说话人（名字按编号保留，可能需要重新对应）。继续？')) return
    void run(() => api.reanalyzeSpeakers(sessionId))
  }

  const speakers = report.speakers ?? []
  const busy = disabled || saving
  const running = status === 'pending' || status === 'running'
  return (
    <Card title="说话人" hint="本机声纹模型区分谁在说话；多人时转写和导出的字幕会标出名字。">
      {running && <p className="card__hint" role="status">正在区分说话人（约为录音时长的 1/8），完成后转写会自动标出…</p>}
      {status === 'failed' && <p className="card__hint" role="alert" style={{ color: 'var(--sig-danger)' }}>说话人区分失败，可以重试。</p>}
      {status === 'unavailable' && <p className="card__hint">这段录音还没有区分说话人。</p>}
      {status === 'done' && speakers.length === 0 && <p className="card__hint">录音较长，只估计了人数（{report.speakerCount ?? 0} 位），转写中不标注。</p>}
      {status === 'done' && speakers.length === 1 && <p className="card__hint">只检测到 1 位说话人，转写中不标注。若有误，可在转写中选中文字，点“说话人”改为新说话人。</p>}
      {status === 'done' && speakers.length > 1 && (
        <>
          <ul className="speaker-list">
            {speakers.map((speaker) => (
              <li key={speaker} className="speaker-list__row">
                <span className={`speaker-tag speaker-tag--${speaker % 6}`}>{speakerName(speaker)}</span>
                <input className="input" value={drafts[String(speaker)] ?? ''} maxLength={20} placeholder="起个名字，如 主持人"
                  aria-label={`${speakerName(speaker)} 的名字`} disabled={busy}
                  onChange={(event) => setDrafts((d) => ({ ...d, [String(speaker)]: event.target.value }))}
                  onBlur={() => saveName(speaker)}
                  onKeyDown={(event) => { if (event.key === 'Enter') (event.target as HTMLInputElement).blur() }} />
              </li>
            ))}
          </ul>
          <p className="card__hint">名字会用于转写标签和导出的 TXT / SRT。认错了人：在转写中选中文字，点选中栏里的“说话人”改正。</p>
        </>
      )}
      {!running && report.speakerModelAvailable && (
        <button className="btn" type="button" disabled={busy} onClick={reanalyze}>
          {status === 'done' ? '重新分析' : status === 'failed' ? '重试' : '开始区分说话人'}
        </button>
      )}
      {error && <p className="card__hint" role="alert" style={{ color: 'var(--sig-danger)' }}>{error}</p>}
    </Card>
  )
}
