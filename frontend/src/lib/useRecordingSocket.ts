import { useCallback, useEffect, useRef, useState } from 'react'
import { api, WS_BASE } from './api'
import { ChunkQueue, encodeFrame } from './chunkQueue'
import { chunkStore } from './chunkStore'
import { toDbfs } from './format'
import type { QualityEvent } from '../types/api'

export type CaptureState = 'IDLE' | 'RECORDING' | 'PAUSED' | 'RECONNECTING' | 'STOPPED'
type StageMsg = { type: 'stage'; stage: string; status: string }
type QualityEventMsg = { type: 'quality_event' } & Pick<QualityEvent, 'id' | 'eventType' | 'severity' | 'clientTimestampMs' | 'message'>

/** MediaRecorder 每 1 秒产出一片，待上传片数约等于未上传的秒数。 */
const TIMESLICE_MS = 1000
/** 停止时等待补传完成的上限；超时则保留在本机，下次打开会话再补传。 */
const DRAIN_TIMEOUT_MS = 20000
const MAX_BACKOFF_MS = 10000

function pickMimeType(): string {
  return ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus', 'audio/mp4']
    .find((mime) => MediaRecorder.isTypeSupported(mime)) ?? ''
}

const sleep = (ms: number) => new Promise((resolve) => window.setTimeout(resolve, ms))

/**
 * 录音采集与上传，支持断线续传：
 * - 分片带序号上传，服务端确认后才从队列与本机存储中移除；
 * - 断线后继续在本机录音并自动重连（指数退避），重连后从服务端给的位置补传；
 * - 停止时先等补传完成；离线停止或页面被关闭时，未上传部分留在本机，重新打开此会话会自动补传并结束。
 */
