import { useCallback, useEffect, useRef, useState } from 'react'
import { api, WS_BASE } from './api'
import { toDbfs } from './format'
import type { QualityEvent } from '../types/api'

type CaptureState = 'IDLE' | 'RECORDING' | 'PAUSED' | 'STOPPED'
type StageMsg = { type: 'stage'; stage: string; status: string }
type QualityEventMsg = { type: 'quality_event' } & Pick<QualityEvent, 'id' | 'eventType' | 'severity' | 'clientTimestampMs' | 'message'>

function pickMimeType(): string {
  return ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus', 'audio/mp4']
    .find((mime) => MediaRecorder.isTypeSupported(mime)) ?? ''
}

/** Owns capture until the last chunk is sent, including when the page unmounts. */
export function useRecordingSocket(sessionId: string | null) {
  const [captureState, setCaptureState] = useState<CaptureState>('IDLE')
  const [connError, setConnError] = useState<string | null>(null)
  const [rmsDbfs, setRmsDbfs] = useState(-60)
  const [peakDbfs, setPeakDbfs] = useState(-60)
  const [committedDurationMs, setCommittedDurationMs] = useState(0)
  const [liveEnvelope, setLiveEnvelope] = useState<number[]>([])
  const [qualityEvents, setQualityEvents] = useState<QualityEventMsg[]>([])
  const [stage, setStage] = useState<StageMsg | null>(null)
  const wsRef = useRef<WebSocket | null>(null)
  const connectingRef = useRef<Promise<WebSocket> | null>(null)
  const recorderRef = useRef<MediaRecorder | null>(null)
  const streamRef = useRef<MediaStream | null>(null)
  const pendingRef = useRef<Promise<void>>(Promise.resolve())
  const stoppingRef = useRef<Promise<void> | null>(null)
  const startingRef = useRef(false)
  const generationRef = useRef(0)

  const finish = useCallback((): Promise<void> => {
    if (stoppingRef.current) return stoppingRef.current
    const recorder = recorderRef.current
    const stream = streamRef.current
    const ws = wsRef.current
    const operation = (async () => {
      let uploadFailed = false
      try {
        if (recorder && recorder.state !== 'inactive') {
          await new Promise<void>((resolve) => {
            recorder.addEventListener('stop', () => resolve(), { once: true })
            recorder.stop()
          })
        }
        try {
          await pendingRef.current
        } catch {
          uploadFailed = true
        }
        if (ws?.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: 'control', action: 'stop' }))
        } else if (sessionId) {
          await api.stopSession(sessionId)
        }
        if (uploadFailed) setConnError((previous) => `${previous ? previous + ' ' : ''}部分音频未能上传，已保存服务端收到的录音。`)
      } catch (error) {
        setConnError(error instanceof Error ? error.message : '录制收尾失败')
        if (sessionId) await api.stopSession(sessionId).catch(() => undefined)
      } finally {
        stream?.getTracks().forEach((track) => track.stop())
        if (recorderRef.current === recorder) recorderRef.current = null
        if (streamRef.current === stream) streamRef.current = null
        setCaptureState('STOPPED')
      }
    })()
    stoppingRef.current = operation
    void operation.finally(() => { stoppingRef.current = null })
    return operation
  }, [sessionId])

  const ensureSocket = useCallback((): Promise<WebSocket> => {
    if (wsRef.current?.readyState === WebSocket.OPEN) return Promise.resolve(wsRef.current)
    if (connectingRef.current) return connectingRef.current
    if (!sessionId) return Promise.reject(new Error('没有可用的会话'))
    const ws = new WebSocket(`${WS_BASE}/ws/recording/${sessionId}`)
    wsRef.current = ws
    const connection = new Promise<WebSocket>((resolve, reject) => {
      let opened = false
      const timeout = window.setTimeout(() => {
        reject(new Error('录制连接超时，请重试'))
        ws.close()
      }, 10000)
      ws.onopen = () => { opened = true; window.clearTimeout(timeout); resolve(ws) }
      ws.onerror = () => {
        window.clearTimeout(timeout)
        reject(new Error('WebSocket 连接失败'))
        setConnError('WebSocket 连接失败')
      }
      ws.onclose = () => {
        window.clearTimeout(timeout)
        reject(new Error('录制连接已关闭，请新建会话重试'))
        if (wsRef.current !== ws) return
        wsRef.current = null
        if (opened && recorderRef.current && !stoppingRef.current) {
          setConnError('录制连接中断，已停止采集并保存已收到的音频。')
          void finish()
        }
      }
      ws.onmessage = (event) => {
        try {
          const msg = JSON.parse(event.data)
          switch (msg.type) {
            case 'level':
              setRmsDbfs(toDbfs(msg.rms)); setPeakDbfs(toDbfs(msg.peak))
              setCommittedDurationMs(msg.committedDurationMs)
              setLiveEnvelope((prev) => [...prev, Math.max(0.06, Math.min(1, msg.peak))])
              break
            case 'quality_event': setQualityEvents((prev) => [...prev, msg]); break
            case 'stage': setStage(msg); break
            case 'error':
              setConnError(`录制请求未被接受：${msg.code}`)
              void finish()
              break
          }
        } catch { /* Ignore malformed server messages. */ }
      }
    })
    connectingRef.current = connection
    void connection.then(
      () => { if (connectingRef.current === connection) connectingRef.current = null },
      () => { if (connectingRef.current === connection) connectingRef.current = null },
    )
    return connection
  }, [sessionId, finish])

  const start = useCallback(async () => {
    if (startingRef.current || recorderRef.current || stoppingRef.current) return
    startingRef.current = true
    const generation = generationRef.current
    setConnError(null)
    try {
      const ws = await ensureSocket()
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
      pendingRef.current = Promise.resolve()
      setLiveEnvelope([]); setQualityEvents([])
      recorder.ondataavailable = (event) => {
        if (!event.data.size) return
        // Serialize conversion and sending so chunk order matches the media container.
        pendingRef.current = pendingRef.current.then(async () => {
          const buffer = await event.data.arrayBuffer()
          if (ws.readyState !== WebSocket.OPEN) throw new Error('录音上传连接已关闭')
          ws.send(buffer)
        })
        void pendingRef.current.catch(() => undefined)
      }
      recorder.onerror = () => { setConnError('音频采集失败，正在保存已录内容'); void finish() }
      const settings = stream.getAudioTracks()[0].getSettings()
      ws.send(JSON.stringify({ type: 'control', action: 'start', settings: { ...settings, mimeType: recorder.mimeType } }))
      recorder.start(1000)
      setCaptureState('RECORDING')
    } catch (error) {
      setConnError(error instanceof Error ? error.message : '无法开始录制')
      if (recorderRef.current) await finish()
      else {
        streamRef.current?.getTracks().forEach((track) => track.stop())
        streamRef.current = null
      }
    } finally { startingRef.current = false }
  }, [ensureSocket, finish])

  const control = useCallback(async (action: 'pause' | 'resume') => {
    const recorder = recorderRef.current
    const ws = wsRef.current
    if (!recorder || stoppingRef.current) return
    try {
      if (ws?.readyState !== WebSocket.OPEN) throw new Error('录制连接已断开')
      if (action === 'pause' && recorder.state === 'recording') recorder.pause()
      else if (action === 'resume' && recorder.state === 'paused') recorder.resume()
      else return
      ws.send(JSON.stringify({ type: 'control', action }))
      setCaptureState(action === 'pause' ? 'PAUSED' : 'RECORDING')
    } catch (error) {
      setConnError(error instanceof Error ? error.message : '录制控制失败')
      await finish()
    }
  }, [finish])

  useEffect(() => {
    let cancelled = false
    if (sessionId) void api.getSession(sessionId).then((session) => {
      if (!cancelled && !startingRef.current && !recorderRef.current) {
        setCaptureState(session.captureStatus === 'IDLE' ? 'IDLE' : 'STOPPED')
        setCommittedDurationMs(session.committedDurationMs)
      }
    }).catch(() => undefined)
    return () => {
      cancelled = true
      generationRef.current += 1
      const ws = wsRef.current
      if (recorderRef.current) void finish().finally(() => ws?.close())
      else ws?.close()
    }
  }, [sessionId, finish])

  return {
    captureState, connError, rmsDbfs, peakDbfs, committedDurationMs, liveEnvelope,
    qualityEvents, stage, start,
    pause: () => control('pause'), resume: () => control('resume'), stop: finish,
  }
}
