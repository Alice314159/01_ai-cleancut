import { RenderTranscriptPanel } from '../components/RenderTranscriptPanel'
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import { RetakeDialog, type RetakeTarget } from '../components/RetakeDialog'
import { ChapterPanel } from '../components/ChapterPanel'
import { CoverPicker } from '../components/CoverPicker'
import { Card, Chip } from '../components/Bits'
import { isBoolean, oneOf, usePref } from '../lib/prefs'
import { makeEntry, planRestore, push, type HistoryEntry } from '../lib/history'
import { WarningList } from '../components/WarningList'
import { WaveformRibbon } from '../components/WaveformRibbon'
import { envelope as mockEnvelope } from '../mock/data'
import { ACTION_LABEL, dur, isCutAction, LOUDNESS_TARGET_LABEL, loudnessSummary, tc } from '../lib/format'
import { api, ApiError, DecisionConflictError, EXPORT_BITRATES, type ExportFormat, type SessionDetail } from '../lib/api'
import type { AudioVersion, Chapter, EditDecision, MappedChapter, LoudnessTarget, RenderOutput, TranscriptionJob, TranscriptToken, Warning } from '../types/api'

const FALLBACK_WAVE = mockEnvelope(190, 19)
const PAUSE_THRESHOLD_MS = 400
// 后端现在会尽量把识别到的标点带回来（较新的转写才有）；没有标点的旧转写，
// 再退回用比词间停顿更长的停顿去近似"这句话说完了"。
const SENTENCE_PAUSE_MS = 600
const LANGUAGE_LABEL: Record<string, string> = { zh: '中文', en: '英文', ja: '日文', ko: '韩文' }
const SENTENCE_END_RE = /[。！？.!?][”’"']?\s*$/
const TOOLS_SECTION_IDS = new Set(['review-advanced', 'review-decisions', 'review-chapters'])

/** 转写词和"停顿间隙"共用的可选中范围——两者都只需要这三个字段。 */
type SelectableSpan = { jobId?: string | null; startMs: number; endMs?: number | null }

export function ReviewPage({ sessionId, onDataChange, onEnhance }: { sessionId: string; onDataChange: () => void; onEnhance: () => void }) {
  const [retake, setRetake] = useState<RetakeTarget | null>(null)
  const [session, setSession] = useState<SessionDetail | null>(null)
  const [source, setSource] = useState<AudioVersion | null>(null)
  const [decisions, setDecisions] = useState<EditDecision[]>([])
  // 撤销 / 重做。decisionsRef 在提交时立即更新，保证连续操作计算差异时拿到的是最新状态。
  const decisionsRef = useRef<EditDecision[]>([])
  const [undoStack, setUndoStack] = useState<HistoryEntry[]>([])
  const [redoStack, setRedoStack] = useState<HistoryEntry[]>([])
  const [historyToast, setHistoryToast] = useState<{ text: string; canUndo: boolean } | null>(null)
  const toastTimerRef = useRef<number | undefined>(undefined)
  const showToast = (text: string, canUndo: boolean) => {
    window.clearTimeout(toastTimerRef.current)
    setHistoryToast({ text, canUndo })
    toastTimerRef.current = window.setTimeout(() => setHistoryToast(null), 6000)
  }
  /** 提交新的决策列表；给 label 时记入撤销历史并清空重做。 */
  const applyDecisions = (next: EditDecision[], label?: string) => {
    if (label) {
      const entry = makeEntry(label, decisionsRef.current, next)
      if (entry) { setUndoStack((stack) => push(stack, entry)); setRedoStack([]); showToast(label, true) }
    }
    decisionsRef.current = next
    setDecisions(next)
  }
  /** 从服务器整体重载：之前的历史可能已对不上，清空。 */
  const resetDecisions = (next: EditDecision[]) => {
    decisionsRef.current = next
    setDecisions(next); setUndoStack([]); setRedoStack([])
  }
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
  // 生成与导出选项记在本机，下次打开默认带出。
  const [smoothCuts, setSmoothCuts] = usePref('smoothCuts', false, isBoolean)
  const [loudnessTarget, setLoudnessTarget] = usePref<LoudnessTarget | ''>('loudnessTarget', '', oneOf('', 'podcast', 'broadcast'))
  const [minPauseMs, setMinPauseMs] = usePref('minPauseMs', 1500, oneOf(1000, 1500, 2000, 3000))
  const [pauseMessage, setPauseMessage] = useState('')
  const [exportBitrate, setExportBitrate] = usePref<number>('exportBitrate', 192, oneOf<number>(...EXPORT_BITRATES))
  const [exporting, setExporting] = useState<ExportFormat | null>(null)
  const [chapters, setChapters] = useState<Chapter[]>([])
  const [chapterText, setChapterText] = useState<string | null>(null)
  const [chapterCopied, setChapterCopied] = useState(false)
  // 成片时间轴上的章节（后端按这版成片的剪辑快照换算），用于导出前核对。
  const [renderChapters, setRenderChapters] = useState<MappedChapter[] | null>(null)
  const [titleDraft, setTitleDraft] = useState<string | null>(null)
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
    const [detail, ds, ts, ws, output, versions, roles, transcriptionJob, chapterList] = await Promise.all([
      api.getSession(sessionId), api.getDecisions(sessionId), api.getTranscript(sessionId),
      api.getWarnings(sessionId), api.getRender(sessionId), api.getVersions(sessionId), api.getRoles(sessionId), api.getLatestTranscription(sessionId),
      api.listChapters(sessionId),
    ])
    if (revision !== loadRevisionRef.current) return detail
    setSession(detail); resetDecisions(ds); setTranscript(ts); setWarnings(ws); setRendered(output); setAsrJob(transcriptionJob); setChapters(chapterList)
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
          setTranscript(tokens); resetDecisions(updatedDecisions); setRendered(output); setSuggestionMessage('')
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
      const verb = accepted === true ? (isCutAction(decision.action) ? '确认删除' : '接受') : accepted === false ? '拒绝' : '改为待确认'
      applyDecisions(decisionsRef.current.map((d) => d.id === updated.id ? updated : d), `${verb}${decision.text ? `「${decision.text.slice(0, 12)}」` : ` ${ACTION_LABEL[decision.action] ?? ''}`}`)
      setConflict(null)
      setRendered((previous) => previous ? { ...previous, stale: true } : null)
    } catch (e) {
      if (!(e instanceof DecisionConflictError)) throw e
      setConflict({ current: e.current, intended: accepted })
      applyDecisions(decisionsRef.current.map((d) => d.id === e.current.id ? e.current : d))
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
    applyDecisions(await api.confirmDecisions(sessionId, decisions), '确认全部待定项')
    setRendered((previous) => previous ? { ...previous, stale: true } : null)
  })

  const handleRejectSuggestions = () => perform(async () => {
    applyDecisions(await api.rejectPendingSuggestions(sessionId, decisions), '拒绝待定建议')
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
      applyDecisions([...decisionsRef.current, created].sort((a, b) => a.startMs - b.startMs), `删除 ${tc(start)}–${tc(end)}`)
      setRendered((previous) => previous ? { ...previous, stale: true } : null)
      clearWordSelection()
    })
  }

  const handleDeleteSelectedText = () => perform(async () => {
    if (!wordSelection) return
    const created = await api.addManualDecision(sessionId, wordSelection.startMs, wordSelection.endMs)
    applyDecisions([...decisionsRef.current, created].sort((a, b) => a.startMs - b.startMs), `删除「${(wordSelectionText || '选中片段').slice(0, 16)}」`)
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
      setRendered(await api.render(sessionId, smoothCuts, loudnessTarget || null))
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

  const handleExportAudio = (format: ExportFormat) => perform(async () => {
    if (!rendered) return
    setExporting(format)
    try {
      const { blob, filename } = await api.exportAudio(sessionId, rendered.version.id, format, exportBitrate)
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url; link.download = filename
      document.body.appendChild(link); link.click(); link.remove()
      window.setTimeout(() => URL.revokeObjectURL(url), 1000)
    } finally { setExporting(null) }
  })

  const runHistory = (direction: 'undo' | 'redo') => perform(async () => {
    const stack = direction === 'undo' ? undoStack : redoStack
    const entry = stack[stack.length - 1]
    if (!entry) return
    const [to, from] = direction === 'undo' ? [entry.before, entry.after] : [entry.after, entry.before]
    try {
      const next = await api.restoreDecisions(sessionId, planRestore(to, from, decisionsRef.current))
      decisionsRef.current = next
      setDecisions(next)
      if (direction === 'undo') { setUndoStack((s) => s.slice(0, -1)); setRedoStack((s) => [...s, entry]) }
      else { setRedoStack((s) => s.slice(0, -1)); setUndoStack((s) => [...s, entry]) }
      setRendered((previous) => previous ? { ...previous, stale: true } : null)
      setConflict(null); setSuggestionMessage(''); setPauseMessage('')
      showToast(`${direction === 'undo' ? '已撤销' : '已重做'}：${entry.label}`, false)
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        await loadAll()
        throw new Error(`${e.message}（撤销记录已清空）`)
      }
      throw e
    }
  })

  const handleRename = () => perform(async () => {
    const title = titleDraft?.trim()
    if (!title || !session) return
    if (title !== session.title) {
      setSession(await api.updateSession(sessionId, { title }))
      onDataChange()
    }
    setTitleDraft(null)
  })

  const handleChapterFromSelection = () => perform(async () => {
    if (!wordSelection) return
    const title = wordSelectionText.replace(/[，。！？、,.!?\s]+$/, '').slice(0, 30) || `第 ${chapters.length + 1} 章`
    const created = await api.createChapter(sessionId, wordSelection.startMs, title)
    setChapters((list) => [...list, created].sort((a, b) => a.startMs - b.startMs))
    clearWordSelection()
  })

  const handleCopyChapters = () => perform(async () => {
    if (!rendered) return
    const text = await api.versionChaptersText(sessionId, rendered.version.id)
    if (!text.trim()) throw new Error('这个成片里没有章节。先在“章节”中添加，再重新生成或直接复制。')
    try {
      await navigator.clipboard.writeText(text)
      setChapterText(null)
      setError(null)
      setChapterCopied(true)
      window.setTimeout(() => setChapterCopied(false), 2000)
    } catch {
      setChapterText(text) // 剪贴板不可用时直接展示，方便手动复制
    }
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

  const handleSuggestPauses = () => perform(async () => {
    // 保留时长取停顿阈值的三分之一，夹在 0.3–0.8 秒之间：句间仍有自然的呼吸感。
    const keepMs = Math.min(800, Math.max(300, Math.round(minPauseMs / 3)))
    const result = await api.suggestPauses(sessionId, minPauseMs, keepMs)
    applyDecisions(result.decisions, '查找过长停顿')
    setPauseMessage(result.addedCount
      ? `找到 ${result.addedCount} 处过长停顿，已加入待确认，每处保留约 ${(keepMs / 1000).toFixed(1)} 秒。`
      : '没有找到新的过长停顿；已有决策覆盖的区间会跳过，底噪过大时也无法可靠判断。')
    if (result.addedCount) setRendered((previous) => previous ? { ...previous, stale: true } : null)
  })

  const handleSuggest = () => perform(async () => {
    const jobId = transcript[0]?.jobId
    if (!jobId) return
    const result = await api.suggestDecisions(sessionId, jobId)
    applyDecisions(result.decisions, '查找重复与语气词')
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

  const renderVersionId = rendered?.version.id
  useEffect(() => {
    if (!renderVersionId || !chapters.length) { setRenderChapters(null); return }
    let cancelled = false
    api.versionChapters(sessionId, renderVersionId)
      .then((list) => { if (!cancelled) setRenderChapters(list) })
      .catch(() => { if (!cancelled) setRenderChapters(null) })
    return () => { cancelled = true }
  }, [sessionId, renderVersionId, chapters])

  // 键盘快捷键：空格 播放/暂停，Delete/Backspace 删除选中文字，Esc 取消选择。
  // 输入框、下拉框、打开的对话框里不拦截；处理函数每次渲染更新到 ref，监听只挂一次。
  const shortcutRef = useRef<(event: KeyboardEvent) => void>(() => undefined)
  shortcutRef.current = (event) => {
    if (event.defaultPrevented) return
    const target = event.target as HTMLElement | null
    if (target?.closest('input, textarea, select, [contenteditable="true"], dialog[open]') || document.querySelector('dialog[open]')) return
    if ((event.metaKey || event.ctrlKey) && !event.altKey) {
      const key = event.key.toLowerCase()
      if (key === 'z' || key === 'y') {
        event.preventDefault()
        if (!busy) void runHistory(key === 'y' || event.shiftKey ? 'redo' : 'undo')
      }
      return
    }
    if (event.metaKey || event.ctrlKey || event.altKey) return
    const onTranscript = !!target?.closest('.transcript-word, .transcript-pause')
    if (event.key === ' ' && (onTranscript || !target?.closest('button, a, summary, audio'))) {
      const audio = audioRef.current
      if (!audio) return
      event.preventDefault()
      previewEndRef.current = null
      if (audio.paused) void audio.play().catch(() => undefined); else audio.pause()
    } else if ((event.key === 'Delete' || event.key === 'Backspace') && wordSelection && !busy) {
      event.preventDefault()
      void handleDeleteSelectedText()
    } else if (event.key === 'Escape' && wordSelection) {
      event.preventDefault()
      clearWordSelection()
    }
  }
  useEffect(() => {
    const listener = (event: KeyboardEvent) => shortcutRef.current(event)
    document.addEventListener('keydown', listener)
    return () => document.removeEventListener('keydown', listener)
  }, [])

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
    decisions.some((d) => d.accepted === accepted && !d.hardProtected && isCutAction(d.action) && d.startMs <= startMs && d.endMs >= endMs)

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

  // 章节标签挂在章节起点之后的第一个词前面（起点落在停顿里时也能找到下一个词）。
  const chapterMarkers = useMemo(() => {
    const markers = new Map<string, Chapter[]>()
    for (const chapter of chapters) {
      const token = transcript.find((t) => t.jobId && t.endMs != null && t.endMs > chapter.startMs)
      if (token) markers.set(token.id, [...(markers.get(token.id) ?? []), chapter])
    }
    return markers
  }, [chapters, transcript])

  const transcriptParagraphs = useMemo(() => {
    const paragraphs: { token: TranscriptToken; index: number }[][] = []
    let length = 0
    transcript.forEach((token, index) => {
      const previous = transcript[index - 1]
      const gap = previous?.endMs != null ? token.startMs - previous.endMs : 0
      if (!paragraphs.length || gap >= 1500 || chapterMarkers.has(token.id) || (length >= 120 && SENTENCE_END_RE.test(previous?.text ?? '')) || length >= 360) {
        paragraphs.push([]); length = 0
      }
      paragraphs[paragraphs.length - 1].push({ token, index })
      length += token.text.length
    })
    return paragraphs
  }, [transcript, chapterMarkers])

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
    const intervals = decisions.filter((d) => d.accepted && !d.hardProtected && isCutAction(d.action))
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

  const transcriptExportable = transcript.length > 0 && transcript.every((t) => t.jobId && t.endMs != null)
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
      <div className="page-head review-head">
        <div>
          <div className="eyebrow">03 · 剪辑与导出</div>
          {titleDraft === null ? (
            <h1 className="review-title">
              <span>{session?.title ?? '剪辑与导出'}</span>
              {session && <button type="button" className="iconbtn" aria-label="重命名" title="重命名（也是导出文件名和音频标题）" onClick={() => setTitleDraft(session.title)}>✎</button>}
            </h1>
          ) : (
            <form className="review-title-form" onSubmit={(event) => { event.preventDefault(); handleRename() }}>
              <input aria-label="会话标题" value={titleDraft} maxLength={120} autoFocus disabled={busy}
                onChange={(event) => setTitleDraft(event.target.value)}
                onKeyDown={(event) => { if (event.key === 'Escape') setTitleDraft(null) }} />
              <button className="btn btn--primary" type="submit" disabled={busy || !titleDraft.trim()}>保存</button>
              <button className="btn" type="button" disabled={busy} onClick={() => setTitleDraft(null)}>取消</button>
            </form>
          )}
          <p>在转写里选中要删的内容或重录，确认建议后生成成片并导出。时间均以剪辑前的录音为准。</p>
        </div>
        <div className="review-head__side">
        <div className="history-bar" role="group" aria-label="撤销与重做">
          <button type="button" className="btn" disabled={busy || !undoStack.length} onClick={() => void runHistory('undo')}
            title={undoStack.length ? `撤销：${undoStack[undoStack.length - 1].label}（Ctrl/⌘+Z）` : '没有可撤销的操作'}>↶ 撤销</button>
          <button type="button" className="btn" disabled={busy || !redoStack.length} onClick={() => void runHistory('redo')}
            title={redoStack.length ? `重做：${redoStack[redoStack.length - 1].label}（Ctrl/⌘+Shift+Z）` : '没有可重做的操作'}>↷ 重做</button>
        </div>
        <dl className="review-metrics" aria-label="剪辑概况">
          <div><dt>原长</dt><dd>{totalMs > 0 ? dur(totalMs) : '—'}</dd></div>
          <div className={savedMs > 0 ? 'is-ok' : undefined}><dt>{savedMs >= 0 ? '预计缩短' : '预计增加'}</dt><dd>{dur(Math.abs(savedMs))}</dd></div>
          <div className={pending.length ? 'is-warn' : undefined}><dt>待确认</dt><dd>{pending.length}</dd></div>
        </dl>
        </div>
      </div>

      <section className="workflow-guide review-workflow" aria-label="剪辑流程">
        <ol className="workflow-steps">
          <li aria-current={!ready ? 'step' : undefined}>1 · 准备音频{ready ? ' ✓' : ''}</li><li aria-current={ready && decisions.length === 0 ? 'step' : undefined}>2 · 转写与选段</li>
          <li aria-current={pending.length ? 'step' : undefined}>3 · 确认建议</li><li aria-current={ready && decisions.length > 0 && !pending.length ? 'step' : undefined}>4 · 生成导出</li>
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

      {totalMs > 0 && (
        <div className="review-timeline">
          <WaveformRibbon
            title="时间线 · 判定"
            hint={waveform ? '波形来自真实录音振幅' : '波形为占位曲线（未取到真实录音数据）'}
            envelope={waveform ?? FALLBACK_WAVE}
            durationMs={totalMs}
            decisions={decisions}
          />
        </div>
      )}

      <div className="review-workspace">
        <div className="review-transcript-column">
          <Card id="review-transcript" title="转写与选词" hint="点击文字试听；点起点和终点或拖选来选中，再删除或重录。时间戳为模型估计，删除前请试听核对。">
            <div className="review-player">
              {source && <audio ref={audioRef} onTimeUpdate={(event) => {
          if (previewEndRef.current != null && event.currentTarget.currentTime >= previewEndRef.current) {
            event.currentTarget.pause()
            previewEndRef.current = null
          }
          const currentMs = event.currentTarget.currentTime * 1000
          const active = transcript.find((t) => t.jobId && t.endMs != null && t.startMs <= currentMs && currentMs < t.endMs)
          setPlayingTokenId((prev) => { const next = active?.id ?? null; return next === prev ? prev : next })
        }} onPlay={pauseOtherReviewAudio} onPause={() => { previewEndRef.current = null; setPreviewDecisionId(null); setPlayingTokenId(null) }} aria-label="剪辑前试听" controls preload="metadata" src={api.audioUrl(sessionId, source.id)} style={{ width: '100%' }} />}
              {!source && <p className="card__hint">音频准备好后可在这里试听剪辑前的录音。</p>}
            </div>
            <div className="transcript-toolbar">
              <label className="inline-field">识别语言 <select aria-label="识别语言" value={language} disabled={busy || asrRunning} onChange={(event) => setLanguage(event.target.value as 'auto' | 'zh' | 'en')}>
                <option value="auto">自动识别</option><option value="zh">中文</option><option value="en">英文</option>
              </select></label>
              <button className={transcript.length ? 'btn' : 'btn btn--primary'} onClick={handleTranscribe} disabled={busy || !ready || asrRunning}>
                {asrRunning ? '转写进行中…' : transcript.length ? '重新转写' : '开始本机转写'}
              </button>
              {transcript.length > 0 && <button className="btn" onClick={handleSuggest} disabled={busy || !ready || asrRunning || transcript.some((token) => !token.jobId || token.endMs == null)}
                title="相邻重复会在转写后自动提示；语气词需手动查找。建议需试听确认。">查找重复与语气词</button>}
            </div>
            {pendingRepetitions > 0 && <button className="pill-badge pill-badge--warn" onClick={() => goToSection('review-decisions')}>疑似重复 {pendingRepetitions} 处 · 复核 →</button>}
            {suggestionMessage && <p role="status">{suggestionMessage}</p>}
            {asrJob && <p role="status">{
              asrJob.status === 'QUEUED' ? '等待本机转写…' :
              asrJob.status === 'LOADING_MODEL' ? '正在加载模型；首次下载可能需要几分钟…' :
              asrJob.status === 'TRANSCRIBING' ? `正在转写 ${Math.round(asrJob.progress * 100)}%` :
              asrJob.status === 'FAILED' ? asrJob.errorMessage :
              `转写完成 · ${LANGUAGE_LABEL[asrJob.detectedLanguage ?? ''] ?? asrJob.detectedLanguage ?? '未知语言'}`
            }</p>}
            {transcript.length === 0 && <p className="card__hint">{asrJob?.status === 'COMPLETED' ? '未识别到可转写的语音。' : '转写完成后，点击起点和终点，或拖过多个词来选择，再点“删除选中文字”。词间停顿也可选择删除。'}</p>}
            {transcript.some((token) => !token.jobId) && <p className="card__hint">未标记时间的文字来自历史候选，不是语音识别结果。</p>}
            {transcript.some((token) => token.jobId) && <details className="transcript-help"><summary>选词与剪辑说明</summary><p className="card__hint">点击一个词确定起点，再点另一词扩展范围；也可直接拖选。手机和电脑操作一致。“‧‧‧0.8s”表示可选的词间停顿。选中后可删除、重录或设为章节；导出转写仍对应剪辑前录音。电脑上可用快捷键：空格 播放/暂停，Delete 删除选中，Esc 取消选择，Ctrl/⌘+Z 撤销，Ctrl/⌘+Shift+Z 重做。</p></details>}

            {transcript.length === 0 && ready && !asrRunning && (
              <div className="empty-state">
                <p>还没有转写文字。点上方“开始本机转写”，识别在本机完成，首次会下载模型。</p>
                <p className="card__hint">不需要按文字剪辑时，也可以在右侧“辅助剪辑”中按秒数剪辑，或直接到下方生成导出。</p>
              </div>
            )}
            {transcript.length > 0 && <div className="transcript">
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
                    {chapterMarkers.get(token.id)?.map((chapter) => (
                      <button key={chapter.id} type="button" className="chapter-marker" title={`章节 · ${tc(chapter.startMs)} · 点击在“章节”中查看`}
                        onClick={() => goToSection('review-chapters')}>§ {chapter.title}</button>
                    ))}
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
            </div>}
            {wordSelection && (
              <>
              {/* 手机上是贴在底部导航上方的固定操作栏，不再盖住正在选择的文字；电脑上跟在转写下方。 */}
              <div className="word-selection-bar" role="toolbar" aria-label="选中内容操作">
                <div className="word-selection-bar__summary">
                  <span className="word-selection-bar__text">「{wordSelectionText || '停顿'}」</span>
                  <span className="word-selection-bar__time mono">{tc(wordSelection.startMs)} – {tc(wordSelection.endMs)} · {((wordSelection.endMs - wordSelection.startMs) / 1000).toFixed(1)} 秒</span>
                </div>
                <div className="word-selection-bar__actions">
                  <button className="btn btn--primary" disabled={busy} onClick={handleDeleteSelectedText}>删除<span className="wide-only">选中文字</span></button>
                  <button className="btn" disabled={busy || !source} onClick={() => previewRange(wordSelection.startMs, wordSelection.endMs)}>▶ 试听</button>
                  <button className="btn" disabled={busy || !ready || asrRunning} onClick={() => openRetake(wordSelection.startMs, wordSelection.endMs, wordSelectionText)}>重录<span className="wide-only">选中片段</span></button>
                  <button className="btn" disabled={busy || !ready} onClick={handleChapterFromSelection} title="以选中内容的开头作为新章节起点"><span className="wide-only">设为</span>章节</button>
                  <button className="btn btn--link" disabled={busy} onClick={clearWordSelection} aria-label="取消选择">取消</button>
                </div>
              </div>
              <div className="word-selection-spacer" aria-hidden="true" />
              </>
            )}
          </Card>
        </div>
        {toolsOpen && <div className="review-tools-backdrop" onClick={() => setToolsOpen(false)} />}
        <aside ref={toolsRef} id="review-tools-panel" role={compactTools ? "dialog" : undefined} aria-modal={compactTools && toolsOpen ? true : undefined} aria-hidden={compactTools && !toolsOpen ? true : undefined} className={`review-tools-column${toolsOpen ? ' review-tools-column--open' : ''}`} aria-label="剪辑决策与辅助工具">
          <div className="review-tools-drawer-head">
            <strong>剪辑决策与辅助工具</strong>
            <button type="button" className="btn btn--link" onClick={() => setToolsOpen(false)}>收起 ✕</button>
          </div>

        {decisions.length === 0 && ready && (
          <Card id="review-decisions" title="剪辑决策">
            <p className="card__hint">还没有剪辑。在左侧转写中选中文字删除，或点“查找重复与语气词”“查找过长停顿”生成建议。</p>
          </Card>
        )}
        {decisions.length > 0 && (
        <Card
          id="review-decisions"
          title="剪辑决策"
          hint="点击一行展开详情；聚焦某条决策后按 A 接受、R 拒绝，无需逐个点击。"
          action={
            <div className="decision-batch-acts">
              <div className="segmented" role="group" aria-label="筛选">
                <button aria-pressed={filter === 'all'} onClick={() => setFilter('all')}>全部 {decisions.length}</button>
                <button aria-pressed={filter === 'review'} onClick={() => setFilter('review')}>待确认 {pending.length}</button>
              </div>
              <button
                className={pending.length > 0 ? 'btn btn--primary' : 'btn'}
                onClick={handleConfirmAll}
                disabled={busy || pending.length === 0}
              >
                确认全部待定项{pending.length > 0 ? ` (${pending.length})` : ''}
              </button>
              <button className="btn" onClick={handleRejectSuggestions}
                disabled={busy || !pending.some((d) => d.source === 'transcript_rule' || d.source === 'silence_rule')}>
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
                        {d.source === 'transcript_rule' ? '文字规则建议' : d.source === 'silence_rule' ? '停顿检测建议' : d.confidence.toFixed(2)}
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

          <Card id="review-advanced" title="辅助剪辑" hint="自动找出过长停顿，或按秒数精确输入剪辑范围。">
        <details className="advanced-editor">
          <summary>压缩过长停顿</summary>
          <p className="card__hint">按音频能量找出过长的停顿，只删中间部分、两侧保留自然间隔。结果作为待确认建议加入剪辑决策，请试听后接受。</p>
          <div className="advanced-editor__form">
            <label className="cut-field">停顿超过
              <select aria-label="停顿阈值" value={minPauseMs} disabled={busy || !ready} onChange={(event) => setMinPauseMs(Number(event.target.value))}>
                {[1000, 1500, 2000, 3000].map((ms) => <option key={ms} value={ms}>{(ms / 1000).toFixed(1)} 秒</option>)}
              </select>
            </label>
            <button className="btn" type="button" disabled={busy || !ready} onClick={handleSuggestPauses}>查找过长停顿</button>
          </div>
          {pauseMessage && <p role="status">{pauseMessage}</p>}
        </details>
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

        <ChapterPanel sessionId={sessionId} chapters={chapters} onChange={setChapters} totalMs={totalMs} disabled={busy || !ready}
          currentMs={() => (audioRef.current?.currentTime ?? 0) * 1000}
          onSeek={(ms) => { const audio = audioRef.current; if (!audio) return; previewEndRef.current = null; audio.currentTime = ms / 1000; void audio.play().catch(() => undefined) }} />

        <Card title="本次处理的提醒">
          <WarningList warnings={warnings} />
        </Card>
        </aside>
        {(compactTools || !toolsOpen) && !(compactTools && wordSelection) && (
          <button ref={toolsTriggerRef} type="button" aria-expanded={toolsOpen} aria-controls="review-tools-panel" className={`review-tools-fab${pending.length > 0 ? ' review-tools-fab--warn' : ''}`} onClick={() => { if (pending.length) { setFilter('review'); jumpToDecision(pending[0].id) } else setToolsOpen(true) }}>
            {pending.length > 0 ? `待确认 ${pending.length} 条` : '剪辑决策'} <span aria-hidden="true">▲</span>
          </button>
        )}
      </div>

      <Card id="review-output" title="生成与导出" hint="确认剪辑后生成成片，试听满意再下载。重叠的删除区间只删一次。">
        {session && <div className="episode-meta">
          <h3>节目封面</h3>
          <CoverPicker session={session} onChange={(updated) => { setSession(updated); onDataChange() }} disabled={busy} />
        </div>}
        <div className="render-actions">
          <label style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            <input type="checkbox" checked={smoothCuts} disabled={busy} onChange={(event) => setSmoothCuts(event.target.checked)} />
            平滑剪辑
          </label>
          <label style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            响度标准化
            <select aria-label="响度标准化" value={loudnessTarget} disabled={busy} onChange={(event) => setLoudnessTarget(event.target.value as LoudnessTarget | '')}>
              <option value="">不调整</option>
              {Object.entries(LOUDNESS_TARGET_LABEL).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
            </select>
          </label>
          <button className="btn btn--primary" disabled={busy || !ready || pending.length > 0 || !!conflict} onClick={handleRender}>
            {rendering ? '渲染中…' : '生成剪辑音频'}
          </button>
        </div>
        {pending.length > 0 && <button className="review-pending-pill" onClick={() => goToSection('review-decisions')}>⚠ 还有 {pending.length} 条待确认，处理后才能生成 →</button>}

        {rendered && (
          <div style={{ marginTop: 12 }}>
            <p role="status">{rendered.stale ? '选择或音频源已变化，下方是上一次成片，请重新生成。' : '成片已就绪。'} 时长 {(rendered.version.durationMs / 1000).toFixed(3)} 秒，{rendered.removedDurationMs >= 0 ? '缩短' : '增加'} {Math.abs(rendered.removedDurationMs / 1000).toFixed(3)} 秒，{rendered.version.engine.endsWith('_fade') ? '已启用平滑剪辑' : '直接拼接'}。</p>
            {rendered.loudness && <p className="card__hint">{LOUDNESS_TARGET_LABEL[rendered.loudness.target] ?? rendered.loudness.target}：{loudnessSummary(rendered.loudness)}</p>}
            {rendered.hasReplacements && <p className="card__hint">此成片包含重录，生成后会自动转写成片，完成后可直接下载匹配的字幕。</p>}
            <audio ref={masterAudioRef} aria-label="成片试听" key={rendered.version.id} controls preload="metadata" onPlay={pauseOtherReviewAudio} src={api.audioUrl(sessionId, rendered.version.id)} style={{ width: '100%' }} />
            <div className="export-groups">
              <div className="export-group">
                <h3>音频</h3>
                <div className="export-group__row">
                  <a className="btn" href={api.downloadUrl(sessionId, rendered.version.id)}>WAV 无损</a>
                  {(['mp3', 'm4a'] as const).map((format) => (
                    <button key={format} className="btn" disabled={busy} onClick={() => handleExportAudio(format)}>
                      {exporting === format ? '转码中…' : format.toUpperCase()}
                    </button>
                  ))}
                  <label className="inline-field">码率
                    <select aria-label="导出码率" value={exportBitrate} disabled={busy} onChange={(event) => setExportBitrate(Number(event.target.value))}>
                      {EXPORT_BITRATES.map((kbps) => <option key={kbps} value={kbps}>{kbps} kbps{kbps === 192 ? '（推荐）' : ''}</option>)}
                    </select>
                  </label>
                </div>
                <p className="card__hint">MP3 兼容性最好；M4A（AAC）同码率音质更好，适合播客平台和苹果设备。</p>
              </div>
              <div className="export-group">
                <h3>字幕与文本</h3>
                <div className="export-group__row">
                  {!rendered.hasReplacements && <button className="btn" disabled={busy} onClick={handleExportMaster}>成片 SRT</button>}
                  {chapters.length > 0 && <button className="btn" disabled={busy} onClick={handleCopyChapters}>{chapterCopied ? '已复制章节列表' : '复制章节列表'}</button>}
                  {transcriptExportable && (['txt', 'srt'] as const).map((format) => (
                    <button key={format} className="btn" disabled={busy || asrRunning} onClick={() => handleExportTranscript(format)}>原始转写 {format.toUpperCase()}</button>
                  ))}
                </div>
                <p className="card__hint">成片 SRT 与章节列表对应剪辑后的时间；原始转写对应剪辑前录音。{chapters.length > 0 && ' 章节已写入 MP3 / M4A。'}</p>
                {renderChapters && renderChapters.length > 0 && (
                  <details className="render-chapters">
                    <summary>成片中的章节 · {renderChapters.length}{chapters.length > renderChapters.length ? `（${chapters.length - renderChapters.length} 个因内容被剪掉已合并）` : ''}</summary>
                    <ol>
                      {renderChapters.map((c) => (
                        <li key={c.id}>
                          <span className="mono">{tc(c.startMs)}</span>
                          <span>{c.title}</span>
                          {c.moved && <span className="render-chapters__note" title={`原起点 ${tc(c.originalStartMs)} 落在被剪掉或重录的部分`}>起点已移到剪切处</span>}
                        </li>
                      ))}
                    </ol>
                  </details>
                )}
                {chapterText && <textarea className="chapter-copy" aria-label="章节列表" readOnly rows={Math.min(8, chapterText.split('\n').length)} value={chapterText} onFocus={(event) => event.currentTarget.select()} />}
              </div>
            </div>
            {rendered.hasReplacements && <RenderTranscriptPanel key={rendered.version.id} sessionId={sessionId} versionId={rendered.version.id} audioRef={masterAudioRef} stale={rendered.stale} />}
          </div>
        )}
        {!rendered && transcriptExportable && <div className="export-groups">
          <div className="export-group">
            <h3>字幕与文本</h3>
            <div className="export-group__row">
              {(['txt', 'srt'] as const).map((format) => (
                <button key={format} className="btn" disabled={busy || asrRunning} onClick={() => handleExportTranscript(format)}>原始转写 {format.toUpperCase()}</button>
              ))}
            </div>
          </div>
        </div>}
      </Card>


      {historyToast && (
        <div className="toast history-toast" role="status">
          <span>{historyToast.text}</span>
          {historyToast.canUndo && undoStack.length > 0 && (
            <button type="button" className="btn btn--link" disabled={busy} onClick={() => { setHistoryToast(null); void runHistory('undo') }}>撤销</button>
          )}
        </div>
      )}
    </>
  )
}
