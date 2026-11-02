import type {
  RenderTranscript,
  AudioQualityReport,
  AudioVersion,
  DenoiseMode,
  EditDecision,
  EnhancementJob,
  LoudnessTarget,
  RoleAssignment,
  RenderOutput,
  TranscriptToken,
  TranscriptionJob,
  Warning,
} from '../types/api'

/**
 * 后端地址。开发时后端默认跑在 8001；部署时通过 VITE_API_BASE 覆盖。
 */
export const API_BASE = import.meta.env.VITE_API_BASE ?? 'http://127.0.0.1:8001'
export const WS_BASE = API_BASE.replace(/^http/, 'ws')

/** SessionsPage 用的精简会话视图，对应后端 RecordingSessionListItemOut。 */
export type SessionListItem = {
  id: string
  title: string
  projectId: string
  captureMode: 'batch' | 'live'
  captureStatus: string
  localPersistenceStatus: string
  uploadStatus: string
  serverMergeStatus: string
  liveAnalysisStatus: string
  processingStatus: string
  totalDurationMs: number
  /** ISO 8601；迁移前的旧会话没有记录创建时间，为 null。 */
  createdAt: string | null
}

/** 会话详情，对应后端 RecordingSessionOut。 */
export type SessionDetail = {
  id: string
  title: string
  projectId: string
  captureMode: 'batch' | 'live'
  analysisLocality: 'local'
  captureStatus: string
  localPersistenceStatus: string
  uploadStatus: string
  serverMergeStatus: string
  liveAnalysisStatus: string
  processingStatus: string
  deviceLabel: string
  actualSampleRate: number
  channelCount: number
  mimeType: string
  lookbackWindowMs: number
  committedDurationMs: number
  totalDurationMs: number
  captureProcessing: {
    applied: boolean
    unknown: boolean
    echoCancellationRequested: boolean
    echoCancellationActual: string
    noiseSuppressionRequested: boolean
    noiseSuppressionActual: string
    autoGainControlRequested: boolean
    autoGainControlActual: string
  }
  clockDrift: Record<string, number | boolean> | null
  warnings: Warning[]
}

export class DecisionConflictError extends Error {
  current: EditDecision
  constructor(current: EditDecision) {
    super('这条决策已被其他操作修改')
    this.current = current
  }
}

class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  // FormData bodies need the browser's own multipart boundary header; setting
  // Content-Type ourselves here would strip it and break the upload.
  const isFormData = init?.body instanceof FormData
  const res = await fetch(`${API_BASE}${path}`, {
    headers: isFormData ? (init?.headers ?? {}) : { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
    ...init,
  })
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    let message = text || res.statusText
    let body
    try { body = JSON.parse(text) } catch { /* Non-JSON error body. */ }
    if (res.status === 409 && body?.detail?.code === 'decision_conflict') {
      throw new DecisionConflictError(body.detail.current)
    }
    if (typeof body?.detail === 'string') message = body.detail
    throw new ApiError(res.status, message)
  }
  if (res.status === 204) return undefined as T
  return (await res.json()) as T
}

/** 404 视为"还没有"，调用方按 null 处理，不当成错误抛出。 */
async function requestOrNull<T>(path: string): Promise<T | null> {
  try {
    return await request<T>(path)
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) return null
    throw e
  }
}

