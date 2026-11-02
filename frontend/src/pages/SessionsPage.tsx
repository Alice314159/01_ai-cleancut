import { useEffect, useMemo, useState } from 'react'
import { Card, Chip, Stat } from '../components/Bits'
import { ImportAudioButton } from '../components/ImportAudioButton'
import { createdLabel, dur } from '../lib/format'
import { api, type SessionListItem } from '../lib/api'
import type { RouteKey } from '../components/AppShell'

const MODE_LABEL: Record<'live' | 'batch', string> = { live: '在线录制', batch: '录后处理' }

const PROCESSING_LABEL: Record<string, { label: string; tone?: 'committed' | 'provisional' | 'danger' }> = {
  NOT_STARTED: { label: '待处理' },
  QUEUED: { label: '排队中', tone: 'provisional' },
  PROCESSING: { label: '处理中', tone: 'provisional' },
  QUALITY_CHECKING: { label: '质检中', tone: 'provisional' },
  FALLING_BACK: { label: '已降级处理', tone: 'danger' },
  DONE: { label: '已完成', tone: 'committed' },
  COMPLETED: { label: '已完成', tone: 'committed' },
  COMPLETED_WITH_FALLBACK: { label: '已降级完成', tone: 'danger' },
  FAILED: { label: '失败', tone: 'danger' },
}

const UPLOAD_LABEL: Record<string, string> = { NOT_STARTED: '尚未收到', COMPLETED: '已接收', UPLOADING: '上传中', STREAMING: '接收中', PAUSED_BY_POLICY: '已暂停', FAILED: '失败' }
const MERGE_LABEL: Record<string, string> = { NOT_STARTED: '待合并', MERGING: '合并校验中', VERIFIED: '已校验', FAILED: '失败' }
const CAPTURE_LABEL: Record<string, string> = { IDLE: '未开始', RECORDING: '录制中', PAUSED: '已暂停', STOPPED: '已停止', IMPORTING: '导入中', DEVICE_LOST: '设备断开', RECONNECTING: '重新连接中', ABANDONED: '已中断' }

/** 音频就绪后直接继续剪辑，增强是可选步骤。 */
function routeForRow(row: SessionListItem): RouteKey {
  if (row.captureMode !== 'batch' && ['IDLE', 'RECORDING', 'PAUSED'].includes(row.captureStatus)) return 'record'
  if (row.liveAnalysisStatus === 'FINALIZED') return 'review'
  return 'enhance'
}

