import { useState, type FormEvent } from 'react'
import { api } from '../lib/api'
import { tc } from '../lib/format'
import type { Chapter } from '../types/api'
import { Card } from './Bits'

/**
 * 章节列表：标在剪辑前的时间上，可改名、删除、点时间跳转试听。
 * 导出成片时后端按剪辑快照换算并写入 MP3 / M4A。
 */
export function ChapterPanel({
  sessionId,
  chapters,
  onChange,
  totalMs,
  disabled,
  currentMs,
  onSeek,
}: {
  sessionId: string
  chapters: Chapter[]
  onChange: (chapters: Chapter[]) => void
  totalMs: number
  disabled: boolean
  /** 读取剪辑前播放器的当前位置（毫秒） */
  currentMs: () => number
  onSeek: (ms: number) => void
}) {
  const [title, setTitle] = useState('')
  const [drafts, setDrafts] = useState<Record<string, string>>({})
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)

  const run = async (operation: () => Promise<void>) => {
    setSaving(true); setError(null)
    try { await operation() } catch (e) { setError(e instanceof Error ? e.message : '章节保存失败，请重试') } finally { setSaving(false) }
  }

  const sorted = (items: Chapter[]) => [...items].sort((a, b) => a.startMs - b.startMs)

  const addAtPlayhead = (event: FormEvent) => {
    event.preventDefault()
    void run(async () => {
      const startMs = Math.min(Math.max(0, Math.round(currentMs())), Math.max(0, totalMs - 1))
      const created = await api.createChapter(sessionId, startMs, title.trim() || `第 ${chapters.length + 1} 章`)
      onChange(sorted([...chapters, created]))
      setTitle('')
    })
  }

  const rename = (chapter: Chapter) => {
    const next = (drafts[chapter.id] ?? chapter.title).trim()
    setDrafts(({ [chapter.id]: _, ...rest }) => rest)
    if (!next || next === chapter.title) return
    void run(async () => {
      const updated = await api.updateChapter(sessionId, chapter.id, { title: next })
      onChange(chapters.map((c) => (c.id === updated.id ? updated : c)))
    })
  }

  const remove = (chapter: Chapter) => void run(async () => {
    await api.deleteChapter(sessionId, chapter.id)
    onChange(chapters.filter((c) => c.id !== chapter.id))
  })

  const busy = disabled || saving
  return (
    <Card id="review-chapters" title={`章节${chapters.length ? ` · ${chapters.length}` : ''}`}
      hint="章节标在剪辑前的时间上；导出成片时自动换算并写入 MP3 / M4A，也可复制章节列表贴到节目简介。">
      {chapters.length === 0 && <p className="card__hint">还没有章节。在转写中选中一句后点“设为章节”，或播放到某处后在下方添加。</p>}
      {chapters.length > 0 && (
        <ol className="chapter-list">
          {chapters.map((chapter) => (
            <li key={chapter.id} className="chapter-row">
              <button type="button" className="chapter-row__time mono" title="从这里试听" onClick={() => onSeek(chapter.startMs)}>{tc(chapter.startMs)}</button>
              <input aria-label={`章节标题 ${tc(chapter.startMs)}`} value={drafts[chapter.id] ?? chapter.title} maxLength={100} disabled={busy}
                onChange={(event) => setDrafts((d) => ({ ...d, [chapter.id]: event.target.value }))}
                onBlur={() => rename(chapter)}
                onKeyDown={(event) => { if (event.key === 'Enter') event.currentTarget.blur() }} />
              <button type="button" className="iconbtn" aria-label={`删除章节 ${chapter.title}`} title="删除章节" disabled={busy} onClick={() => remove(chapter)}>✕</button>
            </li>
          ))}
        </ol>
      )}
      <form className="chapter-add" onSubmit={addAtPlayhead}>
        <input aria-label="新章节标题" placeholder="章节标题（可留空）" value={title} maxLength={100} disabled={busy} onChange={(event) => setTitle(event.target.value)} />
        <button className="btn" type="submit" disabled={busy || totalMs <= 0}>在播放位置添加</button>
      </form>
      {error && <p className="card__hint" role="alert" style={{ color: 'var(--sig-danger)' }}>{error}</p>}
    </Card>
  )
}
