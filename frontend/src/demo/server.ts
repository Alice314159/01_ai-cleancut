/**
 * 演示模式：在浏览器里拦截对 /api 的请求，用内存数据模拟后端，让打包后的单个 HTML
 * 不连后端也能完整点一遍界面。只在 VITE_DEMO=1 构建时加载，正常构建不包含本文件的逻辑。
 *
 * 模拟的是"流程与界面"，不是算法：转写、停顿检测、响度都是按预置脚本给出的结果；
 * 录音、重录需要真实后端，演示中会给出提示。
 */
import { API_BASE } from '../lib/api'
import type { Chapter, EditDecision, TranscriptToken } from '../types/api'
import { applyCuts, DEMO_RATE, envelope, scale, synthesize, toWav, type Word } from './audio'

type Json = Record<string, unknown>
type Item = string | { pause: number }

const CUT = (action: string) => action.startsWith('DELETE_') || action === 'COMPRESS_SILENCE'
let counter = 0
const uid = (prefix: string) => `${prefix}_${(++counter).toString(36).padStart(4, '0')}${Math.random().toString(36).slice(2, 6)}`

/** 把"词 / 停顿"脚本排成时间表：每个字约 0.17 秒，句末多停一会儿。 */
function layout(items: Item[]): { words: Word[]; durationMs: number } {
  const words: Word[] = []
  let t = 400
  for (const item of items) {
    if (typeof item !== 'string') { t += item.pause; continue }
    const chars = [...item.replace(/[，。！？、,.!?\s]/g, '')].length || 1
    const length = Math.max(180, chars * 170)
    words.push({ text: item, startMs: t, endMs: t + length })
    t += length + (/[。！？.!?]$/.test(item) ? 420 : /[，,]$/.test(item) ? 220 : 70)
  }
  return { words, durationMs: t + 500 }
}

const SCRIPTS: Record<string, Item[]> = {
  episode: ['大家好，', '欢迎', '收听', '第十二期', '节目。', { pause: 2600 }, '今天', '我们', '我们', '想聊一聊', '嗯', '本地优先的', '音频', '工作流。',
    '为什么', '要把', '录音', '和', '剪辑', '都放在', '自己的', '电脑上？', '首先', '是', '隐私，', '其次', '是', '速度，', '嗯', '最后', '是', '不依赖', '网络。',
    { pause: 2100 }, '下面', '我们', '就', '一步一步', '来看。'],
  intro: ['你好，', '这里是', '开场白', '试录。', { pause: 1800 }, '这一段', '还没有', '转写，', '可以', '点', '开始', '本机转写', '看看', '效果。'],
  lesson: ['同学们', '好，', '这一讲', '我们', '讲', '响度', '标准化。', { pause: 1500 }, '播客', '常用', '负十六', 'LUFS，', '广播', '用', '负二十三。'],
}

type DemoSession = {
  detail: Json
  words: Word[]
  pcm: Float32Array
  versions: Json[]
  roles: Json[]
  audio: Map<string, Blob>
  pcmByVersion: Map<string, Float32Array>
  tokens: TranscriptToken[]
  job: Json | null
  decisions: EditDecision[]
  render: Json | null
  enhancement: Json | null
  chapters: Chapter[]
}

const sessions = new Map<string, DemoSession>()
const covers = new Map<string, string>() // sessionId → blob URL
const urls = new Map<string, string>()

