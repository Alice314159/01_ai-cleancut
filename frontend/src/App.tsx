import { useCallback, useEffect, useRef, useState } from 'react'
import { AppShell, type RouteKey } from './components/AppShell'
import { ImportAudioButton } from './components/ImportAudioButton'
import { TimebaseStrip } from './components/TimebaseStrip'
import { RecordPage } from './pages/RecordPage'
import { EnhancePage } from './pages/EnhancePage'
import { ReviewPage } from './pages/ReviewPage'
import { SessionsPage } from './pages/SessionsPage'
import { api, type SessionDetail } from './lib/api'
import type { AudioVersion, RoleAssignment } from './types/api'

const LAST_PROJECT_KEY = 'cleancut.lastProjectId'

function readLastProject(): string {
  try {
    return localStorage.getItem(LAST_PROJECT_KEY) || 'proj_default'
  } catch {
    return 'proj_default'
  }
}

export default function App() {
  const [route, setRoute] = useState<RouteKey>('sessions')
  // 预览开关：切窄容器即可看到移动端形态（断点走容器查询）。
  const [vp, setVp] = useState<'desktop' | 'mobile'>('desktop')

  const [sessionId, setSessionId] = useState<string | null>(null)
  const [session, setSession] = useState<SessionDetail | null>(null)
  const [versions, setVersions] = useState<AudioVersion[]>([])
  const [roles, setRoles] = useState<RoleAssignment[]>([])
  const [loadError, setLoadError] = useState<string | null>(null)
  const [importing, setImporting] = useState(false)
  const [lastProjectId, setLastProjectId] = useState(readLastProject)

  const rememberProject = useCallback((id: string) => {
    setLastProjectId(id)
    try {
      localStorage.setItem(LAST_PROJECT_KEY, id)
    } catch {
      // Private browsing or storage disabled — the project name still gets used for this session.
    }
  }, [])

  const sessionLoadRef = useRef(0)

  const refreshSession = useCallback(async () => {
    if (!sessionId) return
    const revision = ++sessionLoadRef.current
    try {
      const [s, v, r] = await Promise.all([
        api.getSession(sessionId),
        api.getVersions(sessionId),
        api.getRoles(sessionId),
      ])
      if (revision !== sessionLoadRef.current) return
      setSession(s)
      setVersions(v)
      setRoles(r)
      setLoadError(null)
    } catch (e) {
      if (revision !== sessionLoadRef.current) return
      setLoadError(e instanceof Error ? e.message : '加载会话失败')
    }
  }, [sessionId])

  useEffect(() => {
    void refreshSession()
    return () => { sessionLoadRef.current += 1 }
  }, [refreshSession])

  const handleSelectSession = useCallback((id: string, targetRoute: RouteKey) => {
    if (id !== sessionId) {
      sessionLoadRef.current += 1
      setSession(null); setVersions([]); setRoles([]); setLoadError(null)
    }
    setSessionId(id)
    setRoute(targetRoute)
  }, [sessionId])

  const handleCreateSession = useCallback(async (projectId?: string) => {
    const project = (projectId ?? lastProjectId).trim() || 'proj_default'
    rememberProject(project)
    const created = await api.createSession({ title: '未命名录制', captureMode: 'live', projectId: project })
    sessionLoadRef.current += 1
    setSessionId(created.id)
    setSession(created)
    setVersions([])
    setRoles([])
    setRoute('record')
  }, [lastProjectId, rememberProject])

  const handleImportAudio = useCallback(async (file: File, projectId?: string) => {
    setImporting(true)
    setLoadError(null)
    try {
      const project = (projectId ?? lastProjectId).trim() || 'proj_default'
      rememberProject(project)
      const created = await api.createSession({ title: file.name, captureMode: 'batch', projectId: project })
      sessionLoadRef.current += 1
      const finalized = await api.importAudio(created.id, file)
      const [v, r] = await Promise.all([api.getVersions(created.id), api.getRoles(created.id)])
      setSessionId(created.id)
      setSession(finalized)
      setVersions(v)
      setRoles(r)
      setRoute(finalized.liveAnalysisStatus === 'FINALIZED' ? 'review' : 'enhance')
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : '导入音频失败')
    } finally {
      setImporting(false)
    }
  }, [lastProjectId, rememberProject])

  return (
    <div className="viewport-host" data-vp={vp}>
      <AppShell
        route={route}
        sessionId={sessionId}
        onNavigate={setRoute}
        strip={
          <div className="topbar">
            <TimebaseStrip versions={versions} roles={roles} />
            {import.meta.env.VITE_DEMO === '1' && (
              <span className="demo-badge" title="界面预览：数据为示例，转写、降噪、导出为模拟结果；录音与重录需要连接真实后端。">演示模式</span>
            )}
            <div className="vp-toggle" role="group" aria-label="预览端形态">
              <button aria-pressed={vp === 'desktop'} onClick={() => setVp('desktop')}>电脑端</button>
              <button aria-pressed={vp === 'mobile'} onClick={() => setVp('mobile')}>手机端</button>
            </div>
          </div>
        }
      >
        {route === 'record' &&
          (sessionId ? (
            session?.captureMode === 'batch' ? (
              <ImportedSessionHasNoRecordPage onGoToReview={() => setRoute('review')} />
            ) : (
              <RecordPage key={sessionId} sessionId={sessionId} session={session} onSessionChange={refreshSession} onContinue={() => setRoute('review')} />
            )
          ) : (
            <NoSession onCreate={handleCreateSession} onImportAudio={handleImportAudio} importing={importing} />
          ))}
        {route === 'enhance' &&
          (sessionId ? (
            <EnhancePage key={sessionId} sessionId={sessionId} versions={versions} roles={roles} onDataChange={refreshSession} onContinue={() => setRoute('review')} />
          ) : (
            <NoSession onCreate={handleCreateSession} onImportAudio={handleImportAudio} importing={importing} />
          ))}
        {route === 'review' &&
          (sessionId ? (
            <ReviewPage key={sessionId} sessionId={sessionId} onDataChange={refreshSession} onEnhance={() => setRoute('enhance')} />
          ) : (
            <NoSession onCreate={handleCreateSession} onImportAudio={handleImportAudio} importing={importing} />
          ))}
        {route === 'sessions' && (
          <SessionsPage
            onSelectSession={handleSelectSession}
            onCreateSession={handleCreateSession}
            onImportAudio={handleImportAudio}
            importing={importing}
            defaultProjectId={lastProjectId}
          />
        )}
      </AppShell>

      {loadError && (
        <div className="toast toast--error" role="alert">
          <span>{loadError}</span>
          <button className="btn btn--link" onClick={() => setLoadError(null)}>关闭</button>
        </div>
      )}

    </div>
  )
}

