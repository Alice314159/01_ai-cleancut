import { useEffect, useRef, useState } from 'react'
import { api } from '../lib/api'
import { tc } from '../lib/format'
import type { EditDecision } from '../types/api'

export type RetakeTarget = {
  startMs: number; endMs: number; text: string; sourceId: string; decisions: EditDecision[]
}

export function RetakeDialog({ sessionId, target, onClose, onSaved }: {
  sessionId: string; target: RetakeTarget; onClose: () => void; onSaved: () => Promise<void>
}) {
  const dialog = useRef<HTMLDialogElement>(null)
  const recorder = useRef<MediaRecorder | null>(null)
  const stream = useRef<MediaStream | null>(null)
  const alive = useRef(false)
  const attempt = useRef(0)
  const [status, setStatus] = useState<'idle' | 'requesting' | 'recording' | 'stopping'>('idle')
  const [blob, setBlob] = useState<Blob | null>(null)
  const [url, setUrl] = useState('')
  const [seconds, setSeconds] = useState(0)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [decisions, setDecisions] = useState(target.decisions)

  useEffect(() => {
    alive.current = true
    dialog.current?.showModal()
    return () => {
      alive.current = false
      attempt.current += 1
      if (recorder.current) {
        recorder.current.onstop = null
        recorder.current.ondataavailable = null
        recorder.current.onerror = null
        if (recorder.current.state !== 'inactive') recorder.current.stop()
      }
      stream.current?.getTracks().forEach((track) => track.stop())
    }
  }, [])

  useEffect(() => {
    if (!blob) { setUrl(''); return }
    const objectUrl = URL.createObjectURL(blob)
    setUrl(objectUrl)
    return () => URL.revokeObjectURL(objectUrl)
  }, [blob])

  const stop = () => {
    if (recorder.current?.state === 'recording') {
      setStatus('stopping')
      recorder.current.stop()
    }
  }

  useEffect(() => {
    if (status !== 'recording') return
    const started = Date.now()
    const timer = window.setInterval(() => {
      const elapsed = (Date.now() - started) / 1000
      setSeconds(Math.floor(elapsed))
      if (elapsed >= 300) stop()
    }, 200)
    return () => window.clearInterval(timer)
  }, [status])

  const start = async () => {
    dialog.current?.querySelectorAll('audio').forEach((audio) => audio.pause())
    const request = ++attempt.current
    setError(''); setStatus('requesting')
    let media: MediaStream | null = null
    try {
      if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === 'undefined') {
        throw new Error('当前浏览器无法录音，请使用支持麦克风的浏览器，并通过 localhost 或 HTTPS 打开。')
      }
      media = await navigator.mediaDevices.getUserMedia({ audio: true })
      if (!alive.current || request !== attempt.current) { media.getTracks().forEach((track) => track.stop()); return }
      stream.current = media
      const mimeType = ['audio/webm;codecs=opus', 'audio/mp4', 'audio/webm'].find((type) => MediaRecorder.isTypeSupported(type))
      const active = new MediaRecorder(media, mimeType ? { mimeType } : undefined)
      recorder.current = active
      const chunks: Blob[] = []
      let failed = false
      let bytes = 0
      active.ondataavailable = (event) => {
        bytes += event.data.size
        if (bytes > 60 * 1024 * 1024) {
          failed = true
          if (alive.current) setError('录音超过 60 MB，请缩短片段后重试。')
          if (active.state !== 'inactive') active.stop()
        } else if (event.data.size) chunks.push(event.data)
      }
      active.onerror = () => {
        failed = true
        media?.getTracks().forEach((track) => track.stop())
        if (alive.current) { setError('录音设备发生错误，请重新录制。'); setStatus('idle') }
      }
      active.onstop = () => {
        media?.getTracks().forEach((track) => track.stop())
        if (!alive.current) return
        setStatus('idle')
        if (failed) return
        const take = new Blob(chunks, { type: active.mimeType || mimeType || 'audio/webm' })
        if (take.size) setBlob(take)
        else setError('没有录到音频，请检查麦克风后重试。')
      }
      media.getAudioTracks().forEach((track) => track.addEventListener('ended', () => {
        if (active.state !== 'inactive') {
          failed = true
          if (alive.current) setError('麦克风已断开，请重新录制。')
          active.stop()
        }
      }))
      active.start(250)
      setBlob(null); setSeconds(0); setStatus('recording')
    } catch (e) {
      media?.getTracks().forEach((track) => track.stop())
      if (!alive.current) return
      setStatus('idle')
      setError(e instanceof DOMException && e.name === 'NotAllowedError' ? '麦克风权限未开启，请允许麦克风后重试。' : e instanceof Error ? e.message : '无法启动录音')
    }
  }

  const save = async () => {
    if (!blob) return
    setSaving(true); setError('')
    try {
      await api.saveRetake(sessionId, blob, target.startMs, target.endMs, target.sourceId, decisions)
    } catch (e) {
      if (alive.current) { setError(e instanceof Error ? e.message : '重录保存失败'); setSaving(false) }
      return
    }
    // A successful save must not be retried if the subsequent page refresh fails.
    try { await onSaved() } finally { onClose() }
  }

  const refresh = async () => {
    setSaving(true)
    try { setDecisions(await api.getDecisions(sessionId)); setError('已刷新剪辑选择，请核对下方提示后重新应用。') }
    catch (e) { setError(e instanceof Error ? e.message : '刷新失败') }
    finally { if (alive.current) setSaving(false) }
  }
  const overlap = decisions.filter((d) => d.accepted !== false && d.startMs < target.endMs && target.startMs < d.endMs)
  const active = status !== 'idle'

  return <dialog ref={dialog} className="retake-dialog" aria-labelledby="retake-title"
    onCancel={(event) => { event.preventDefault(); if (!saving) onClose() }}>
    <h2 id="retake-title">重录选中片段</h2>
    <p>替换范围：{tc(target.startMs)} – {tc(target.endMs)}（{((target.endMs - target.startMs) / 1000).toFixed(3)} 秒）</p>
    {target.text && <blockquote>{target.text}</blockquote>}
    <p className="card__hint">录好后先试听，再应用到剪辑。新录音可长可短，单次最多 5 分钟。原音频保留，生成成片时才会替换。</p>
    {overlap.length > 0 && <p role="status">此范围涉及 {overlap.length} 条已有剪辑。应用重录会将范围内的旧剪辑改为拒绝；部分重叠或保护区间需先处理。</p>}
    {error && <p role="alert">{error}</p>}
    <p role="status">{status === 'requesting' ? '等待麦克风权限…' : status === 'recording' ? `正在重录 · ${seconds} 秒` : status === 'stopping' ? '正在整理录音…' : blob ? '录音已就绪，请试听确认。' : '准备好后开始重录。'}</p>
    {url && <audio aria-label="新录音试听" controls src={url} style={{ width: '100%' }} />}
    <div className="retake-dialog__actions">
      <button className="btn" disabled={active || saving} onClick={() => void start()}>{blob ? '重新录一遍' : '开始重录'}</button>
      {status === 'recording' && <button className="btn" onClick={stop}>停止重录</button>}
      <button className="btn btn--primary" disabled={!blob || active || saving} onClick={() => void save()}>{saving ? '处理中…' : '应用重录替换'}</button>
      <button className="btn" disabled={saving} onClick={onClose}>取消重录</button>
      {error && blob && <button className="btn" disabled={saving || active} onClick={() => void refresh()}>刷新剪辑选择</button>}
    </div>
    <p className="card__hint">左侧文字仍对应原录音。应用重录并生成成片后，会自动识别成片的新文字和字幕。</p>
  </dialog>
}
