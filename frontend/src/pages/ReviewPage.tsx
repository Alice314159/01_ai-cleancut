import { RenderTranscriptPanel } from '../components/RenderTranscriptPanel'
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import { RetakeDialog, type RetakeTarget } from '../components/RetakeDialog'
import { Card, Chip, Stat } from '../components/Bits'
import { WarningList } from '../components/WarningList'
import { WaveformRibbon } from '../components/WaveformRibbon'
import { envelope as mockEnvelope } from '../mock/data'
import { ACTION_LABEL, dur, tc } from '../lib/format'
import { api, DecisionConflictError, type SessionDetail } from '../lib/api'
import type { AudioVersion, EditDecision, RenderOutput, TranscriptionJob, TranscriptToken, Warning } from '../types/api'

const FALLBACK_WAVE = mockEnvelope(190, 19)
const PAUSE_THRESHOLD_MS = 400
// 后端现在会尽量把识别到的标点带回来（较新的转写才有）；没有标点的旧转写，
// 再退回用比词间停顿更长的停顿去近似"这句话说完了"。
const SENTENCE_PAUSE_MS = 600
const SENTENCE_END_RE = /[。！？.!?][”’"']?\s*$/
const TOOLS_SECTION_IDS = new Set(['review-output', 'review-advanced', 'review-decisions'])

/** 转写词和"停顿间隙"共用的可选中范围——两者都只需要这三个字段。 */
type SelectableSpan = { jobId?: string | null; startMs: number; endMs?: number | null }

export function ReviewPage({ sessionId, onDataChange, onEnhance }: { sessionId: string; onDataChange: () => void; onEnhance: () => void }) {
  const [retake, setRetake] = useState<RetakeTarget | null>(null)
  const [session, setSession] = useState<SessionDetail | null>(null)
  const [source, setSource] = useState<AudioVersion | null>(null)
  const [decisions, setDecisions] = useState<EditDecision[]>([])
  const [transcript, setTranscript] = useState<TranscriptToken[]>([])
  const [warnings, setWarnings] = useState<Warning[]>([])
  const [waveform, setWaveform] = useState<number[] | null>(null)
  const [rendered, setRendered] = useState<RenderOutput | null>(null)
  const [asrJob, setAsrJob] = useState<TranscriptionJob | null>(null)
  const [suggestionMessage, setSuggestionMessage] = useState('')
  const [language, setLanguage] = useState<'auto' | 'zh' | 'en'>('auto')
  const masterAudioRef = useRef<HTMLAudioElement | null>(null)
  const audioRef = useRef<HTMLAudioElement | null>(null)
  const previewEndRef = useRef<number | null>(null)
  const wordAnchorRef = useRef<SelectableSpan | null>(null)
  const draggingRef = useRef(false)
  const dragStartRef = useRef<SelectableSpan | null>(null)
  const dragMovedRef = useRef(false)
  const suppressClickRef = useRef(false)
  const wordSelectionRef = useRef<{ startMs: number; endMs: number } | null>(null)
  const [filter, setFilter] = useState<'all' | 'review'>('all')
  const [busy, setBusy] = useState(false)
  const busyRef = useRef(false)
  const loadRevisionRef = useRef(0)
  const [conflict, setConflict] = useState<{ current: EditDecision; intended: boolean | null } | null>(null)
  const [smoothCuts, setSmoothCuts] = useState(false)
  const [rendering, setRendering] = useState(false)
  const [loaded, setLoaded] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [startSeconds, setStartSeconds] = useState('')
  const [endSeconds, setEndSeconds] = useState('')
  const [wordSelection, setWordSelection] = useState<{ startMs: number; endMs: number } | null>(null)
  const [toolsOpen, setToolsOpen] = useState(false)
  const toolsRef = useRef<HTMLElement | null>(null)
  const toolsTriggerRef = useRef<HTMLButtonElement | null>(null)
  const [compactTools, setCompactTools] = useState(false)
  useLayoutEffect(() => {
    const root = toolsRef.current?.closest('.app-root')
    if (!root) return
    const observer = new ResizeObserver(([entry]) => setCompactTools(entry.contentRect.width <= 1180))
    setCompactTools(root.clientWidth <= 1180)
    observer.observe(root)
    return () => observer.disconnect()
  }, [])
  useLayoutEffect(() => {
    const panel = toolsRef.current
    if (!panel) return
    panel.inert = compactTools && !toolsOpen
    if (!compactTools || !toolsOpen) return
    const previous = document.activeElement as HTMLElement | null
    const background: { element: HTMLElement; inert: boolean }[] = []
    let branch: HTMLElement = panel
    while (branch.parentElement) {
      for (const sibling of branch.parentElement.children) {
        if (sibling instanceof HTMLElement && sibling !== branch && !sibling.classList.contains('review-tools-backdrop')) {
          background.push({ element: sibling, inert: sibling.inert })
          sibling.inert = true
        }
      }
      branch = branch.parentElement
      if (branch === document.body) break
    }
    panel.querySelector<HTMLButtonElement>('button')?.focus({ preventScroll: true })
    const handleKey = (event: KeyboardEvent) => {
      if (document.querySelector('dialog[open]')) return
      if (event.key === 'Escape') { event.preventDefault(); setToolsOpen(false) }
      if (event.key === 'Tab') {
        const controls = Array.from(panel.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), select:not(:disabled), a[href], summary, audio[controls], [tabindex="0"]'))
          .filter((element) => element.getClientRects().length > 0)
        const first = controls[0], last = controls.at(-1)
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus() }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
      }
    }
    document.addEventListener('keydown', handleKey)
    return () => {
      document.removeEventListener('keydown', handleKey)
      background.forEach(({ element, inert }) => { element.inert = inert })
      if (previous?.isConnected && !panel.contains(previous)) previous.focus({ preventScroll: true })
      else toolsTriggerRef.current?.focus({ preventScroll: true })
    }
  }, [compactTools, toolsOpen])
  const [expandedDecisions, setExpandedDecisions] = useState<Set<string>>(new Set())
  const [hoveredDecisionId, setHoveredDecisionId] = useState<string | null>(null)
  const [previewDecisionId, setPreviewDecisionId] = useState<string | null>(null)
  const [playingTokenId, setPlayingTokenId] = useState<string | null>(null)

  const loadAll = useCallback(async () => {
    const revision = ++loadRevisionRef.current
    const [detail, ds, ts, ws, output, versions, roles, transcriptionJob] = await Promise.all([
      api.getSession(sessionId), api.getDecisions(sessionId), api.getTranscript(sessionId),
      api.getWarnings(sessionId), api.getRender(sessionId), api.getVersions(sessionId), api.getRoles(sessionId), api.getLatestTranscription(sessionId),
    ])
    if (revision !== loadRevisionRef.current) return detail
    setSession(detail); setDecisions(ds); setTranscript(ts); setWarnings(ws); setRendered(output); setAsrJob(transcriptionJob)
    const sourceId = roles.find((role) => role.role === 'RENDER_SOURCE')?.audioVersionId
    setSource(versions.find((version) => version.id === sourceId) ?? null)
    setLoaded(true)
    return detail
  }, [sessionId])

  useEffect(() => {
    let cancelled = false
    let timer: number | undefined
    const load = async () => {
      try {
        const detail = await loadAll()
        if (cancelled) return
        if (detail.captureStatus !== 'IDLE' && !['FINALIZED', 'FAILED'].includes(detail.liveAnalysisStatus)) {
          timer = window.setTimeout(load, 800)
        } else if (detail.liveAnalysisStatus === 'FINALIZED') {
          const wave = await api.getWaveform(sessionId, 190)
          if (!cancelled) setWaveform(wave?.envelope ?? null)
        }
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : '复核数据加载失败')
      }
    }
    void load()
    return () => { cancelled = true; loadRevisionRef.current += 1; window.clearTimeout(timer) }
  }, [loadAll, sessionId])

  const pendingRepetitions = decisions.filter((d) => d.source === 'transcript_rule' && d.action === 'DELETE_REPETITION' && d.accepted === null).length
  const asrJobId = asrJob?.id
  const asrStatus = asrJob?.status
  const asrRunning = !!asrStatus && ['QUEUED', 'LOADING_MODEL', 'TRANSCRIBING'].includes(asrStatus)
  useEffect(() => {
    if (!asrRunning || !asrJobId) return
    let cancelled = false
    let timer: number | undefined
    const poll = async () => {
      try {
        const latest = await api.getTranscription(sessionId, asrJobId)
        if (cancelled) return
        if (latest.status === 'COMPLETED') {
          if (busyRef.current) { timer = window.setTimeout(poll, 300); return }
          const revision = loadRevisionRef.current
          const [tokens, updatedDecisions, output] = await Promise.all([api.getTranscript(sessionId), api.getDecisions(sessionId), api.getRender(sessionId)])
          if (cancelled) return
          if (busyRef.current || revision !== loadRevisionRef.current) { timer = window.setTimeout(poll, 300); return }
          setTranscript(tokens); setDecisions(updatedDecisions); setRendered(output); setSuggestionMessage('')
          wordAnchorRef.current = null; setWordSelection(null)
        }
        setAsrJob(latest)
        if (latest.status === 'COMPLETED' || latest.status === 'FAILED') return
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : '转写进度读取失败，正在重试')
      }
      if (!cancelled) timer = window.setTimeout(poll, 1000)
    }
    timer = window.setTimeout(poll, 300)
    return () => { cancelled = true; window.clearTimeout(timer) }
  }, [asrRunning, asrJobId, sessionId])

  const perform = async (operation: () => Promise<void>) => {
    if (busyRef.current) return
    busyRef.current = true; loadRevisionRef.current += 1; setBusy(true); setError(null)
    try {
      await operation()
    } catch (e) {
      setError(e instanceof Error ? e.message : '操作失败，请重试')
    } finally {
      busyRef.current = false; setBusy(false)
    }
  }

  const saveChoice = async (decision: EditDecision, accepted: boolean | null) => {
    try {
      const updated = await api.updateDecision(sessionId, decision.id, accepted, decision.revision)
      setDecisions((ds) => ds.map((d) => d.id === updated.id ? updated : d))
      setConflict(null)
      setRendered((previous) => previous ? { ...previous, stale: true } : null)
    } catch (e) {
      if (!(e instanceof DecisionConflictError)) throw e
      setConflict({ current: e.current, intended: accepted })
      setDecisions((ds) => ds.map((d) => d.id === e.current.id ? e.current : d))
      setRendered((previous) => previous ? { ...previous, stale: true } : null)
    }
  }

  const set = (id: string, accepted: boolean | null) => perform(async () => {
    const decision = decisions.find((d) => d.id === id)
    if (decision) await saveChoice(decision, accepted)
  })

  // 原音 / 成片 / 各条重录试听共享同一个滚动区域，同时播放会互相干扰，所以一个开始播放就暂停其它的。
  const pauseOtherReviewAudio = (event: { currentTarget: HTMLAudioElement }) => {
    document.querySelectorAll<HTMLAudioElement>('.review-workspace audio').forEach((audio) => {
      if (audio !== event.currentTarget) audio.pause()
    })
  }

  const toggleDecisionDetail = (id: string) => setExpandedDecisions((prev) => {
    const next = new Set(prev)
    if (next.has(id)) next.delete(id); else next.add(id)
    return next
  })

  const choiceLabel = (accepted: boolean | null) => accepted === null ? '待确认' : accepted ? '接受' : '拒绝'

  const handleConfirmAll = () => perform(async () => {
    setDecisions(await api.confirmDecisions(sessionId, decisions))
    setRendered((previous) => previous ? { ...previous, stale: true } : null)
  })

  const handleRejectSuggestions = () => perform(async () => {
    setDecisions(await api.rejectPendingSuggestions(sessionId, decisions))
    setRendered((previous) => previous ? { ...previous, stale: true } : null)
  })

  const previewRange = (startMs: number, endMs: number, decisionId: string | null = null) => {
    const audio = audioRef.current
    if (!audio) return
    previewEndRef.current = endMs / 1000
    audio.currentTime = Math.max(0, startMs / 1000)
    setPreviewDecisionId(decisionId)
    void audio.play().catch(() => {
      previewEndRef.current = null
      setError('试听未能自动开始，请点击播放器。')
    })
  }

  const clearWordSelection = () => {
    wordAnchorRef.current = null
    setWordSelection(null)
    setStartSeconds(''); setEndSeconds('')
  }

  const addRange = (event: FormEvent) => {
    event.preventDefault()
    void perform(async () => {
      const start = Math.round(Number(startSeconds) * 1000)
      const end = Math.round(Number(endSeconds) * 1000)
      if (!startSeconds || !endSeconds || !Number.isFinite(start) || !Number.isFinite(end) || end <= start) {
        throw new Error('请填写有效的开始和结束时间，结束时间必须更晚。')
      }
      const created = await api.addManualDecision(sessionId, start, end)
      setDecisions((ds) => [...ds, created].sort((a, b) => a.startMs - b.startMs))
      setRendered((previous) => previous ? { ...previous, stale: true } : null)
      clearWordSelection()
    })
  }

  const handleDeleteSelectedText = () => perform(async () => {
    if (!wordSelection) return
    const created = await api.addManualDecision(sessionId, wordSelection.startMs, wordSelection.endMs)
    setDecisions((ds) => [...ds, created].sort((a, b) => a.startMs - b.startMs))
    setRendered((previous) => previous ? { ...previous, stale: true } : null)
    clearWordSelection()
  })

  const openRetake = (startMs: number, endMs: number, text = '') => {
    if (!source || !Number.isFinite(startMs) || !Number.isFinite(endMs) || startMs < 0 || endMs <= startMs || endMs > (session?.totalDurationMs ?? 0)) {
      setError('请先选择有效的重录范围，或填写开始和结束时间。')
      return
    }
    audioRef.current?.pause()
    document.querySelectorAll<HTMLAudioElement>('audio').forEach((audio) => audio.pause())
    setRetake({ startMs, endMs, text, sourceId: source.id, decisions })
  }

  const handleRender = () => perform(async () => {
    setRendering(true)
    try {
      setRendered(await api.render(sessionId, smoothCuts))
      onDataChange()
    } finally { setRendering(false) }
  })

  const handleTranscribe = () => perform(async () => {
    setAsrJob(await api.startTranscription(sessionId, language))
  })

  const handleExportMaster = () => perform(async () => {
    if (!rendered) return
    const blob = await api.downloadRenderSubtitles(sessionId, rendered.version.id)
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url; link.download = 'transcript-master.srt'
    document.body.appendChild(link); link.click(); link.remove()
    window.setTimeout(() => URL.revokeObjectURL(url), 1000)
  })

  const handleExportTranscript = (format: 'txt' | 'srt') => perform(async () => {
    const jobId = transcript[0]?.jobId
    if (!jobId) return
    const blob = await api.downloadTranscript(sessionId, jobId, format)
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url; link.download = `transcript-original.${format}`
    document.body.appendChild(link); link.click(); link.remove()
    window.setTimeout(() => URL.revokeObjectURL(url), 1000)
  })

  const handleSuggest = () => perform(async () => {
    const jobId = transcript[0]?.jobId
    if (!jobId) return
    const result = await api.suggestDecisions(sessionId, jobId)
    setDecisions(result.decisions)
    setSuggestionMessage(result.addedCount ? `新增 ${result.addedCount} 条建议，请试听后接受或拒绝。` : '没有新增建议；已处理或重叠的区间会跳过。')
    if (result.addedCount) setRendered((previous) => previous ? { ...previous, stale: true } : null)
  })

  const selectWord = (span: SelectableSpan, extend: boolean, options?: { preview?: boolean }) => {
    if (!span.jobId || span.endMs == null) return
    const anchor = extend ? wordAnchorRef.current : null
    const start = Math.min(anchor?.startMs ?? span.startMs, span.startMs)
    const end = Math.max(anchor?.endMs ?? span.endMs, span.endMs)
    if (!extend || !anchor) wordAnchorRef.current = span
    setStartSeconds((start / 1000).toFixed(3)); setEndSeconds((end / 1000).toFixed(3))
    setWordSelection({ startMs: start, endMs: end })
    if (options?.preview !== false) previewRange(start, end)
  }

  useEffect(() => { wordSelectionRef.current = wordSelection }, [wordSelection])

  // 原音播放时逐词高亮（卡拉OK效果），当前词变化时跟随滚动，方便边听边看。
  useEffect(() => {
    if (!playingTokenId) return
    document.getElementById(`tok-${playingTokenId}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' })
  }, [playingTokenId])

  // Pointer Events 同时覆盖鼠标、触控笔和触屏。拖动途中不试听，松开后才试听一次。
  // 未发生拖动时保留 click：第一次点选起点，第二次点击可直接扩展，手机无需 Shift。
  useEffect(() => {
    const stopDrag = () => {
      if (!draggingRef.current) return
      draggingRef.current = false
      if (dragMovedRef.current) {
        suppressClickRef.current = true
        const sel = wordSelectionRef.current
        if (sel) previewRange(sel.startMs, sel.endMs)
        window.setTimeout(() => { suppressClickRef.current = false }, 0)
      }
      dragStartRef.current = null
      dragMovedRef.current = false
    }
    window.addEventListener('pointerup', stopDrag)
    window.addEventListener('pointercancel', stopDrag)
    return () => {
      window.removeEventListener('pointerup', stopDrag)
      window.removeEventListener('pointercancel', stopDrag)
    }
  }, [])

  const startWordDrag = (span: SelectableSpan, event: {
    button: number
    pointerId: number
    currentTarget: { hasPointerCapture: (id: number) => boolean; releasePointerCapture: (id: number) => void }
  }) => {
    if (event.button !== 0) return
    // Touch pointers are captured by default. Release capture so entering another word
    // produces pointerenter events and uses the same drag path as a mouse.
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId)
    }
    draggingRef.current = true
    dragStartRef.current = span
    dragMovedRef.current = false
  }

  const dragOverWord = (span: SelectableSpan) => {
    if (!draggingRef.current || !dragStartRef.current) return
    if (!dragMovedRef.current) selectWord(dragStartRef.current, false, { preview: false })
    dragMovedRef.current = true
    selectWord(span, true, { preview: false })
  }

  const clickWord = (span: SelectableSpan) => {
    if (suppressClickRef.current) return
    selectWord(span, wordAnchorRef.current !== null)
  }

  const wordSelectionText = useMemo(() => {
    if (!wordSelection) return ''
    return transcript
      .filter((t) => t.jobId && t.endMs != null && t.startMs >= wordSelection.startMs && t.endMs! <= wordSelection.endMs)
      .map((t) => t.text).join('')
  }, [wordSelection, transcript])

  const pending = decisions.filter((d) => !d.hardProtected && d.action !== 'KEEP_PROTECTED' && d.accepted === null)
  const shown = filter === 'review' ? pending : decisions

  const coveredBy = (startMs: number, endMs: number, accepted: boolean | null) =>
    decisions.some((d) => d.accepted === accepted && !d.hardProtected && d.action.startsWith('DELETE_') && d.startMs <= startMs && d.endMs >= endMs)

  // 疑似重复的待定建议单独标记，方便在正文中和其它待定删除（如手动选段）区分开。
  const coveredByRepetition = (startMs: number, endMs: number) =>
    decisions.some((d) => d.accepted === null && d.source === 'transcript_rule' && d.action === 'DELETE_REPETITION' && !d.hardProtected && d.startMs <= startMs && d.endMs >= endMs)

  // 正文 ↔ 决策列表联动：给定一段时间范围，找到覆盖它的那条决策（用于悬停高亮和点击定位）。
  // 重录会在同一个时间范围留下一条已拒绝的旧决策和一条新的 REPLACE_AUDIO 决策；
  // 联动时要认新的那条，不能随便捞到数组里排在前面、其实已经作废的旧决策。
  const decisionForRange = (startMs: number, endMs: number) => {
    const covering = decisions.filter((d) => d.startMs < endMs && d.endMs > startMs)
    return covering.find((d) => d.accepted !== false) ?? covering[0]
  }

  const jumpToDecision = (id: string) => {
    setHoveredDecisionId(id)
    setExpandedDecisions((prev) => { const next = new Set(prev); next.add(id); return next })
    if (filter === 'review' && !pending.some((p) => p.id === id)) setFilter('all')
    setToolsOpen(true)
    window.requestAnimationFrame(() => {
      const decision = document.getElementById(`decision-${id}`)
      decision?.scrollIntoView({ behavior: 'instant', block: 'nearest' })
      decision?.querySelector<HTMLButtonElement>('.decision__row')?.focus({ preventScroll: true })
    })
  }

  const jumpToTranscript = (d: EditDecision) => {
    const anchor = transcript.find((t) => t.jobId && t.endMs != null && t.startMs < d.endMs && t.endMs > d.startMs)
    if (!anchor) return
    window.requestAnimationFrame(() => {
      document.getElementById(`tok-${anchor.id}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' })
    })
  }

  // 每个词/停顿相对决策列表的状态（是否删除、疑似重复、关联哪条决策）只在转写或决策变化时才需要
  // 重新扫描一遍（O(词数×决策数)）。悬停、试听高亮、当前播放词这些高频状态不应该触发这个重算，
  // 不然长录音配合逐词播放高亮会很卡——所以这里单独缓存，渲染时只做轻量的 O(1) 查表。
  const tokenDecisionInfo = useMemo(() => {
    const words = new Map<string, { cut: boolean; pendingCut: boolean; pendingRepeat: boolean; replaced: boolean; decision?: EditDecision }>()
    const pauses = new Map<string, { cut: boolean; pendingCut: boolean; pendingRepeat: boolean; decision?: EditDecision }>()
    transcript.forEach((token, index) => {
      if (token.jobId && token.endMs != null) {
        const replaced = decisions.some((d) => d.accepted && d.action === 'REPLACE_AUDIO' && d.startMs < token.endMs! && token.startMs < d.endMs)
        const cut = coveredBy(token.startMs, token.endMs, true)
        const pendingCut = !cut && coveredBy(token.startMs, token.endMs, null)
        const pendingRepeat = pendingCut && coveredByRepetition(token.startMs, token.endMs)
        const decision = decisionForRange(token.startMs, token.endMs)
        words.set(token.id, { cut, pendingCut, pendingRepeat, replaced, decision })
      }
      const prev = transcript[index - 1]
      const gapMs = prev?.jobId && prev.endMs != null && token.jobId && token.endMs != null ? token.startMs - prev.endMs : 0
      if (gapMs >= PAUSE_THRESHOLD_MS && prev?.endMs != null) {
        const startMs = prev.endMs
        const endMs = token.startMs
        const cut = coveredBy(startMs, endMs, true)
        const pendingCut = !cut && coveredBy(startMs, endMs, null)
        const pendingRepeat = pendingCut && coveredByRepetition(startMs, endMs)
        const decision = decisionForRange(startMs, endMs)
        pauses.set(token.id, { cut, pendingCut, pendingRepeat, decision })
      }
    })
    return { words, pauses }
  }, [transcript, decisions])

  const transcriptParagraphs = useMemo(() => {
    const paragraphs: { token: TranscriptToken; index: number }[][] = []
    let length = 0
    transcript.forEach((token, index) => {
      const previous = transcript[index - 1]
      const gap = previous?.endMs != null ? token.startMs - previous.endMs : 0
      if (!paragraphs.length || gap >= 1500 || (length >= 120 && SENTENCE_END_RE.test(previous?.text ?? '')) || length >= 360) {
        paragraphs.push([]); length = 0
      }
      paragraphs[paragraphs.length - 1].push({ token, index })
      length += token.text.length
    })
    return paragraphs
  }, [transcript])

  // 切出"句子"用来给播放高亮和"试听上下文"定边界。优先认识别带回来的标点（。！？等）——
  // 没有标点的旧转写，才退回用比词间停顿更长的停顿去近似句读。
  const sentenceInfo = useMemo(() => {
    const sentences: { startMs: number; endMs: number }[] = []
    const tokenToSentence = new Map<string, number>()
    transcript.forEach((token, index) => {
      if (!token.jobId || token.endMs == null) return
      const prev = transcript[index - 1]
      const gap = prev?.jobId && prev.endMs != null ? token.startMs - prev.endMs : Infinity
      const prevEndedSentence = !!prev?.jobId && SENTENCE_END_RE.test(prev.text)
      if (!sentences.length || gap >= SENTENCE_PAUSE_MS || prevEndedSentence) sentences.push({ startMs: token.startMs, endMs: token.endMs })
      else sentences[sentences.length - 1].endMs = token.endMs
      tokenToSentence.set(token.id, sentences.length - 1)
    })
    return { sentences, tokenToSentence }
  }, [transcript])

  // "试听上下文"扩到覆盖这条决策的完整句子，而不是前后各拍脑袋加一段固定秒数——
  // 识别本身可能有错，掐头去尾的固定窗口容易听不出前后语境到底对不对。
  // 但识别不出标点，说话人一口气不停顿几十秒时"这句话"会被判断成一大段——
  // 所以每侧再设一个上限，超出部分不再跟着句子边界走，避免试听变成听一整段独白。
  const MAX_SENTENCE_CONTEXT_MS = 6000
  const sentenceRangeForDecision = (d: EditDecision) => {
    const coveredSentences = transcript
      .filter((t) => t.jobId && t.endMs != null && t.startMs < d.endMs && t.endMs > d.startMs)
      .map((t) => sentenceInfo.tokenToSentence.get(t.id))
      .filter((index): index is number => index != null)
    if (!coveredSentences.length) return { startMs: Math.max(0, d.startMs - 1000), endMs: Math.min(totalMs, d.endMs + 2000) }
    const first = sentenceInfo.sentences[Math.min(...coveredSentences)]
    const last = sentenceInfo.sentences[Math.max(...coveredSentences)]
    return {
      startMs: Math.max(0, first.startMs, d.startMs - MAX_SENTENCE_CONTEXT_MS),
      endMs: Math.min(totalMs, last.endMs, d.endMs + MAX_SENTENCE_CONTEXT_MS),
    }
  }

  const totalMs = session?.totalDurationMs ?? 0
  const savedMs = useMemo(() => {
    const intervals = decisions.filter((d) => d.accepted && !d.hardProtected && d.action.startsWith('DELETE_'))
      .map((d) => [Math.max(0, d.startMs), Math.min(totalMs, d.endMs)])
      .filter(([start, end]) => end > start).sort((a, b) => a[0] - b[0])
    let end = 0
    let saved = 0
    for (const [start, nextEnd] of intervals) {
      saved += Math.max(0, nextEnd - Math.max(start, end))
      end = Math.max(end, nextEnd)
    }
    return saved + decisions.filter((d) => d.accepted && d.action === 'REPLACE_AUDIO').reduce((sum, d) => sum + d.endMs - d.startMs - (d.replacementDurationMs ?? 0), 0)
  }, [decisions, totalMs])

  const goToSection = (id: string) => {
    if (id === 'review-decisions' && pending.length) { setFilter('review'); jumpToDecision(pending[0].id); return }
    if (TOOLS_SECTION_IDS.has(id)) setToolsOpen(true)
    window.requestAnimationFrame(() => {
      const section = document.getElementById(id)
      section?.scrollIntoView({ behavior: 'smooth', block: 'start' })
      const control = section?.querySelector<HTMLElement>('select, input:not(:disabled), button:not(:disabled)')
      ;(control ?? section)?.focus({ preventScroll: true })
    })
  }

  const playingSentenceIdx = playingTokenId != null ? sentenceInfo.tokenToSentence.get(playingTokenId) : undefined

  const ready = loaded && session?.liveAnalysisStatus === 'FINALIZED' && !!source
  const nextStep = pending.length
    ? { label: '去确认建议', target: 'review-decisions' }
    : rendered && !rendered.stale
      ? { label: '试听并下载', target: 'review-output' }
      : !transcript.length
        ? { label: '从转写开始', target: 'review-transcript' }
        : decisions.length === 0
          ? { label: '开始选词剪辑', target: 'review-transcript' }
          : { label: '准备生成成片', target: 'review-output' }

  return (
    <>
      {retake && <RetakeDialog sessionId={sessionId} target={retake} onClose={() => setRetake(null)} onSaved={async () => {
        setRendered((previous) => previous ? { ...previous, stale: true } : null)
        clearWordSelection()
        try { await loadAll(); onDataChange() } catch { setError('重录已保存，请刷新数据查看最新剪辑。') }
      }} />}
      {error && <div className="card" role="alert">{error} <button className="btn" disabled={busy} onClick={() => void perform(async () => { await loadAll() })}>刷新数据</button></div>}
      <div className="page-head">
        <div>
          <div className="eyebrow">02 · 剪辑与导出</div>
          <h1>确认剪辑</h1>
          <p>
            试听完整音频，删除不需要的内容，或选中不满意的片段重新录制，再生成成片。时间均以剪辑前的录音为准。
          </p>
        </div>
        <div className="segmented" role="group" aria-label="筛选">
          <button aria-pressed={filter === 'all'} onClick={() => setFilter('all')}>
            全部 {decisions.length}
          </button>
          <button aria-pressed={filter === 'review'} onClick={() => setFilter('review')}>
            待确认 {pending.length}
          </button>
        </div>
      </div>

      <div className="grid grid--3 review-summary">
        <Stat k={savedMs >= 0 ? "预计缩短" : "预计增加"} v={dur(Math.abs(savedMs))} note={totalMs > 0 ? `原长 ${dur(totalMs)}` : undefined} tone={savedMs > 0 ? 'ok' : undefined} />
        <Stat k="待人工确认" v={String(pending.length)} note="先接受或拒绝，再生成成片" tone={pending.length > 0 ? 'warn' : 'ok'} />
        <Stat k="决策总数" v={String(decisions.length)} />
      </div>

      <section className="workflow-guide review-workflow" aria-label="剪辑流程">
        <ol className="workflow-steps">
          <li aria-current={!ready ? 'step' : undefined}>1 · 准备音频{ready ? ' ✓' : ''}</li><li aria-current={ready && decisions.length === 0 ? 'step' : undefined}>2 · 转写与选段</li>
          <li aria-current={pending.length ? 'step' : undefined}>3 · 确认剪辑</li><li aria-current={ready && decisions.length > 0 && !pending.length ? 'step' : undefined}>4 · 试听导出</li>
        </ol>
        <p role="status">{!ready ? (session?.liveAnalysisStatus === 'FAILED' ? '音频分析失败，请重新录制或导入。' : '正在准备音频，请等待分析完成。') : asrRunning ? '正在转写，你可以先试听音频。' : conflict ? '请先处理下方的选择冲突。' : pending.length ? <>还有 <button type="button" className="pill-badge pill-badge--warn" onClick={() => { setFilter('review'); goToSection('review-decisions') }}>{pending.length} 条待确认</button> 建议，接受后才会删除。</> : rendered && !rendered.stale ? '成片已生成，试听满意后下载音频和字幕。' : !transcript.length ? '先转写文字，再直接选择需要删除的词句；也可以用秒数手动剪辑。' : decisions.length === 0 ? '转写已完成。点击文字选中要删除的内容，或生成剪辑建议。' : '选择已确认，可以生成成片了。'}</p>
        <div className="workflow-actions">
          <button key={nextStep.target} data-target={nextStep.target} className="btn btn--primary" disabled={!ready || busy || asrRunning || !!conflict}
            onClick={(event) => {
              const target = event.currentTarget.dataset.target!
              if (target === 'review-decisions') setFilter('review')
              goToSection(target)
            }}>{nextStep.label}</button>
          {!transcript.length && decisions.length === 0 && ready && (
            <button className="btn" disabled={busy || asrRunning} onClick={handleRender}>
              跳过转写，导出完整音频
            </button>
          )}
          <button className="btn btn--link" onClick={onEnhance} disabled={busy || asrRunning}>需要降噪？调整音质</button>
        </div>
      </section>
      {conflict && <Card title="剪辑选择冲突">
        <p role="alert">“{conflict.current.text}”：最新选择为{choiceLabel(conflict.current.accepted)}，你的选择为{choiceLabel(conflict.intended)}。</p>
        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
          <button className="btn" disabled={busy} onClick={() => void perform(async () => { await loadAll(); setConflict(null) })}>采用最新选择</button>
          <button className="btn" disabled={busy} onClick={() => void perform(() => saveChoice(conflict.current, conflict.intended))}>应用我的选择</button>
          <button className="btn" disabled={busy} onClick={() => void perform(() => saveChoice(conflict.current, null))}>改为待确认</button>
          <button className="btn" disabled={busy} onClick={() => setConflict(null)}>暂不处理</button>
        </div>
        <p className="card__hint">暂不处理不会写入；列表已显示服务器返回的选择。再次提交仍会检查是否有新修改。</p>
      </Card>}
      {!ready && <div className="card" role="status">{session?.liveAnalysisStatus === 'FAILED' ? '音频分析失败，请重新录制或重新导入音频。' : '请先完成录制或导入音频，分析就绪后即可剪辑。'}</div>}

      <div className="review-workspace">
        <div className="review-transcript-column">
          <Card id="review-transcript" title="语音转写" hint="音频在本机处理。首次使用会下载模型；时间戳为模型估计，删除前请试听核对。">
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
              <label>识别语言 <select aria-label="识别语言" value={language} disabled={busy || asrRunning} onChange={(event) => setLanguage(event.target.value as 'auto' | 'zh' | 'en')}>
                <option value="auto">自动识别</option><option value="zh">中文</option><option value="en">英文</option>
              </select></label>
              <button className={transcript.length ? 'btn' : 'btn btn--primary'} onClick={handleTranscribe} disabled={busy || !ready || asrRunning}>
                {asrRunning ? '转写进行中…' : '开始本机转写'}
              </button>
            </div>
            {transcript.length > 0 && <>
              <button className="btn" onClick={handleSuggest} disabled={busy || !ready || asrRunning || transcript.some((token) => !token.jobId || token.endMs == null)}>查找重复与语气词</button>
              <p className="card__hint">相邻重复会自动提示，语气词可手动查找；建议需试听确认。</p>
            </>}
            {pendingRepetitions > 0 && <button className="pill-badge pill-badge--warn" onClick={() => goToSection('review-decisions')}>疑似重复 {pendingRepetitions} 处 · 复核 →</button>}
            {suggestionMessage && <p role="status">{suggestionMessage}</p>}
            {asrJob && <p role="status">{
              asrJob.status === 'QUEUED' ? '等待本机转写…' :
              asrJob.status === 'LOADING_MODEL' ? '正在加载模型；首次下载可能需要几分钟…' :
              asrJob.status === 'TRANSCRIBING' ? `正在转写 ${Math.round(asrJob.progress * 100)}%` :
              asrJob.status === 'FAILED' ? asrJob.errorMessage :
              `转写完成 · ${asrJob.detectedLanguage ?? '未知语言'}`
            }</p>}
            {transcript.length === 0 && <p className="card__hint">{asrJob?.status === 'COMPLETED' ? '未识别到可转写的语音。' : '转写完成后，点击起点和终点，或拖过多个词来选择，再点“删除选中文字”。词间停顿也可选择删除。'}</p>}
            {transcript.some((token) => !token.jobId) && <p className="card__hint">未标记时间的文字来自历史候选，不是语音识别结果。</p>}
            {transcript.some((token) => token.jobId) && <details className="transcript-help"><summary>选词与剪辑说明</summary><p className="card__hint">点击一个词确定起点，再点另一词扩展范围；也可直接拖选。手机和电脑操作一致。“‧‧‧0.8s”表示可选的词间停顿。选中后可删除或重录；导出转写仍对应剪辑前录音。</p></details>}
            {transcript.length > 0 && <details className="export-menu">
              <summary className="btn btn--link">导出转写 ▾</summary>
              <div className="export-menu__list" role="menu">
                {(['txt', 'srt'] as const).map((format) => <button key={format} role="menuitem"
                  disabled={busy || asrRunning || !transcript.length || transcript.some((t) => !t.jobId || t.endMs == null)}
                  onClick={(event) => { handleExportTranscript(format); event.currentTarget.closest('details')?.removeAttribute('open') }}>{format.toUpperCase()} 文本</button>)}
              </div>
            </details>}

            <div className="transcript">
              {transcriptParagraphs.map((paragraph) => <p className="transcript-paragraph" key={paragraph[0].token.id}>{paragraph.map(({ token, index }) => {
                const prev = transcript[index - 1]
                const gapMs = prev?.jobId && prev.endMs != null && token.jobId && token.endMs != null
                  ? token.startMs - prev.endMs : 0
                const pause: SelectableSpan | null = gapMs >= PAUSE_THRESHOLD_MS
                  ? { jobId: token.jobId, startMs: prev.endMs!, endMs: token.startMs } : null
                const wordInfo = tokenDecisionInfo.words.get(token.id)
                const pauseInfo = pause ? tokenDecisionInfo.pauses.get(token.id) : undefined
                const replaced = !!wordInfo?.replaced
                const cut = !!wordInfo?.cut
                const pendingCut = !!wordInfo?.pendingCut
                const pendingRepeat = !!wordInfo?.pendingRepeat
                const pauseCut = !!pauseInfo?.cut
                const pausePendingCut = !!pauseInfo?.pendingCut
                const pausePendingRepeat = !!pauseInfo?.pendingRepeat
                const tokenSelected = token.endMs != null && !!wordSelection && token.startMs >= wordSelection.startMs && token.endMs <= wordSelection.endMs
                const pauseSelected = !!pause && !!wordSelection && pause.startMs >= wordSelection.startMs && pause.endMs! <= wordSelection.endMs
                const tokenDecision = wordInfo?.decision
                const pauseDecision = pauseInfo?.decision
                const tokenLinked = !!tokenDecision && (hoveredDecisionId === tokenDecision.id || previewDecisionId === tokenDecision.id)
                const pauseLinked = !!pauseDecision && (hoveredDecisionId === pauseDecision.id || previewDecisionId === pauseDecision.id)
                const tokenPlaying = token.id === playingTokenId
                const tokenSentencePlaying = playingSentenceIdx != null && sentenceInfo.tokenToSentence.get(token.id) === playingSentenceIdx
                const pauseSentencePlaying = !!pause && playingSentenceIdx != null && sentenceInfo.tokenToSentence.get(token.id) === playingSentenceIdx
                return (
                  <span key={token.id} style={{ display: 'contents' }}>
                    {pause && (
                      <button
                        className={`transcript-pause${pauseCut ? ' tok--cut' : ''}${pausePendingCut ? ' tok--pending-cut' : ''}${pausePendingRepeat ? ' tok--pending-repeat' : ''}${pauseSelected ? ' tok--selected' : ''}${pauseLinked ? ' tok--linked' : ''}${pauseSentencePlaying ? ' tok--sentence-playing' : ''}`}
                        disabled={busy}
                        title={`停顿 ${(gapMs / 1000).toFixed(1)} 秒 · 点击或拖选可一并试听/删除`}
                        onPointerDown={(event) => startWordDrag(pause, event)}
                        onPointerEnter={() => dragOverWord(pause)}
                        onMouseEnter={() => { if (pauseDecision) setHoveredDecisionId(pauseDecision.id) }}
                        onMouseLeave={() => setHoveredDecisionId((id) => (id === pauseDecision?.id ? null : id))}
                        onClick={() => { clickWord(pause); if (pauseDecision) jumpToDecision(pauseDecision.id) }}
                      >
                        ‧‧‧{(gapMs / 1000).toFixed(1)}s
                      </button>
                    )}
                    {token.jobId && token.endMs != null ? (
                      <button id={`tok-${token.id}`} className={`transcript-word${replaced ? ' tok--replaced' : ''}${cut ? ' tok--cut' : ''}${pendingCut ? ' tok--pending-cut' : ''}${pendingRepeat ? ' tok--pending-repeat' : ''}${tokenSelected ? ' tok--selected' : ''}${tokenLinked ? ' tok--linked' : ''}${tokenSentencePlaying ? ' tok--sentence-playing' : ''}${tokenPlaying ? ' tok--playing' : ''}`} disabled={busy}
                        title={`${tc(token.startMs)} – ${tc(token.endMs)}${pendingCut ? ' · 有待确认的删除建议' : ''}${replaced ? ' · 此处已应用重录，文字仍为原录音' : ''} · 点击起点和终点，或拖动跨词选择`}
                        onPointerDown={(event) => startWordDrag(token, event)}
                        onPointerEnter={() => dragOverWord(token)}
                        onMouseEnter={() => { if (tokenDecision) setHoveredDecisionId(tokenDecision.id) }}
                        onMouseLeave={() => setHoveredDecisionId((id) => (id === tokenDecision?.id ? null : id))}
                        onClick={() => { clickWord(token); if (tokenDecision) jumpToDecision(tokenDecision.id) }}>{token.text}</button>
                    ) : <span>{token.text}</span>}
                  </span>
                )
              })}</p>)}
            </div>
            {wordSelection && (
              <div className="word-selection-bar" style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap', marginTop: 8, padding: 8, background: 'var(--surface-2)', borderRadius: 'var(--radius)' }}>
                <span className="card__hint" style={{ margin: 0 }}>
                  已选中「{wordSelectionText}」· {tc(wordSelection.startMs)} – {tc(wordSelection.endMs)}
                </span>
                <button className="btn btn--primary" disabled={busy} onClick={handleDeleteSelectedText}>删除选中文字</button>
                <button className="btn" disabled={busy || !ready || asrRunning} onClick={() => openRetake(wordSelection.startMs, wordSelection.endMs, wordSelectionText)}>重录选中片段</button>
                <button className="btn btn--link" disabled={busy} onClick={clearWordSelection}>取消选择</button>
              </div>
            )}
          </Card>
        </div>
        {toolsOpen && <div className="review-tools-backdrop" onClick={() => setToolsOpen(false)} />}
        <aside ref={toolsRef} id="review-tools-panel" role={compactTools ? "dialog" : undefined} aria-modal={compactTools && toolsOpen ? true : undefined} aria-hidden={compactTools && !toolsOpen ? true : undefined} className={`review-tools-column${toolsOpen ? ' review-tools-column--open' : ''}`} aria-label="试听与剪辑工具">
          <div className="review-tools-drawer-head">
            <strong>试听与剪辑工具</strong>
            <button type="button" className="btn btn--link" onClick={() => setToolsOpen(false)}>收起 ✕</button>
          </div>
      <Card id="review-output" title="试听与生成" hint="先试听剪辑前音频。确认删除内容后生成成片；重叠区间只删除一次。">
        {source && <audio ref={audioRef} onTimeUpdate={(event) => {
          if (previewEndRef.current != null && event.currentTarget.currentTime >= previewEndRef.current) {
            event.currentTarget.pause()
            previewEndRef.current = null
          }
          const currentMs = event.currentTarget.currentTime * 1000
          const active = transcript.find((t) => t.jobId && t.endMs != null && t.startMs <= currentMs && currentMs < t.endMs)
          setPlayingTokenId((prev) => { const next = active?.id ?? null; return next === prev ? prev : next })
        }} onPlay={pauseOtherReviewAudio} onPause={() => { previewEndRef.current = null; setPreviewDecisionId(null); setPlayingTokenId(null) }} aria-label="剪辑前试听" controls preload="metadata" src={api.audioUrl(sessionId, source.id)} style={{ width: '100%' }} />}
        <div className="render-actions">
          <label style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            <input type="checkbox" checked={smoothCuts} disabled={busy} onChange={(event) => setSmoothCuts(event.target.checked)} />
            平滑剪辑
          </label>
          <button className="btn btn--primary" disabled={busy || !ready || pending.length > 0 || !!conflict} onClick={handleRender}>
            {rendering ? '渲染中…' : '生成剪辑音频'}
          </button>
        </div>
        {pending.length > 0 && <button className="review-pending-pill" onClick={() => goToSection('review-decisions')}>⚠ 待确认 {pending.length} 条 · 去处理 →</button>}

        {rendered && (
          <div style={{ marginTop: 12 }}>
            <p role="status">{rendered.stale ? '选择或音频源已变化，下方是上一次成片，请重新生成。' : '成片已就绪。'} 时长 {(rendered.version.durationMs / 1000).toFixed(3)} 秒，{rendered.removedDurationMs >= 0 ? '缩短' : '增加'} {Math.abs(rendered.removedDurationMs / 1000).toFixed(3)} 秒，{rendered.version.engine.endsWith('_fade') ? '已启用平滑剪辑' : '直接拼接'}。</p>
            {rendered.hasReplacements && <p className="card__hint">此成片包含重录，生成后会自动转写成片，完成后可直接下载匹配的字幕。</p>}
            <audio ref={masterAudioRef} aria-label="成片试听" key={rendered.version.id} controls preload="metadata" onPlay={pauseOtherReviewAudio} src={api.audioUrl(sessionId, rendered.version.id)} style={{ width: '100%' }} />
            <div style={{ display: 'flex', gap: 8, marginTop: 8 }}>
              <a className="btn" href={api.downloadUrl(sessionId, rendered.version.id)}>{rendered.stale ? '下载上一次成片 WAV' : '下载成片 WAV'}</a>
              {!rendered.hasReplacements && <button className="btn" disabled={busy} onClick={handleExportMaster}>{rendered.stale ? '导出上一次成片 SRT' : '导出成片 SRT'}</button>}
            </div>
            {rendered.hasReplacements && <RenderTranscriptPanel key={rendered.version.id} sessionId={sessionId} versionId={rendered.version.id} audioRef={masterAudioRef} stale={rendered.stale} />}
          </div>
        )}
      </Card>

          <Card id="review-advanced">
        <details className="advanced-editor">
          <summary>高级剪辑：按时间输入删除区间或重录</summary>
          <form onSubmit={addRange} className="advanced-editor__form">
            <label className="cut-field">开始（秒）<input aria-label="开始时间（秒）" type="number" min="0" max={totalMs / 1000} step="0.001" required value={startSeconds} onChange={(event) => setStartSeconds(event.target.value)} disabled={busy || !ready} /></label>
            <label className="cut-field">结束（秒）<input aria-label="结束时间（秒）" type="number" min="0.001" max={totalMs / 1000} step="0.001" required value={endSeconds} onChange={(event) => setEndSeconds(event.target.value)} disabled={busy || !ready} /></label>
            <button className="btn" type="submit" disabled={busy || !ready}>添加删除区间</button>
            <button className="btn" type="button" disabled={busy || !ready || asrRunning || !startSeconds || !endSeconds} onClick={() => openRetake(Math.round(Number(startSeconds) * 1000), Math.round(Number(endSeconds) * 1000))}>重录此时间范围</button>
          </form>
        </details>
          </Card>
        {decisions.length > 0 && (
        <Card
          id="review-decisions"
          title="剪辑决策"
          hint="点击一行展开详情；聚焦某条决策后按 A 接受、R 拒绝，无需逐个点击。"
          action={
            <div className="decision-batch-acts">
              <button
                className={pending.length > 0 ? 'btn btn--primary' : 'btn'}
                onClick={handleConfirmAll}
                disabled={busy || pending.length === 0}
              >
                确认全部待定项{pending.length > 0 ? ` (${pending.length})` : ''}
              </button>
              <button className="btn" onClick={handleRejectSuggestions}
                disabled={busy || !pending.some((d) => d.source === 'transcript_rule')}>
                拒绝待定建议
              </button>
            </div>
          }
        >
          {shown.length === 0 && <p className="card__hint">没有符合筛选条件的决策。</p>}
          <div className="decision-list">
          {shown.map((d) => {
            const expanded = expandedDecisions.has(d.id)
            const linked = hoveredDecisionId === d.id || previewDecisionId === d.id
            return (
              <div className={`decision${expanded ? ' decision--expanded' : ''}${linked ? ' decision--linked' : ''}`} key={d.id} id={`decision-${d.id}`}
                onKeyDown={(event) => {
                  if (busy || d.hardProtected) return
                  const key = event.key.toLowerCase()
                  if (key !== 'a' && key !== 'r') return
                  event.preventDefault()
                  const next = event.currentTarget.nextElementSibling as HTMLElement | null
                  set(d.id, key === 'a' ? (d.accepted === true ? null : true) : (d.accepted === false ? null : false))
                  window.requestAnimationFrame(() => next?.querySelector<HTMLElement>('.decision__row')?.focus())
                }}
              >
                <button
                  type="button"
                  className="decision__row"
                  aria-expanded={expanded}
                  onClick={() => { toggleDecisionDetail(d.id); jumpToTranscript(d) }}
                  onMouseEnter={() => setHoveredDecisionId(d.id)}
                  onMouseLeave={() => setHoveredDecisionId((id) => (id === d.id ? null : id))}
                >
                  <span className="decision__tc">
                    {tc(d.startMs)}<span className="decision__dur">+{((d.endMs - d.startMs) / 1000).toFixed(1)}s</span>
                  </span>
                  <span className="decision__snippet">
                    <Chip tone={d.action === 'KEEP_PROTECTED' ? 'protect' : undefined}>{ACTION_LABEL[d.action]}</Chip>
                    {d.accepted === null && d.reviewRequired && <Chip tone="provisional">待确认</Chip>}
                    <span className="decision__snippet-text">{d.text}</span>
                  </span>
                  <span className="decision__chevron" aria-hidden="true">{expanded ? '▾' : '▸'}</span>
                </button>

                <div className="decision__quickacts">
                  <button
                    className="iconbtn"
                    aria-pressed={d.accepted === true}
                    aria-label="接受（快捷键 A）"
                    title="接受（快捷键 A）"
                    disabled={busy || d.hardProtected}
                    onClick={() => set(d.id, d.accepted === true ? null : true)}
                  >
                    ✓
                  </button>
                  <button
                    className="iconbtn"
                    aria-pressed={d.accepted === false}
                    aria-label="拒绝（快捷键 R）"
                    title="拒绝（快捷键 R）"
                    disabled={busy || d.hardProtected}
                    onClick={() => set(d.id, d.accepted === false ? null : false)}
                  >
                    ✕
                  </button>
                </div>

                {expanded && (
                  <div className="decision__detail">
                    <div style={{ display: 'flex', gap: 6, alignItems: 'center', flexWrap: 'wrap' }}>
                      {d.hardProtected && <Chip tone="protect">需先解除保护</Chip>}
                      <span className="mono" style={{ fontSize: 'var(--step--1)', color: 'var(--text-faint)' }}>
                        {d.source === 'transcript_rule' ? '文字规则建议' : d.confidence.toFixed(2)}
                      </span>
                    </div>
                    <p className="decision__why">{d.reason}</p>
                    {d.replacementVersionId && <>
                      <p className="card__hint">新录音 {((d.replacementDurationMs ?? 0) / 1000).toFixed(3)} 秒 · {d.accepted ? '已应用，可撤销恢复原片段' : '未应用'}</p>
                      <audio aria-label="已保存的重录试听" controls preload="none" src={api.audioUrl(sessionId, d.replacementVersionId)}
                        onPlay={(event) => { pauseOtherReviewAudio(event); setPreviewDecisionId(d.id) }}
                        onPause={() => setPreviewDecisionId((id) => (id === d.id ? null : id))}
                        style={{ width: '100%', minWidth: 0 }} />
                      <button className="btn" disabled={busy} onClick={() => set(d.id, !d.accepted)}>{d.accepted ? '撤销重录' : '使用此重录'}</button>
                    </>}
                    <div className="decision__acts">
                      {!d.hardProtected && d.action !== 'KEEP_PROTECTED' && <button className="btn" disabled={busy || !ready || asrRunning} onClick={() => openRetake(d.startMs, d.endMs, d.text)}>重录这段</button>}
                      <button className="btn" disabled={!source} onClick={() => {
                        const range = sentenceRangeForDecision(d)
                        previewRange(range.startMs, range.endMs, d.id)
                        jumpToTranscript(d)
                      }}>试听上下文</button>
                    </div>
                  </div>
                )}
              </div>
            )
          })}
          </div>
        </Card>
        )}

        <Card title="本次处理的提醒">
          <WarningList warnings={warnings} />
        </Card>
        </aside>
        {(compactTools || !toolsOpen) && (
          <button ref={toolsTriggerRef} type="button" aria-expanded={toolsOpen} aria-controls="review-tools-panel" className={`review-tools-fab${pending.length > 0 ? ' review-tools-fab--warn' : ''}`} onClick={() => { if (pending.length) { setFilter('review'); jumpToDecision(pending[0].id) } else setToolsOpen(true) }}>
            {pending.length > 0 ? `待确认 ${pending.length} 条` : '试听与生成'} <span aria-hidden="true">▲</span>
          </button>
        )}
      </div>


      {totalMs > 0 && (
        <div style={{ margin: 'var(--sp-4) 0' }}>
          <WaveformRibbon
            title="时间线 · 判定"
            hint={waveform ? '波形来自真实录音振幅' : '波形为占位曲线（未取到真实录音数据）'}
            envelope={waveform ?? FALLBACK_WAVE}
            durationMs={totalMs}
            decisions={decisions}
          />
        </div>
      )}
    </>
  )
}