/** 导入音频的会话没有实时录制阶段——直接说明原因并给一条出路，而不是默默显示一个用不上的录制界面。 */
function ImportedSessionHasNoRecordPage({ onGoToReview }: { onGoToReview: () => void }) {
  return (
    <div className="card" style={{ textAlign: 'center', padding: 32 }}>
      <p style={{ marginBottom: 'var(--sp-4)' }}>音频已导入，可以直接转写和剪辑。需要改善噪声时，再选择音频增强。</p>
      <button className="btn btn--primary" onClick={onGoToReview}>
        进入剪辑
      </button>
    </div>
  )
}

function NoSession({
  onCreate,
  onImportAudio,
  importing,
}: {
  onCreate: (projectId?: string) => void
  onImportAudio: (file: File, projectId?: string) => void
  importing: boolean
}) {
  return (
    <div className="card" style={{ textAlign: 'center', padding: 32 }}>
      <p style={{ marginBottom: 'var(--sp-4)' }}>还没有选中的会话。</p>
      <div style={{ display: 'flex', gap: 8, justifyContent: 'center' }}>
        <button className="btn btn--primary" onClick={() => onCreate()}>
          开始新录制
        </button>
        <ImportAudioButton onImport={onImportAudio} busy={importing} />
      </div>
    </div>
  )
}