function createSession(title: string, projectId: string, script: Item[], createdMinutesAgo: number, transcribed: boolean): DemoSession {
  const id = uid('rs')
  const { words, durationMs } = layout(script)
  const pcm = synthesize(words, durationMs, 0.006, id.length + createdMinutesAgo)
  const original = uid('av')
  const session: DemoSession = {
    detail: {
      id, title, projectId, captureMode: 'batch', analysisLocality: 'local', captureStatus: 'STOPPED', localPersistenceStatus: 'NOT_SUPPORTED',
      uploadStatus: 'COMPLETED', serverMergeStatus: 'VERIFIED', liveAnalysisStatus: 'FINALIZED', processingStatus: 'NOT_STARTED',
      deviceLabel: '演示音频', actualSampleRate: DEMO_RATE, channelCount: 1, mimeType: 'audio/wav', lookbackWindowMs: 15000,
      committedDurationMs: durationMs, totalDurationMs: durationMs, warnings: [], clockDrift: null,
      createdAt: new Date(Date.now() - createdMinutesAgo * 60000).toISOString(),
      captureProcessing: { applied: false, unknown: true, echoCancellationRequested: false, echoCancellationActual: 'unknown',
        noiseSuppressionRequested: false, noiseSuppressionActual: 'unknown', autoGainControlRequested: false, autoGainControlActual: 'unknown' },
    },
    words, pcm, versions: [], roles: [], audio: new Map(), pcmByVersion: new Map(), tokens: [], job: null, decisions: [], render: null, enhancement: null, chapters: [],
  }
  addVersion(session, original, 'original', '原始录音', pcm)
  session.roles = ['TIMEBASE', 'ASR_SOURCE', 'REVIEW_SOURCE', 'RENDER_SOURCE', 'EXPORT_SOURCE'].map((role) => ({ role, audioVersionId: original }))
  if (transcribed) completeTranscription(session)
  sessions.set(id, session)
  return session
}

function addVersion(session: DemoSession, id: string, versionType: string, label: string, pcm: Float32Array, engine = 'demo') {
  session.versions.push({ id, versionType, label, durationMs: Math.round((pcm.length / DEMO_RATE) * 1000), sampleCount: pcm.length, sampleRate: DEMO_RATE,
    latencyMs: 0, engine, engineVersion: '1.0', determinism: 'deterministic', processingLocality: 'local' })
  session.audio.set(id, toWav(pcm))
  session.pcmByVersion.set(id, pcm)
}

function completeTranscription(session: DemoSession) {
  const jobId = uid('asr')
  session.job = { id: jobId, sourceVersionId: session.roles[0].audioVersionId, model: 'base', requestedLanguage: 'auto', detectedLanguage: 'zh',
    status: 'COMPLETED', progress: 1, errorMessage: null }
  session.tokens = session.words.map((w, seq) => ({ id: uid('t'), text: w.text, startMs: w.startMs, endMs: w.endMs, jobId, state: 'committed', cut: false, seq } as TranscriptToken))
  // 和真实后端一样：转写完成即自动提示相邻重复
  session.tokens.forEach((token, i) => {
    const next = session.tokens[i + 1]
    if (next && token.text === next.text) addDecision(session, token.startMs, token.endMs!, 'DELETE_REPETITION', token.text, 'transcript_rule',
      `相邻文字重复，建议保留最后一次（${(next.startMs / 1000).toFixed(3)} 秒起）。也可能是刻意强调，请试听确认。`)
  })
}

function addDecision(session: DemoSession, startMs: number, endMs: number, action: string, text: string, source: string, reason: string, accepted: boolean | null = null) {
  const decision = { id: uid('d'), startMs, endMs, action, text, reason, confidence: 1, source, hardProtected: false, reviewRequired: true,
    accepted, revision: 0, replacementVersionId: null, replacementDurationMs: null } as unknown as EditDecision
  session.decisions.push(decision)
  session.decisions.sort((a, b) => a.startMs - b.startMs)
  return decision
}

const overlaps = (session: DemoSession, a: number, b: number) => session.decisions.some((d) => d.startMs < b && a < d.endMs)
const renderSourceId = (session: DemoSession) => session.roles.find((r) => r.role === 'RENDER_SOURCE')!.audioVersionId as string
const fingerprint = (session: DemoSession) => JSON.stringify([renderSourceId(session), session.decisions.map((d) => [d.id, d.accepted])])