export const api = {
  createSession(body: { title?: string; captureMode?: 'batch' | 'live'; deviceLabel?: string; projectId?: string }) {
    return request<SessionDetail>('/api/sessions', { method: 'POST', body: JSON.stringify(body) })
  },
  listSessions() {
    return request<SessionListItem[]>('/api/sessions')
  },
  getSession(id: string) {
    return request<SessionDetail>(`/api/sessions/${id}`)
  },
  updateSession(id: string, body: { title?: string; projectId?: string }) {
    return request<SessionDetail>(`/api/sessions/${id}`, { method: 'PATCH', body: JSON.stringify(body) })
  },
  deleteSession(id: string) {
    return request<void>(`/api/sessions/${id}`, { method: 'DELETE' })
  },
  stopSession(id: string) {
    return request<SessionDetail>(`/api/sessions/${id}/stop`, { method: 'POST' })
  },
  importAudio(id: string, file: File) {
    const body = new FormData()
    body.append('file', file)
    return request<SessionDetail>(`/api/sessions/${id}/import`, { method: 'POST', body })
  },
  getVersions(id: string) {
    return request<AudioVersion[]>(`/api/sessions/${id}/versions`)
  },
  getRoles(id: string) {
    return request<RoleAssignment[]>(`/api/sessions/${id}/roles`)
  },
  getQuality(id: string) {
    return requestOrNull<AudioQualityReport>(`/api/sessions/${id}/quality`)
  },
  getWaveform(id: string, buckets = 200) {
    return requestOrNull<{ envelope: number[] }>(`/api/sessions/${id}/waveform?buckets=${buckets}`)
  },
  audioUrl(sessionId: string, versionId: string) {
    return `${API_BASE}/api/sessions/${sessionId}/versions/${versionId}/audio`
  },
  getWarnings(id: string) {
    return request<Warning[]>(`/api/sessions/${id}/warnings`)
  },
  triggerEnhancement(id: string, mode: DenoiseMode | 'auto' | 'conservative' | 'aggressive') {
    return request<EnhancementJob>(`/api/sessions/${id}/enhancement`, {
      method: 'POST',
      body: JSON.stringify({ mode }),
    })
  },
  getEnhancement(id: string, jobId: string) {
    return request<EnhancementJob>(`/api/sessions/${id}/enhancement/${jobId}`)
  },
  getLatestEnhancement(id: string) {
    return requestOrNull<EnhancementJob>(`/api/sessions/${id}/enhancement`)
  },
  addManualDecision(id: string, startMs: number, endMs: number) {
    return request<EditDecision>(`/api/sessions/${id}/decisions`, {
      method: 'POST', body: JSON.stringify({ startMs, endMs }),
    })
  },
  saveRetake(id: string, blob: Blob, startMs: number, endMs: number, sourceVersionId: string, decisions: EditDecision[]) {
    const body = new FormData()
    body.append('file', blob, 'retake.webm')
    body.append('startMs', String(startMs))
    body.append('endMs', String(endMs))
    body.append('sourceVersionId', sourceVersionId)
    body.append('snapshot', JSON.stringify({ expectedRevisions: Object.fromEntries(decisions.map((d) => [d.id, d.revision])) }))
    return request<EditDecision>(`/api/sessions/${id}/retakes`, { method: 'POST', body })
  },
  render(id: string, smoothCuts = false, loudnessTarget: LoudnessTarget | null = null) {
    return request<RenderOutput>(`/api/sessions/${id}/render`, { method: 'POST', body: JSON.stringify({ smoothCuts, loudnessTarget }) })
  },
  suggestPauses(id: string, minPauseMs: number, keepMs: number) {
    return request<{ addedCount: number; decisions: EditDecision[] }>(`/api/sessions/${id}/decisions/suggest-pauses`, {
      method: 'POST', body: JSON.stringify({ minPauseMs, keepMs }),
    })
  },
  getRender(id: string) {
    return requestOrNull<RenderOutput>(`/api/sessions/${id}/render`)
  },
  downloadUrl(id: string, versionId: string) {
    return `${API_BASE}/api/sessions/${id}/versions/${versionId}/audio?download=true`
  },
  suggestDecisions(id: string, transcriptionJobId: string) {
    return request<{ addedCount: number; decisions: EditDecision[] }>(`/api/sessions/${id}/decisions/suggest`, {
      method: 'POST', body: JSON.stringify({ transcriptionJobId }),
    })
  },
  getDecisions(id: string) {
    return request<EditDecision[]>(`/api/sessions/${id}/decisions`)
  },
  updateDecision(sessionId: string, decisionId: string, accepted: boolean | null, expectedRevision?: number) {
    return request<EditDecision>(`/api/sessions/${sessionId}/decisions/${decisionId}`, {
      method: 'PATCH',
      body: JSON.stringify({ accepted, expectedRevision }),
    })
  },
  rejectPendingSuggestions(sessionId: string, decisions: EditDecision[]) {
    return request<EditDecision[]>(`/api/sessions/${sessionId}/decisions/reject-suggestions`, { method: 'POST', body: JSON.stringify({ expectedRevisions: Object.fromEntries(decisions.map((d) => [d.id, d.revision])) }) })
  },
  confirmDecisions(sessionId: string, decisions: EditDecision[]) {
    return request<EditDecision[]>(`/api/sessions/${sessionId}/decisions/confirm`, { method: 'POST', body: JSON.stringify({ expectedRevisions: Object.fromEntries(decisions.map((d) => [d.id, d.revision])) }) })
  },
  startTranscription(id: string, language: 'auto' | 'zh' | 'en') {
    return request<TranscriptionJob>(`/api/sessions/${id}/transcription`, {
      method: 'POST', body: JSON.stringify({ language }),
    })
  },
  getLatestTranscription(id: string) {
    return requestOrNull<TranscriptionJob>(`/api/sessions/${id}/transcription`)
  },
  getTranscription(id: string, jobId: string) {
    return request<TranscriptionJob>(`/api/sessions/${id}/transcription/${jobId}`)
  },
  getRenderTranscript(id: string, versionId: string) {
    return request<RenderTranscript>(`/api/sessions/${id}/render/${versionId}/transcript`)
  },
  transcribeRender(id: string, versionId: string, language: 'auto' | 'zh' | 'en') {
    return request<TranscriptionJob>(`/api/sessions/${id}/render/${versionId}/transcription`, {
      method: 'POST', body: JSON.stringify({ language }),
    })
  },
  async downloadRenderSubtitles(id: string, versionId: string, format: 'txt' | 'srt' = 'srt') {
    const response = await fetch(`${API_BASE}/api/sessions/${id}/render/${versionId}/subtitles?format=${format}`)
    if (!response.ok) {
      const error = await response.json().catch(() => null)
      throw new Error(error?.detail ?? '成片字幕导出失败')
    }
    return response.blob()
  },
  async downloadTranscript(id: string, transcriptionJobId: string, format: 'txt' | 'srt') {
    const query = new URLSearchParams({ transcriptionJobId, format })
    const response = await fetch(`${API_BASE}/api/sessions/${id}/transcript/export?${query}`)
    if (!response.ok) {
      const error = await response.json().catch(() => null)
      throw new Error(error?.detail ?? '转写导出失败')
    }
    return response.blob()
  },
  getTranscript(id: string) {
    return request<TranscriptToken[]>(`/api/sessions/${id}/transcript`)
  },
}