export function SessionsPage({
  onSelectSession,
  onCreateSession,
  onImportAudio,
  importing,
  defaultProjectId,
}: {
  onSelectSession: (id: string, route: RouteKey) => void
  onCreateSession: (projectId?: string) => void
  onImportAudio: (file: File, projectId?: string) => void
  importing: boolean
  defaultProjectId: string
}) {
  const [rows, setRows] = useState<SessionListItem[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [search, setSearch] = useState('')
  const [projectFilter, setProjectFilter] = useState('')
  const [projectInput, setProjectInput] = useState(defaultProjectId)
  const [editingId, setEditingId] = useState<string | null>(null)
  const [editTitle, setEditTitle] = useState('')
  const [editProject, setEditProject] = useState('')
  const [deletingId, setDeletingId] = useState<string | null>(null)
  const [busyId, setBusyId] = useState<string | null>(null)
  const [rowError, setRowError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    let timer: number | undefined
    const refresh = async () => {
      try {
        const data = await api.listSessions()
        if (!cancelled) { setRows(data); setError(null) }
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : '加载会话列表失败')
      } finally {
        if (!cancelled) { setLoading(false); timer = window.setTimeout(refresh, 2000) }
      }
    }
    void refresh()
    return () => { cancelled = true; window.clearTimeout(timer) }
  }, [])

  const projects = useMemo(() => Array.from(new Set(rows.map((r) => r.projectId))).sort(), [rows])

  const filteredRows = useMemo(() => {
    const q = search.trim().toLowerCase()
    return rows.filter((r) =>
      (!projectFilter || r.projectId === projectFilter) &&
      (!q || r.title.toLowerCase().includes(q) || r.id.toLowerCase().includes(q)),
    )
  }, [rows, search, projectFilter])

  const projectStats = useMemo(() => {
    if (!projectFilter) return null
    const inProject = rows.filter((r) => r.projectId === projectFilter)
    const totalMs = inProject.reduce((sum, r) => sum + r.totalDurationMs, 0)
    const byStatus = new Map<string, number>()
    for (const r of inProject) byStatus.set(r.processingStatus, (byStatus.get(r.processingStatus) ?? 0) + 1)
    return { count: inProject.length, totalMs, byStatus }
  }, [rows, projectFilter])

  const newSessionProject = projectInput.trim() || 'proj_default'

  const startEdit = (row: SessionListItem) => {
    setDeletingId(null)
    setRowError(null)
    setEditingId(row.id)
    setEditTitle(row.title)
    setEditProject(row.projectId)
  }

  const saveEdit = async (id: string) => {
    const title = editTitle.trim()
    const projectId = editProject.trim()
    if (!title || !projectId) { setRowError('标题和项目都不能为空。'); return }
    setBusyId(id)
    setRowError(null)
    try {
      const updated = await api.updateSession(id, { title, projectId })
      setRows((rs) => rs.map((r) => r.id === id ? { ...r, title: updated.title, projectId: updated.projectId } : r))
      setEditingId(null)
    } catch (e) {
      setRowError(e instanceof Error ? e.message : '保存失败，请重试。')
    } finally {
      setBusyId(null)
    }
  }

  const confirmDelete = async (id: string) => {
    setBusyId(id)
    setRowError(null)
    try {
      await api.deleteSession(id)
      setRows((rs) => rs.filter((r) => r.id !== id))
      setDeletingId(null)
    } catch (e) {
      setRowError(e instanceof Error ? e.message : '删除失败，请重试。')
    } finally {
      setBusyId(null)
    }
  }

  return (
    <>
      <div className="page-head sessions-heading">
        <div>
          <div className="eyebrow">会话管理</div>
          <h1>录制记录 <span className="sessions-count">{rows.length}</span></h1>
        </div>
        <details className="sessions-help">
          <summary>ⓘ 状态说明</summary>
          <p>分别显示采集、服务端接收、合并校验和增强状态，每 2 秒刷新。增强为可选步骤。浏览器持久化与断点续传尚未支持；断线仅保留服务端已收到的音频。</p>
        </details>
      </div>

      <section className="sessions-toolbar" aria-label="录制记录工具栏">
        <div className="sessions-toolbar__filters">
          <input type="search" value={search} onChange={(event) => setSearch(event.target.value)}
            placeholder="按标题或会话 ID 搜索" aria-label="搜索会话" />
          <select aria-label="按项目筛选" value={projectFilter} onChange={(event) => setProjectFilter(event.target.value)}>
            <option value="">全部项目（{rows.length}）</option>
            {projects.map((p) => <option key={p} value={p}>{p}（{rows.filter((r) => r.projectId === p).length}）</option>)}
          </select>
        </div>
        <div className="sessions-toolbar__actions">
          <button className="btn btn--primary" onClick={() => onCreateSession(newSessionProject)}>开始新录制</button>
          <ImportAudioButton onImport={(file) => onImportAudio(file, newSessionProject)} busy={importing} />
        </div>
        <div className="sessions-project-setting">
          <span>显示 {filteredRows.length} 条记录</span>
          <label>新录制／导入归属项目
            <input list="project-suggestions" value={projectInput} onChange={(event) => setProjectInput(event.target.value)}
              placeholder="proj_default" aria-label="新建会话所属项目" />
          </label>
          <datalist id="project-suggestions">{projects.map((p) => <option key={p} value={p} />)}</datalist>
        </div>
      </section>

      {projectStats && (
        <Card title={`项目分析 · ${projectFilter}`} hint="仅统计当前项目下的会话，随列表一起每 2 秒刷新。">
          <div className="grid grid--3" style={{ marginBottom: 'var(--sp-4)' }}>
            <Stat k="会话数" v={String(projectStats.count)} />
            <Stat k="总时长" v={dur(projectStats.totalMs)} />
            <Stat k="平均时长" v={dur(projectStats.count ? projectStats.totalMs / projectStats.count : 0)} />
          </div>
          <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
            {[...projectStats.byStatus.entries()].map(([status, count]) => {
              const label = PROCESSING_LABEL[status] ?? { label: status }
              return <Chip key={status} tone={label.tone}>{label.label} × {count}</Chip>
            })}
          </div>
        </Card>
      )}

      <div className="sessions-list" aria-label="录制记录列表">
        {loading && <p className="card__hint">加载中…</p>}
        {error && <p className="card__hint" style={{ color: 'tomato' }}>{error}</p>}
        {!loading && !error && rows.length === 0 && (
          <p className="card__hint">还没有任何录制。点右上角「开始新录制」或「导入音频」建一个。</p>
        )}
        {!loading && !error && rows.length > 0 && filteredRows.length === 0 && (
          <p className="card__hint">没有匹配搜索或项目筛选的会话。</p>
        )}
        {filteredRows.map((r) => {
          const status = PROCESSING_LABEL[r.processingStatus] ?? { label: r.processingStatus }
          const busy = busyId === r.id
          const open = () => onSelectSession(r.id, routeForRow(r))
          const created = createdLabel(r.createdAt)

          if (editingId === r.id) {
            return (
              <div className="session-row" key={r.id} style={{ display: 'grid', gap: 8 }}>
                <div style={{ display: 'flex', gap: 'var(--sp-4)', flexWrap: 'wrap' }}>
                  <div className="session-row__main" style={{ display: 'grid', gap: 8, maxWidth: 360 }}>
                    <label className="cut-field" style={{ marginBottom: 0 }}>标题
                      <input value={editTitle} onChange={(event) => setEditTitle(event.target.value)} aria-label="重命名标题" disabled={busy} />
                    </label>
                    <label className="cut-field" style={{ marginBottom: 0 }}>项目
                      <input list="project-suggestions" value={editProject} onChange={(event) => setEditProject(event.target.value)} aria-label="修改所属项目" disabled={busy} />
                    </label>
                  </div>
                  <div className="session-row__acts">
                    <button className="btn btn--primary" disabled={busy} onClick={() => void saveEdit(r.id)}>{busy ? '保存中…' : '保存'}</button>
                    <button className="btn" disabled={busy} onClick={() => setEditingId(null)}>取消</button>
                  </div>
                </div>
                {rowError && <p className="card__hint" role="alert" style={{ color: 'tomato', margin: 0 }}>{rowError}</p>}
              </div>
            )
          }

          if (deletingId === r.id) {
            return (
              <div className="session-row" key={r.id} style={{ display: 'grid', gap: 8 }}>
                <div style={{ display: 'flex', gap: 'var(--sp-4)', flexWrap: 'wrap', alignItems: 'center' }}>
                  <div className="session-row__main">
                    <p role="alert" style={{ margin: 0 }}>删除「{r.title}」？音频、转写和剪辑决策都会一并清除，不可恢复。</p>
                  </div>
                  <div className="session-row__acts">
                    <button className="btn btn--danger" disabled={busy} onClick={() => void confirmDelete(r.id)}>{busy ? '删除中…' : '确认删除'}</button>
                    <button className="btn" disabled={busy} onClick={() => setDeletingId(null)}>取消</button>
                  </div>
                </div>
                {rowError && <p className="card__hint" role="alert" style={{ color: 'tomato', margin: 0 }}>{rowError}</p>}
              </div>
            )
          }

          return (
            <div className="session-row" key={r.id}>
              <div className="session-row__header">
                <button type="button" className={`session-cover${r.coverVersion ? '' : ' session-cover--empty'}`} onClick={open} tabIndex={-1} aria-hidden="true">
                  {r.coverVersion ? <img src={api.coverUrl(r.id, r.coverVersion)} alt="" loading="lazy" /> : <span>{r.title.trim().slice(0, 1) || '·'}</span>}
                </button>
                <button className="session-row__title" aria-label={`打开会话 ${r.title}`} onClick={open}>{r.title}</button>
                <div className="session-row__acts">
                  <button className="iconbtn" aria-label="重命名或修改项目" title="重命名或修改项目" disabled={busy} onClick={() => startEdit(r)}>✎</button>
                  <button className="iconbtn" aria-label="删除会话" title="删除会话" disabled={busy} onClick={() => { setDeletingId(r.id); setEditingId(null); setRowError(null) }}>🗑</button>
                </div>
              </div>
              <div className="session-row__meta">
                <span className="mono">{r.id}</span>
                <button className="session-project" onClick={() => setProjectFilter(r.projectId)} title="按此项目筛选">{r.projectId}</button>
                <span>{MODE_LABEL[r.captureMode]}</span>
                {created && <time dateTime={r.createdAt ?? undefined}>{created}</time>}
                <span className="session-row__duration">{dur(r.totalDurationMs)}</span>
              </div>
              <div className="session-row__stages" aria-label="处理阶段">
                <SessionStage label="采集" value={CAPTURE_LABEL[r.captureStatus] ?? r.captureStatus} status={r.captureStatus} />
                <SessionStage label="接收" value={UPLOAD_LABEL[r.uploadStatus] ?? (r.uploadStatus === 'RECEIVING' ? (r.captureStatus === 'STOPPED' ? '已接收，待校验' : '接收中') : r.uploadStatus)} status={r.uploadStatus} />
                <SessionStage label="校验" value={MERGE_LABEL[r.serverMergeStatus] ?? r.serverMergeStatus} status={r.serverMergeStatus} />
                <SessionStage label="增强 · 可选" value={status.label} status={r.processingStatus} />
              </div>
            </div>
          )
        })}
      </div>
    </>
  )
}


function SessionStage({ label, value, status }: { label: string; value: string; status: string }) {
  const tone = ['STOPPED', 'COMPLETED', 'VERIFIED', 'DONE'].includes(status) ? 'done'
    : ['FAILED', 'DEVICE_LOST', 'ABANDONED', 'FALLING_BACK', 'COMPLETED_WITH_FALLBACK'].includes(status) ? 'failed'
    : ['NOT_STARTED', 'IDLE', 'NOT_APPLICABLE'].includes(status) ? 'idle' : 'active'
  return <span className={`session-stage session-stage--${tone}`}>
    <span className="session-stage__label">{label}</span>
    <span className="session-stage__value"><i aria-hidden="true" />{value}</span>
  </span>
}