function mergedCuts(session: DemoSession) {
  const ranges = session.decisions.filter((d) => d.accepted && CUT(d.action)).map((d) => [d.startMs, d.endMs]).sort((a, b) => a[0] - b[0])
  const merged: { startMs: number; endMs: number }[] = []
  for (const [a, b] of ranges) {
    const last = merged[merged.length - 1]
    if (last && a <= last.endMs) last.endMs = Math.max(last.endMs, b)
    else merged.push({ startMs: a, endMs: b })
  }
  return merged
}

function renderOut(session: DemoSession) {
  if (!session.render) return null
  const { fingerprint: fp, ...rest } = session.render
  return { ...rest, stale: fp !== fingerprint(session) }
}

function srt(cues: { startMs: number; endMs: number; text: string }[]) {
  const ts = (ms: number) => {
    const h = Math.floor(ms / 3600000), m = Math.floor(ms / 60000) % 60, s = Math.floor(ms / 1000) % 60
    return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')},${String(Math.round(ms % 1000)).padStart(3, '0')}`
  }
  return cues.map((c, i) => `${i + 1}\n${ts(c.startMs)} --> ${ts(c.endMs)}\n${c.text}\n`).join('\n')
}

function sentenceCues(tokens: { text: string; startMs: number; endMs: number }[]) {
  const cues: { startMs: number; endMs: number; text: string }[] = []
  for (const t of tokens) {
    const last = cues[cues.length - 1]
    if (last && !/[。！？.!?]$/.test(last.text) && t.startMs - last.endMs < 800) { last.text += t.text; last.endMs = t.endMs }
    else cues.push({ ...t })
  }
  return cues
}

// ───────────── 路由 ─────────────