export function useRecordingSocket(sessionId: string | null) {
  const [captureState, setCaptureState] = useState<CaptureState>('IDLE')
  const [connError, setConnError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [pendingChunks, setPendingChunks] = useState(0)
  const [rmsDbfs, setRmsDbfs] = useState(-60)
  const [peakDbfs, setPeakDbfs] = useState(-60)
  const [committedDurationMs, setCommittedDurationMs] = useState(0)
  const [liveEnvelope, setLiveEnvelope] = useState<number[]>([])
  const [qualityEvents, setQualityEvents] = useState<QualityEventMsg[]>([])
  const [stage, setStage] = useState<StageMsg | null>(null)

  const wsRef = useRef<WebSocket | null>(null)
  const queueRef = useRef(new ChunkQueue())
  const recorderRef = useRef<MediaRecorder | null>(null)
  const streamRef = useRef<MediaStream | null>(null)
  const convertRef = useRef<Promise<void>>(Promise.resolve())
  const stoppingRef = useRef<Promise<void> | null>(null)
  const startingRef = useRef(false)
  const generationRef = useRef(0)
  /** 会话处于可续传的录制中（含本页恢复补传）：断线时重连而不是结束。 */
  const resumableRef = useRef(false)
  /** 断线前的录制状态，重连时告诉服务端。 */
  const wantedStateRef = useRef<'RECORDING' | 'PAUSED'>('RECORDING')
  const reconnectTimerRef = useRef<number | undefined>(undefined)
  const attemptRef = useRef(0)
  const deadRef = useRef(false)

  const syncPending = useCallback(() => setPendingChunks(queueRef.current.size), [])

  const sendChunks = useCallback((ws: WebSocket, chunks: readonly { seq: number; data: ArrayBuffer }[]) => {
    for (const chunk of chunks) {
      if (ws.readyState !== WebSocket.OPEN) return
      ws.send(encodeFrame(chunk.seq, chunk.data))
    }
  }, [])

  // eslint 风格的前置声明：connect 与 scheduleReconnect 互相引用，用 ref 打破环。
  const scheduleReconnectRef = useRef<() => void>(() => undefined)

  const connect = useCallback((resume: boolean): Promise<WebSocket> => {
    if (!sessionId) return Promise.reject(new Error('没有可用的会话'))
    const ws = new WebSocket(`${WS_BASE}/ws/recording/${sessionId}${resume ? '?resume=1' : ''}`)
    wsRef.current = ws
    ws.binaryType = 'arraybuffer'
    return new Promise<WebSocket>((resolve, reject) => {
      let opened = false
      const timeout = window.setTimeout(() => { reject(new Error('录制连接超时')); ws.close() }, 10000)
      ws.onopen = () => {
        opened = true
        window.clearTimeout(timeout)
        if (resume) ws.send(JSON.stringify({ type: 'control', action: 'reconnect', state: wantedStateRef.current }))
        resolve(ws)
      }
      ws.onerror = () => { window.clearTimeout(timeout); reject(new Error('WebSocket 连接失败')) }
      ws.onclose = (event) => {
        window.clearTimeout(timeout)
        reject(new Error('录制连接已关闭'))
        if (wsRef.current !== ws) return
        wsRef.current = null
        // 4404/4409：会话不存在或已经结束（例如等待重连超时），无法再续传
        if (event.code === 4404 || event.code === 4409) {
          if (resumableRef.current) {
            deadRef.current = true
            resumableRef.current = false
            const lost = queueRef.current.size
            setConnError(lost
              ? `网络中断太久，服务端已按收到的内容结束录音，约 ${lost} 秒未能上传。`
              : '录制已在服务端结束。')
            if (sessionId) void chunkStore.clear(sessionId)
            setCaptureState('STOPPED')
            streamRef.current?.getTracks().forEach((track) => track.stop())
          }
          return
        }
        if (resumableRef.current && !deadRef.current) {
          if (opened) setCaptureState('RECONNECTING')
          scheduleReconnectRef.current()
        } else if (opened && recorderRef.current && !stoppingRef.current) {
          setConnError('录制连接中断，已停止采集并保存已收到的音频。')
          void finishRef.current()
        }
      }
      ws.onmessage = (event) => {
        let msg
        try { msg = JSON.parse(event.data) } catch { return }
        switch (msg.type) {
          case 'ack': {
            queueRef.current.ack(msg.seq)
            if (sessionId) void chunkStore.removeUpTo(sessionId, msg.seq)
            syncPending()
            break
          }
          case 'resume_from':
            attemptRef.current = 0
            setNotice(null)
            if (!stoppingRef.current) setCaptureState(wantedStateRef.current)
            sendChunks(ws, queueRef.current.resendFrom(msg.nextSeq))
            syncPending()
            break
          case 'resync':
            sendChunks(ws, queueRef.current.resendFrom(msg.nextSeq))
            break
          case 'level':
            setRmsDbfs(toDbfs(msg.rms)); setPeakDbfs(toDbfs(msg.peak))
            setCommittedDurationMs(msg.committedDurationMs)
            setLiveEnvelope((prev) => [...prev, Math.max(0.06, Math.min(1, msg.peak))])
            break
          case 'quality_event': setQualityEvents((prev) => [...prev, msg]); break
          case 'stage': setStage(msg); break
          case 'error':
            if (msg.code === 'not_resumable') { ws.close(); break }
            setConnError(`录制请求未被接受：${msg.code}`)
            void finishRef.current()
            break
        }
      }
    })
  }, [sessionId, sendChunks, syncPending])

  scheduleReconnectRef.current = () => {
    window.clearTimeout(reconnectTimerRef.current)
    const attempt = attemptRef.current++
    const delay = Math.min(MAX_BACKOFF_MS, 1000 * 2 ** attempt)
    setNotice(`网络中断，正在重连（${Math.round(delay / 1000)} 秒后重试）…录音仍在本机继续，恢复后自动补传。`)
    reconnectTimerRef.current = window.setTimeout(() => {
      if (!resumableRef.current || deadRef.current) return
      connect(true).catch(() => undefined) // 失败由 onclose 再次安排重连
    }, delay)
  }

  /** 等待队列清空（全部被服务端确认）；断线时期间会自动重连。 */
  const drain = useCallback(async (timeoutMs: number) => {
    const deadline = Date.now() + timeoutMs
    while (queueRef.current.size > 0 && Date.now() < deadline && !deadRef.current) await sleep(200)
    return queueRef.current.size === 0
  }, [])

  const finish = useCallback((): Promise<void> => {
    if (stoppingRef.current) return stoppingRef.current
    const recorder = recorderRef.current
    const stream = streamRef.current
    const operation = (async () => {
      try {
        if (recorder && recorder.state !== 'inactive') {
          await new Promise<void>((resolve) => {
            recorder.addEventListener('stop', () => resolve(), { once: true })
            recorder.stop()
          })
        }
        await convertRef.current.catch(() => undefined)
        stream?.getTracks().forEach((track) => track.stop())
        if (deadRef.current) return
        const drained = !resumableRef.current || await drain(DRAIN_TIMEOUT_MS)
        if (!drained) {
          // 离线停止：不结束服务端会话，未上传部分留在本机，恢复网络后打开此会话自动补传。
          setNotice(null)
          setConnError(`网络未恢复，约 ${queueRef.current.size} 秒录音尚未上传，已保存在本机。网络恢复后重新打开此会话会自动补传；5 分钟内未补传将按已收到的内容处理。`)
          resumableRef.current = false
          window.clearTimeout(reconnectTimerRef.current)
          wsRef.current?.close()
          return
        }
        resumableRef.current = false
        window.clearTimeout(reconnectTimerRef.current)
        const ws = wsRef.current
        if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'control', action: 'stop' }))
        else if (sessionId) await api.stopSession(sessionId)
        if (sessionId) void chunkStore.clear(sessionId)
      } catch (error) {
        setConnError(error instanceof Error ? error.message : '录制收尾失败')
        if (sessionId) await api.stopSession(sessionId).catch(() => undefined)
      } finally {
        if (recorderRef.current === recorder) recorderRef.current = null
        if (streamRef.current === stream) streamRef.current = null
        setNotice(null)
        setCaptureState('STOPPED')
      }
    })()
    stoppingRef.current = operation
    void operation.finally(() => { stoppingRef.current = null })
    return operation
  }, [sessionId, drain])
  const finishRef = useRef(finish)
  finishRef.current = finish

  const start = useCallback(async () => {
    if (startingRef.current || recorderRef.current || stoppingRef.current || !sessionId) return
    startingRef.current = true
    const generation = generationRef.current
    setConnError(null)
    try {
      const ws = wsRef.current?.readyState === WebSocket.OPEN ? wsRef.current : await connect(false)
      if (generation !== generationRef.current) return
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      if (generation !== generationRef.current || ws.readyState !== WebSocket.OPEN) {
        stream.getTracks().forEach((track) => track.stop())
        return
      }
      streamRef.current = stream
      const mimeType = pickMimeType()
      const recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined)
      recorderRef.current = recorder
      queueRef.current = new ChunkQueue()
      deadRef.current = false
      attemptRef.current = 0
      wantedStateRef.current = 'RECORDING'
      void chunkStore.clear(sessionId)
      setLiveEnvelope([]); setQualityEvents([]); syncPending()
      recorder.ondataavailable = (event) => {
        if (!event.data.size) return
        // 串行转换，保证分片顺序与媒体容器一致
        convertRef.current = convertRef.current.then(async () => {
          const chunk = queueRef.current.add(await event.data.arrayBuffer())
          void chunkStore.put(sessionId, chunk)
          syncPending()
          const socket = wsRef.current
          if (socket?.readyState === WebSocket.OPEN) socket.send(encodeFrame(chunk.seq, chunk.data))
        })
        void convertRef.current.catch(() => undefined)
      }
      recorder.onerror = () => { setConnError('音频采集失败，正在保存已录内容'); void finishRef.current() }
      const settings = stream.getAudioTracks()[0].getSettings()
      ws.send(JSON.stringify({ type: 'control', action: 'start', settings: { ...settings, mimeType: recorder.mimeType, resumable: true } }))
      resumableRef.current = true
      recorder.start(TIMESLICE_MS)
      setCaptureState('RECORDING')
    } catch (error) {
      setConnError(error instanceof Error ? error.message : '无法开始录制')
      if (recorderRef.current) await finishRef.current()
      else {
        streamRef.current?.getTracks().forEach((track) => track.stop())
        streamRef.current = null
      }
    } finally { startingRef.current = false }
  }, [sessionId, connect, syncPending])

  const control = useCallback(async (action: 'pause' | 'resume') => {
    const recorder = recorderRef.current
    const ws = wsRef.current
    if (!recorder || stoppingRef.current) return
    if (ws?.readyState !== WebSocket.OPEN) { setNotice('网络重连中，暂时不能暂停或继续；录音仍在本机进行。'); return }
    if (action === 'pause' && recorder.state === 'recording') recorder.pause()
    else if (action === 'resume' && recorder.state === 'paused') recorder.resume()
    else return
    ws.send(JSON.stringify({ type: 'control', action }))
    wantedStateRef.current = action === 'pause' ? 'PAUSED' : 'RECORDING'
    setCaptureState(wantedStateRef.current)
  }, [])

  /** 页面重新打开：本机还有未上传的分片时，自动补传并结束这次录音。 */
  const recoverStored = useCallback(async (generation: number) => {
    if (!sessionId) return
    const stored = await chunkStore.list(sessionId)
    if (!stored.length || generation !== generationRef.current || recorderRef.current) return
    queueRef.current = new ChunkQueue()
    queueRef.current.restore(stored)
    syncPending()
    deadRef.current = false
    resumableRef.current = true
    wantedStateRef.current = 'PAUSED'
    setCaptureState('RECONNECTING')
    setNotice(`发现本机有约 ${stored.length} 秒未上传的录音，正在补传…`)
    try { await connect(true) } catch { /* onclose 负责重连或报告 */ }
    if (await drain(60000)) {
      resumableRef.current = false
      const ws = wsRef.current
      if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'control', action: 'stop' }))
      else await api.stopSession(sessionId).catch(() => undefined)
      await chunkStore.clear(sessionId)
      setNotice('未上传的录音已补传完成，正在处理。')
      setCaptureState('STOPPED')
    }
  }, [sessionId, connect, drain, syncPending])

  useEffect(() => {
    let cancelled = false
    const generation = generationRef.current
    if (sessionId) void api.getSession(sessionId).then((session) => {
      if (cancelled || startingRef.current || recorderRef.current) return
      setCommittedDurationMs(session.committedDurationMs)
      if (['RECORDING', 'PAUSED', 'RECONNECTING'].includes(session.captureStatus)) {
        setCaptureState('RECONNECTING')
        void recoverStored(generation).then(() => {
          if (!cancelled && !resumableRef.current && queueRef.current.size === 0 && !stoppingRef.current) {
            setCaptureState((state) => (state === 'RECONNECTING' ? 'STOPPED' : state))
          }
        })
      } else {
        setCaptureState(session.captureStatus === 'IDLE' ? 'IDLE' : 'STOPPED')
      }
    }).catch(() => undefined)
    return () => {
      cancelled = true
      generationRef.current += 1
      window.clearTimeout(reconnectTimerRef.current)
      const ws = wsRef.current
      if (recorderRef.current) void finishRef.current().finally(() => ws?.close())
      else { resumableRef.current = false; ws?.close() } // 补传未完成的分片仍在本机，下次打开再补
    }
  }, [sessionId, recoverStored])

  return {
    captureState, connError, notice, pendingSeconds: Math.round(pendingChunks * TIMESLICE_MS / 1000),
    rmsDbfs, peakDbfs, committedDurationMs, liveEnvelope, qualityEvents, stage, start,
    pause: () => control('pause'), resume: () => control('resume'), stop: finish,
  }
}
