import { useEffect, useState, type RefObject } from 'react'
import { saveFile } from '../lib/saveFile'
import { api } from '../lib/api'
import type { RenderTranscript } from '../types/api'
import { tc } from '../lib/format'

export function RenderTranscriptPanel({ sessionId, versionId, audioRef, stale }: {
  sessionId: string; versionId: string; audioRef: RefObject<HTMLAudioElement>; stale: boolean
}) {
  const [data, setData] = useState<RenderTranscript | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [reload, setReload] = useState(0)
  const [language, setLanguage] = useState<'auto' | 'zh' | 'en'>('auto')
  const running = !!data?.job && ['QUEUED', 'LOADING_MODEL', 'TRANSCRIBING'].includes(data.job.status)
  useEffect(() => {
    let cancelled = false
    let timer: number | undefined
    const poll = async () => {
      try {
        const result = await api.getRenderTranscript(sessionId, versionId)
        if (cancelled) return
        setData(result); setError('')
        if (!result.job || !['QUEUED', 'LOADING_MODEL', 'TRANSCRIBING'].includes(result.job.status)) return
      } catch (e) {
        if (cancelled) return
        setError(e instanceof Error ? e.message : '成片转写状态读取失败，正在重试')
      }
      timer = window.setTimeout(poll, 1000)
    }
    void poll()
    return () => { cancelled = true; window.clearTimeout(timer) }
  }, [sessionId, versionId, reload])

  const start = async () => {
    setBusy(true); setError('')
    try {
      const job = await api.transcribeRender(sessionId, versionId, language)
      setData((previous) => ({ versionId, tokens: previous?.tokens ?? [], transcriptJobId: previous?.transcriptJobId ?? null, job }))
      setReload((value) => value + 1)
    } catch (e) { setError(e instanceof Error ? e.message : '成片转写启动失败') }
    finally { setBusy(false) }
  }
  const download = async (format: 'txt' | 'srt') => {
    setBusy(true); setError('')
    try {
      const blob = await api.downloadRenderSubtitles(sessionId, versionId, format)
      await saveFile(blob, `transcript-master.${format}`)
    } catch (e) { setError(e instanceof Error ? e.message : '成片文字导出失败') }
    finally { setBusy(false) }
  }
  const status = data?.job?.status
  return <section className="master-transcript" aria-label="成片转写">
    <h3>{stale ? '上一次成片转写' : '成片转写'}</h3>
    <p className="card__hint">重新识别这版成片的实际语音（含重录），文字和字幕对应成片时间轴；左侧原文仍用于原始选段。</p>
    <p role="status">{!data ? '读取成片转写状态…' : status === 'QUEUED' ? '成片转写排队中…' : status === 'LOADING_MODEL' ? '正在加载成片转写模型…' : status === 'TRANSCRIBING' ? `成片转写 ${Math.round((data.job?.progress ?? 0) * 100)}%` : status === 'FAILED' ? data.job?.errorMessage : status === 'COMPLETED' ? data.tokens.length ? '成片转写完成，字幕可下载。' : '成片转写完成，未识别到语音。' : '尚未转写这版成片，可在此开始。'}</p>
    {error && <p role="alert">{error}</p>}
    {data?.tokens.length && data.transcriptJobId !== data.job?.id ? <p className="card__hint">当前显示这版成片上一次成功的转写，重试成功后更新。</p> : null}
    <div className="master-transcript__actions">
      <select aria-label="成片识别语言" value={language} disabled={running || busy} onChange={(event) => setLanguage(event.target.value as typeof language)}>
        <option value="auto">自动识别</option><option value="zh">中文</option><option value="en">英文</option>
      </select>
      <button className="btn" disabled={!data || running || busy} onClick={() => void start()}>{status === 'FAILED' ? '重试成片转写' : data?.job ? '重新转写成片' : '开始成片转写'}</button>
      <button className="btn" disabled={!data?.tokens.length || busy} onClick={() => void download('srt')}>{stale ? '导出上一次成片 SRT' : '导出成片 SRT'}</button>
      <button className="btn btn--link" disabled={!data?.tokens.length || busy} onClick={() => void download('txt')}>导出成片 TXT</button>
    </div>
    {!!data?.tokens.length && <details><summary>查看成片文字 · {data.tokens.length} 个词</summary>
      <div className="master-transcript__text">{data.tokens.map((token, index) => <button key={index} title={`${tc(token.startMs)} – ${tc(token.endMs)}`} onClick={() => {
        const audio = audioRef.current
        if (!audio) return
        audio.currentTime = token.startMs / 1000
        void audio.play().catch(() => setError('请点击成片播放器开始试听'))
      }}>{token.text}</button>)}</div>
    </details>}
  </section>
}