const json = (status: number, body: unknown, headers: Record<string, string> = {}) =>
  new Response(status === 204 ? null : JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json', ...headers } })
const notFound = (detail = 'not found') => json(404, { detail })

function listItem(s: DemoSession) {
  const d = s.detail
  return { id: d.id, title: d.title, projectId: d.projectId, captureMode: d.captureMode, captureStatus: d.captureStatus, localPersistenceStatus: 'NOT_SUPPORTED',
    uploadStatus: d.uploadStatus, serverMergeStatus: d.serverMergeStatus, liveAnalysisStatus: d.liveAnalysisStatus, processingStatus: d.processingStatus,
    totalDurationMs: d.totalDurationMs, createdAt: d.createdAt }
}

async function handle(method: string, path: string, query: URLSearchParams, body: Json, form?: FormData): Promise<Response> {
  if (path === '/api/health') return json(200, { status: 'ok' })
  if (path === '/api/sessions' && method === 'GET') {
    return json(200, [...sessions.values()].sort((a, b) => String(b.detail.createdAt).localeCompare(String(a.detail.createdAt))).map(listItem))
  }
  if (path === '/api/sessions' && method === 'POST') {
    const session = createSession(String(body.title ?? '未命名录制'), String(body.projectId ?? 'proj_default'), SCRIPTS.intro, 0, false)
    if (body.captureMode !== 'batch') {
      // 实时录音需要真实后端（WebSocket + ffmpeg），演示中改成一段预置音频。
      session.detail.title = `${session.detail.title}（演示音频）`
    }
    return json(201, session.detail)
  }
  const match = /^\/api\/sessions\/([^/]+)(?:\/(.*))?$/.exec(path)
  if (!match) return notFound()
  const session = sessions.get(match[1])
  if (!session) return notFound('session not found')
  const rest = match[2] ?? ''
  const versionMatch = /^versions\/([^/]+)\/(audio|export)$/.exec(rest)

  if (rest === '' && method === 'GET') return json(200, session.detail)
  if (rest === '' && method === 'PATCH') { Object.assign(session.detail, body.title ? { title: body.title } : {}, body.projectId ? { projectId: body.projectId } : {}); return json(200, session.detail) }
  if (rest === '' && method === 'DELETE') { sessions.delete(match[1]); return json(204, null) }
  if (rest === 'import') return json(201, session.detail)
  if (rest === 'cover' && method === 'PUT') {
    const file = form?.get('file')
    if (!(file instanceof Blob) || !file.type.startsWith('image/')) return json(422, { detail: '封面仅支持 JPEG、PNG 或 WebP 图片' })
    if (file.size > 5 * 1024 * 1024) return json(422, { detail: '封面图片不能超过 5MB' })
    const previous = covers.get(match[1])
    if (previous) URL.revokeObjectURL(previous)
    covers.set(match[1], URL.createObjectURL(file))
    session.detail.coverVersion = String(Date.now())
    return json(200, session.detail)
  }
  if (rest === 'cover' && method === 'DELETE') {
    const previous = covers.get(match[1])
    if (previous) URL.revokeObjectURL(previous)
    covers.delete(match[1])
    session.detail.coverVersion = null
    return json(200, session.detail)
  }
  if (rest === 'stop') return json(200, session.detail)
  if (rest === 'versions') return json(200, session.versions)
  if (rest === 'roles') return json(200, session.roles)
  if (rest === 'warnings') return json(200, [])
  if (rest === 'waveform') return json(200, { envelope: envelope(session.pcm, Number(query.get('buckets') ?? 200)) })
  if (rest === 'quality') return json(200, { qualityGrade: 'GOOD', gradeReason: '演示数据：语音清晰，底噪轻微。', integratedLufs: -21.4, truePeakDbtp: -3.2,
    noiseFloorDbfs: -52, estimatedSnrDb: 31, clippingRatio: 0, humProbability: 0.04, reverbScore: 0.18, speechCoverage: 0.72, silenceCoverage: 0.28,
    noiseTypes: ['broadband'], contentType: 'unknown', contentTypeConfidence: 0, contentTypeOverriddenByUser: false })

  if (versionMatch) {
    const blob = session.audio.get(versionMatch[1])
    if (!blob) return notFound('version not found')
    const version = session.versions.find((v) => v.id === versionMatch[1])!
    const label = version.versionType === 'master' ? '成片' : version.versionType === 'denoised' ? '降噪' : '原始'
    const fmt = versionMatch[2] === 'export' ? (query.get('format') ?? 'mp3') : 'wav'
    // 演示里不做真实转码，下载的是同一段 WAV；文件名注明以免误会。
    const filename = `${session.detail.title}-${label}${fmt === 'wav' ? '' : `（演示，实为WAV）`}.wav`
    return new Response(blob, { status: 200, headers: { 'Content-Type': 'audio/wav', 'Content-Disposition': `attachment; filename*=utf-8''${encodeURIComponent(filename)}` } })
  }

  if (rest === 'enhancement' && method === 'GET') return session.enhancement ? json(200, session.enhancement) : notFound('no enhancement yet')
  if (rest.startsWith('enhancement/')) return session.enhancement ? json(200, session.enhancement) : notFound()
  if (rest === 'enhancement' && method === 'POST') {
    const id = uid('av')
    addVersion(session, id, 'denoised', '降噪版本', synthesize(session.words, Number(session.detail.totalDurationMs), 0.0012, 7), 'spectral_subtract')
    for (const role of session.roles) if (role.role !== 'TIMEBASE') { role.upgradedFrom = role.audioVersionId; role.audioVersionId = id }
    session.detail.processingStatus = 'DONE'
    session.enhancement = { id: uid('job'), mode: body.mode === 'auto' ? 'balanced' : body.mode, status: 'COMPLETED', progress: 1, resultVersionId: id, errorMessage: null,
      enhancementSuccess: true, noiseReductionDb: 11.6, requestedChain: ['denoise'], actualChain: ['denoise'], warnings: [],
      voiceProtection: [
        { key: 'speech_energy', label: '语音能量保留', value: 97.8, unit: '%', target: '≥ 95%', pass: true, hard: true },
        { key: 'spectral_centroid', label: '音色偏移', value: 2.1, unit: '%', target: '≤ 8%', pass: true },
      ] }
    return json(202, session.enhancement)
  }

  if (rest === 'transcript' && method === 'GET') return json(200, session.tokens)
  if (rest === 'transcription' && method === 'GET') return session.job ? json(200, session.job) : notFound('no transcription')
  if (rest === 'transcription' && method === 'POST') {
    session.job = { id: uid('asr'), sourceVersionId: renderSourceId(session), model: 'base', requestedLanguage: body.language ?? 'auto', detectedLanguage: null,
      status: 'TRANSCRIBING', progress: 0.1, errorMessage: null }
    return json(202, session.job)
  }
  if (rest.startsWith('transcription/')) {
    const job = session.job
    if (!job) return notFound()
    if (job.status === 'TRANSCRIBING') {
      job.progress = Math.min(1, Number(job.progress) + 0.3)
      if (Number(job.progress) >= 1) { completeTranscription(session); return json(200, session.job) }
    }
    return json(200, job)
  }
  if (rest === 'transcript/export') {
    const text = query.get('format') === 'srt' ? srt(sentenceCues(session.tokens as { text: string; startMs: number; endMs: number }[])) : session.tokens.map((t) => t.text).join('') + '\n'
    return new Response(text, { headers: { 'Content-Type': 'text/plain; charset=utf-8' } })
  }

  if (rest === 'decisions' && method === 'GET') return json(200, session.decisions)
  if (rest === 'decisions' && method === 'POST') {
    const start = Number(body.startMs), end = Number(body.endMs)
    if (!(end > start) || end > Number(session.detail.totalDurationMs)) return json(422, { detail: '删除区间超出录音时长' })
    return json(201, addDecision(session, start, end, 'DELETE_SELECTION', session.tokens.filter((t) => t.startMs >= start && (t.endMs ?? 0) <= end).map((t) => t.text).join(''), 'user', '手动选择删除', true))
  }
  const decisionMatch = /^decisions\/(d_[^/]+)$/.exec(rest)
  if (decisionMatch && method === 'PATCH') {
    const decision = session.decisions.find((d) => d.id === decisionMatch[1])
    if (!decision) return notFound('decision not found')
    decision.accepted = body.accepted as boolean | null
    decision.revision = (decision.revision ?? 0) + 1
    return json(200, decision)
  }
  if (rest === 'decisions/confirm') { session.decisions.forEach((d) => { if (d.accepted === null) { d.accepted = true; d.revision = (d.revision ?? 0) + 1 } }); return json(200, session.decisions) }
  if (rest === 'decisions/reject-suggestions') {
    session.decisions.forEach((d) => { if (d.accepted === null && ['transcript_rule', 'silence_rule'].includes(d.source)) { d.accepted = false; d.revision = (d.revision ?? 0) + 1 } })
    return json(200, session.decisions)
  }
  if (rest === 'decisions/suggest') {
    let added = 0
    for (const t of session.tokens) {
      if (['嗯', '呃', 'um', 'uh'].includes(t.text.replace(/[，。,.\s]/g, '')) && !overlaps(session, t.startMs, t.endMs!)) {
        addDecision(session, t.startMs, t.endMs!, 'DELETE_FILLER', t.text, 'transcript_rule', '独立语气词规则命中；可能表达回应或犹豫，请结合上下文试听确认。'); added += 1
      }
    }
    return json(200, { addedCount: added, decisions: session.decisions })
  }
  if (rest === 'decisions/suggest-pauses') {
    const minPause = Number(body.minPauseMs ?? 1500), keep = Number(body.keepMs ?? 500)
    let added = 0
    session.words.forEach((w, i) => {
      const next = session.words[i + 1]
      if (!next || next.startMs - w.endMs < minPause) return
      const start = w.endMs + Math.floor(keep / 2), end = next.startMs - Math.floor(keep / 2)
      if (overlaps(session, start, end)) return
      addDecision(session, start, end, 'COMPRESS_SILENCE', '', 'silence_rule',
        `停顿 ${((next.startMs - w.endMs) / 1000).toFixed(1)} 秒，建议缩短为约 ${(keep / 1000).toFixed(1)} 秒。依据音频能量判断，可能是有意的停顿，请试听确认。`)
      added += 1
    })
    return json(200, { addedCount: added, decisions: session.decisions })
  }

  if (rest === 'render' && method === 'GET') return session.render ? json(200, renderOut(session)) : notFound('no rendered audio yet')
  if (rest === 'render' && method === 'POST') {
    if (session.decisions.some((d) => d.accepted === null)) return json(409, { detail: '还有待定决策，请先接受或拒绝后再渲染' })
    const cuts = mergedCuts(session)
    const source = session.pcmByVersion.get(renderSourceId(session))!
    let pcm = applyCuts(source, cuts)
    const target = body.loudnessTarget as string | null
    const loudness = target ? { target, targetLufs: target === 'podcast' ? -16 : -23, inputLufs: -21.4, inputTruePeakDb: -3.2,
      gainDb: target === 'podcast' ? 1.7 : -1.6, outputLufs: target === 'podcast' ? -19.7 : -23, limitedBy: target === 'podcast' ? 'peak' : null } : null
    if (loudness) pcm = scale(pcm, 10 ** (loudness.gainDb / 20))
    const id = uid('av')
    addVersion(session, id, 'master', '剪辑成片', pcm, body.smoothCuts ? 'pcm_cut_fade' : 'pcm_cut')
    session.roles.find((r) => r.role === 'EXPORT_SOURCE')!.audioVersionId = id
    session.render = { version: session.versions[session.versions.length - 1], sourceVersionId: renderSourceId(session),
      removedDurationMs: cuts.reduce((sum, c) => sum + c.endMs - c.startMs, 0), cutRanges: cuts, hasReplacements: false,
      transcriptionJob: null, transcriptReady: false, loudness, fingerprint: fingerprint(session), cutsSnapshot: cuts }
    return json(201, renderOut(session))
  }
  const subtitles = /^render\/([^/]+)\/subtitles$/.exec(rest)
  if (subtitles && session.render) {
    const cuts = session.render.cutsSnapshot as { startMs: number; endMs: number }[]
    const shift = (t: number) => t - cuts.reduce((sum, c) => sum + Math.max(0, Math.min(t, c.endMs) - c.startMs), 0)
    const kept = session.tokens.filter((t) => !cuts.some((c) => c.startMs <= t.startMs && (t.endMs ?? 0) <= c.endMs))
      .map((t) => ({ text: t.text, startMs: shift(t.startMs), endMs: shift(t.endMs ?? t.startMs) }))
    if (!kept.length) return json(409, { detail: '此成片没有转写快照，请完成转写后重新生成成片' })
    return new Response(srt(sentenceCues(kept)), { headers: { 'Content-Type': 'text/plain; charset=utf-8' } })
  }
  if (rest === 'chapters' && method === 'GET') return json(200, session.chapters)
  if (rest === 'chapters' && method === 'POST') {
    const startMs = Number(body.startMs), title = String(body.title ?? '').trim()
    if (!title) return json(422, { detail: '章节标题不能为空' })
    if (session.chapters.some((c) => c.startMs === startMs)) return json(409, { detail: '该时间点已有章节' })
    const chapter = { id: uid('ch'), startMs, title }
    session.chapters = [...session.chapters, chapter].sort((a, b) => a.startMs - b.startMs)
    return json(201, chapter)
  }
  const chapterMatch = /^chapters\/([^/]+)$/.exec(rest)
  if (chapterMatch) {
    const chapter = session.chapters.find((c) => c.id === chapterMatch[1])
    if (!chapter) return notFound('chapter not found')
    if (method === 'DELETE') { session.chapters = session.chapters.filter((c) => c !== chapter); return json(204, null) }
    if (typeof body.title === 'string' && body.title.trim()) chapter.title = body.title.trim()
    return json(200, chapter)
  }
  const versionChapters = /^versions\/([^/]+)\/chapters$/.exec(rest)
  if (versionChapters) {
    const version = session.versions.find((v) => v.id === versionChapters[1])
    if (!version) return notFound('version not found')
    const cuts = version.versionType === 'master' && session.render ? session.render.cutsSnapshot as { startMs: number; endMs: number }[] : []
    const map = (t: number) => {
      let shift = 0
      for (const c of cuts) { if (t < c.startMs) break; if (t < c.endMs) return { at: c.startMs - shift, moved: true }; shift += c.endMs - c.startMs }
      return { at: t - shift, moved: false }
    }
    const duration = Number(version.durationMs)
    const byStart = new Map<number, Json>()
    for (const c of session.chapters) { const m = map(c.startMs); if (m.at < duration) byStart.set(m.at, { ...c, startMs: m.at, originalStartMs: c.startMs, moved: m.moved }) }
    const list: Json[] = [...byStart.values()].sort((a, b) => Number(a.startMs) - Number(b.startMs)).map((c, i, all): Json => ({ ...c, endMs: i + 1 < all.length ? all[i + 1].startMs : duration }))
    if (query.get('format') === 'txt') {
      const ts = (ms: number) => `${String(Math.floor(ms / 60000)).padStart(2, '0')}:${String(Math.floor(ms / 1000) % 60).padStart(2, '0')}`
      return new Response(list.map((c) => `${ts(Number(c.startMs))} ${c.title}\n`).join(''), { headers: { 'Content-Type': 'text/plain; charset=utf-8' } })
    }
    return json(200, list)
  }
  if (rest === 'retakes') return json(409, { detail: '演示模式不支持重录（需要麦克风和真实后端）' })
  return json(404, { detail: '演示模式未模拟该操作' })
}

export function installDemo() {
  const lesson = createSession('课程第 3 讲 · 响度标准化', 'proj_course', SCRIPTS.lesson, 60 * 26, true)
  const pause = lesson.words.findIndex((w, i) => lesson.words[i + 1] && lesson.words[i + 1].startMs - w.endMs > 1000)
  addDecision(lesson, lesson.words[pause].endMs + 250, lesson.words[pause + 1].startMs - 250, 'COMPRESS_SILENCE', '', 'silence_rule', '停顿 1.5 秒，建议缩短为约 0.5 秒。', true)
  void handle('POST', `/api/sessions/${lesson.detail.id}/render`, new URLSearchParams(), { loudnessTarget: 'podcast' })
  createSession('播客试录 · 开场白', 'proj_podcast', SCRIPTS.intro, 45, false)
  const episode = createSession('第 12 期 · 本地优先的音频工作流', 'proj_podcast', SCRIPTS.episode, 5, true)
  const at = (text: string) => episode.words.find((w) => w.text === text)!.startMs
  episode.chapters = [
    { id: uid('ch'), startMs: 0, title: '开场' },
    { id: uid('ch'), startMs: at('为什么'), title: '为什么要本地优先' },
    { id: uid('ch'), startMs: at('下面'), title: '动手流程' },
  ]

  ;(globalThis as Record<string, unknown>).__CLEANCUT_DEMO_COVER__ = (sessionId: string) => covers.get(sessionId) ?? ''
  ;(globalThis as Record<string, unknown>).__CLEANCUT_DEMO_AUDIO__ = (sessionId: string, versionId: string) => {
    const key = `${sessionId}/${versionId}`
    const blob = sessions.get(sessionId)?.audio.get(versionId)
    if (!blob) return ''
    if (!urls.has(key)) urls.set(key, URL.createObjectURL(blob))
    return urls.get(key)!
  }

  const realFetch = window.fetch.bind(window)
  window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(typeof input === 'string' ? input : input instanceof URL ? input.href : input.url, window.location.href)
    const base = new URL(API_BASE, window.location.href)
    if (url.origin !== base.origin || !url.pathname.startsWith('/api/')) return realFetch(input, init)
    let body: Json = {}
    if (typeof init?.body === 'string') { try { body = JSON.parse(init.body) as Json } catch { body = {} } }
    await new Promise((resolve) => window.setTimeout(resolve, 120)) // 模拟一点网络延迟，让加载态可见
    return handle((init?.method ?? 'GET').toUpperCase(), url.pathname, url.searchParams, body, init?.body instanceof FormData ? init.body : undefined)
  }
}
