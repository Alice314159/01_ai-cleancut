import { useEffect, useState } from 'react'
import { Card, Chip, KV } from '../components/Bits'
import { WarningList } from '../components/WarningList'
import { CONTENT_TYPE_LABEL, GRADE_LABEL, db } from '../lib/format'
import { api } from '../lib/api'
import { oneOf, usePref } from '../lib/prefs'
import type { AudioQualityReport, AudioVersion, EnhancementJob, RoleAssignment } from '../types/api'

const CHAIN_LABEL: Record<string, string> = { declip: '去削波', denoise: '降噪' }

const MODES: { key: 'auto' | 'conservative' | 'aggressive'; label: string; note: string }[] = [
  { key: 'conservative', label: '轻度', note: '保守，优先自然度' },
  { key: 'auto', label: '均衡', note: '一般室内口播' },
  { key: 'aggressive', label: '较强', note: '噪声明显时，可能损失高频' },
]

export function EnhancePage({
  sessionId,
  versions,
  roles,
  onDataChange,
  onContinue,
}: {
  sessionId: string
  versions: AudioVersion[]
  roles: RoleAssignment[]
  onDataChange: () => void
  onContinue: () => void
}) {
  const [quality, setQuality] = useState<AudioQualityReport | null>(null)
  const [job, setJob] = useState<EnhancementJob | null>(null)
  const [mode, setMode] = useState<'auto' | 'conservative' | 'aggressive'>('auto')
  const [declip, setDeclip] = usePref<'auto' | 'on' | 'off'>('declip', 'auto', oneOf('auto', 'on', 'off'))
  const [triggering, setTriggering] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [qualityPending, setQualityPending] = useState(true)
  const [retry, setRetry] = useState(0)

  useEffect(() => {
    let cancelled = false
    let timer: number | undefined
    setQuality(null)
    setJob(null)
    setQualityPending(true)
    setError(null)
    void api.getLatestEnhancement(sessionId).then((latest) => {
      if (!cancelled) setJob(latest)
    }).catch((e) => { if (!cancelled) setError(e.message) })
    const load = async () => {
      try {
        const [report, session] = await Promise.all([api.getQuality(sessionId), api.getSession(sessionId)])
        if (cancelled) return
        setQuality(report)
        if (report) {
          setQualityPending(false)
          onDataChange()
        } else if (session.liveAnalysisStatus === 'FAILED') {
          setQualityPending(false)
          setError('音频分析失败，无法生成质量报告。请重新录制或重新导入音频。')
        } else if (session.captureStatus !== 'IDLE' && session.liveAnalysisStatus !== 'FINALIZED') {
          timer = window.setTimeout(load, 800)
        } else {
          setQualityPending(false)
        }
      } catch (e) {
        if (!cancelled) {
          setQualityPending(false)
          setError(e instanceof Error ? e.message : '加载失败')
        }
      }
    }
    void load()
    return () => { cancelled = true; window.clearTimeout(timer) }
  }, [sessionId, onDataChange, retry])

  const jobId = job?.id
  const jobStatus = job?.status
  useEffect(() => {
    if (!jobId || !jobStatus || !['QUEUED', 'PROCESSING', 'QUALITY_CHECKING'].includes(jobStatus)) return
    let cancelled = false
    let timer: number | undefined
    const poll = async () => {
      try {
        const latest = await api.getEnhancement(sessionId, jobId)
        if (cancelled) return
        setJob(latest)
        setError(null)
        if (!['QUEUED', 'PROCESSING', 'QUALITY_CHECKING'].includes(latest.status)) {
          onDataChange()
          return
        }
      } catch (e) {
        if (cancelled) return
        setError(e instanceof Error ? e.message : '进度查询失败，正在重试')
      }
      if (!cancelled) timer = window.setTimeout(poll, 800)
    }
    timer = window.setTimeout(poll, 800)
    return () => { cancelled = true; window.clearTimeout(timer) }
  }, [jobId, jobStatus, sessionId, onDataChange])

  const handleTrigger = async () => {
    setTriggering(true)
    setError(null)
    try {
      const created = await api.triggerEnhancement(sessionId, mode, declip)
      setJob(created)
    } catch (e) {
      setError(e instanceof Error ? e.message : '无法启动增强')
    } finally {
      setTriggering(false)
    }
  }

  const pageHead = (
    <div className="page-head">
      <div>
        <div className="eyebrow">02 · 音质优化（可选）</div>
        <h1>音频增强</h1>
        <p>
          增强只改变成片音质，不改变识别用的时间基准。所以调整强度不会让已经做好的剪辑决策失效。
        </p>
      </div>
    </div>
  )

  if (!quality) {
    return (
      <>
        {pageHead}
        <div className="card">
          <p className="card__hint" role="status">
            {error ?? (qualityPending ? '录音分析中，完成后会自动显示质量报告…' : '还没有质量报告，请先完成一次录制。')}
          </p>
          {error && <button className="btn" onClick={() => setRetry((value) => value + 1)}>重新加载</button>}
        </div>
      </>
    )
  }

  const gradeTone =
    quality.qualityGrade === 'EXCELLENT' || quality.qualityGrade === 'GOOD'
      ? 'ok'
      : quality.qualityGrade === 'FAIR'
        ? 'warn'
        : 'bad'

  const failed = job?.voiceProtection.filter((m) => !m.pass) ?? []
  const chainChanged = job ? job.actualChain.join('>') !== job.requestedChain.join('>') : false
  const jobRunning = job && ['QUEUED', 'PROCESSING', 'QUALITY_CHECKING', 'FALLING_BACK'].includes(job.status)

  const metrics = [
    ['估算信噪比', quality.estimatedSnrDb.toFixed(1), 'dB'],
    ['综合响度', quality.integratedLufs.toFixed(1), 'LUFS'],
    ['噪声底', quality.noiseFloorDbfs.toFixed(1), 'dBFS'],
    ['真峰值', quality.truePeakDbtp.toFixed(1), 'dBTP'],
    ['削波比例', (quality.clippingRatio * 100).toFixed(2), '%'],
    ['人声占比', (quality.speechCoverage * 100).toFixed(0), '%'],
    ['电流声概率', quality.humProbability.toFixed(2), ''],
    ['混响评分', quality.reverbScore.toFixed(2), ''],
  ]
  const statusLabels: Record<string, string> = {
    QUEUED: '等待增强', PROCESSING: '正在降噪', QUALITY_CHECKING: '检查人声保护',
    FALLING_BACK: '正在回退', COMPLETED: '增强完成', COMPLETED_WITH_FALLBACK: '已保留此前输出', FAILED: '增强失败',
  }
  const roleLabels: Record<string, string> = {
    TIMEBASE: '时间基准', ASR_SOURCE: '语音识别', REVIEW_SOURCE: '复核源',
    RENDER_SOURCE: '剪辑试听／渲染', EXPORT_SOURCE: '导出', REFERENCE_SOURCE: '参考',
  }

  return (
    <div className="enhance-page">
      {pageHead}
      {(error || job?.errorMessage) && <div className="status-banner status-banner--warning" role="alert">{error || job?.errorMessage}</div>}
      <div className="enhance-workspace">
        <Card title="质量报告" action={<Chip tone={gradeTone === 'ok' ? 'committed' : gradeTone === 'warn' ? 'provisional' : 'danger'}>{GRADE_LABEL[quality.qualityGrade]}</Chip>}
          hint={quality.gradeReason}>
          <dl className="quality-dashboard">
            {metrics.map(([label, value, unit]) => <div className="quality-metric" key={label}>
              <dt>{label}</dt><dd>{value}<span>{unit}</span></dd>
            </div>)}
          </dl>
          <div className="quality-context">
            <span title={`类型置信度 ${quality.contentTypeConfidence.toFixed(2)}`}>素材类型 <strong>{CONTENT_TYPE_LABEL[quality.contentType] ?? quality.contentType}</strong></span>
            <span>识别到的噪声 <strong>{quality.noiseTypes.join('、') || '无'}</strong></span>
          </div>
          {(quality.contentType === 'music_dominant' || quality.contentType === 'speech_with_music') && (
            <p className="card__hint declip-hint">检测到{quality.contentType === 'music_dominant' ? '以音乐为主' : '背景音乐'}：降噪会把音乐当作噪声削弱，建议选轻度或跳过降噪。</p>
          )}
          {quality.contentType === 'no_speech' && <p className="card__hint declip-hint">没有检测到人声，请确认录音或导入的文件是否正确。</p>}
          <p className="card__hint quality-footnote">以上为信号分析估算值（素材类型按音量节奏与频谱特征判断，不区分单人与多人），结合试听判断音质。</p>
        </Card>

        <Card title="音质增强" hint="可修复破音（削波）并降噪，处理后对比原音与结果。">
          <div className="denoise-modes" role="group" aria-label="降噪强度">
            {MODES.map((m) => <button key={m.key} aria-pressed={mode === m.key} disabled={triggering || !!jobRunning} onClick={() => setMode(m.key)}>{m.label}</button>)}
          </div>
          <p className="denoise-mode-note">{MODES.find((m) => m.key === mode)?.note}</p>
          <label className="inline-field declip-field">修复破音（削波）
            <select aria-label="修复破音" value={declip} disabled={triggering || !!jobRunning} onChange={(event) => setDeclip(event.target.value as 'auto' | 'on' | 'off')}>
              <option value="auto">自动（检测到削波时修复）</option>
              <option value="on">总是尝试</option>
              <option value="off">不修复</option>
            </select>
          </label>
          {quality && quality.clippingRatio > 0.001 && (
            <p className="card__hint declip-hint">检测到 {(quality.clippingRatio * 100).toFixed(2)}% 的采样被削平（破音）。修复会根据两侧波形重建被削掉的峰，严重失真的长段无法还原。</p>
          )}
          <button className="btn btn--primary denoise-start" onClick={handleTrigger} disabled={triggering || !!jobRunning}>
            {triggering ? '正在启动…' : jobRunning ? `处理中 ${Math.round((job?.progress ?? 0) * 100)}%` : '开始增强'}
          </button>
          {job && <div className="enhancement-status" role="status">
            <strong>{statusLabels[job.status] ?? job.status}</strong>
            {jobRunning && <progress aria-label="增强进度" max={1} value={job.progress} />}
            {!jobRunning && job.enhancementSuccess !== null && <span>{job.enhancementSuccess ? `${job.actualChain.includes('denoise') ? `噪声下降 ${db(job.noiseReductionDb)} dB · ` : ''}人声保护通过` : failed.length ? '人声保护未通过，输出保留此前版本。请试听核对。' : '未生成可用增强结果，原音频保留。'}</span>}
            {!jobRunning && job.requestedChain.includes('declip') && <span>{job.declippedRegions ? `已修复 ${job.declippedRegions} 处破音` : '未发现需要修复的破音'}</span>}
          </div>}
          <div className="enhance-continue">
            <button className="btn" onClick={onContinue} disabled={triggering || !!jobRunning}>继续剪辑</button>
            <span>降噪为可选步骤，音质满意可直接继续。</span>
          </div>
        </Card>
      </div>

      {job && <div className="enhance-protection">
        <details className="protection-details">
          <summary><span>人声保护与处理详情</span><span className={`protection-summary ${failed.length ? 'protection-summary--warning' : ''}`}>
            {jobRunning ? '检查中' : job.voiceProtection.length ? `${job.voiceProtection.length - failed.length} / ${job.voiceProtection.length} 项通过` : '暂无检查结果'}
          </span></summary>
          <p className="card__hint">通过音色相似度、频谱质心偏移和动态范围检查后才会切换输出；指标为信号分析近似值。</p>
          {job.voiceProtection.map((m) => <KV key={m.key} k={`${m.pass ? '✓' : '!' } ${m.label}${m.hard ? ' · 硬约束' : ''}`}
            v={`${m.value}${m.unit ? ` ${m.unit}` : ''} / ${m.target}`} tone={m.pass ? 'ok' : 'bad'} />)}
          <KV k="请求处理链" v={job.requestedChain.map((step) => CHAIN_LABEL[step] ?? step).join(' → ') || '—'} />
          <KV k="实际处理链" v={job.actualChain.map((step) => CHAIN_LABEL[step] ?? step).join(' → ') || '—'} tone={chainChanged ? 'warn' : undefined} />
          <KV k="任务状态" v={job.status} />
        </details>
        {job.warnings.length > 0 && <div className="status-banner status-banner--warning"><WarningList warnings={job.warnings} /></div>}
      </div>}

      <Card title="音频版本" hint="高亮行为当前剪辑试听／渲染使用的版本；角色标签说明各版本的用途。">
        <div className="audio-versions">
          <table className="table audio-version-table">
            <thead><tr><th>版本与用途</th><th>试听对比</th><th>音频信息</th></tr></thead>
            <tbody>
              {versions.length === 0 && <tr><td colSpan={3}>还没有音频版本。</td></tr>}
              {versions.map((v) => {
                const rs = roles.filter((r) => r.audioVersionId === v.id)
                const current = rs.some((r) => r.role === 'RENDER_SOURCE')
                return <tr key={v.id} className={current ? 'audio-version--active' : undefined}>
                  <td data-label="版本与用途">
                    <div className="audio-version-name"><strong>{v.label}</strong>{current && <span className="audio-current-badge">当前剪辑源</span>}</div>
                    <div className="audio-version-roles">{rs.map((r) => <span key={r.role} title={r.role}>{roleLabels[r.role] ?? r.role}</span>)}{!rs.length && <span>历史／独立片段</span>}</div>
                  </td>
                  <td data-label="试听对比"><audio aria-label={`试听 ${v.label} ${v.id}`} controls preload="metadata" src={api.audioUrl(sessionId, v.id)} onPlay={(event) => {
                    const currentAudio = event.currentTarget
                    currentAudio.closest('.audio-versions')?.querySelectorAll('audio').forEach((audio) => { if (audio !== currentAudio) audio.pause() })
                  }} /></td>
                  <td data-label="音频信息"><span className="mono">{v.sampleRate / 1000} kHz · {(v.durationMs / 1000).toFixed(1)} 秒</span>
                    <details className="audio-version-details"><summary>技术详情</summary><div>
                      <span>类型：{v.versionType}</span><span>延迟：{v.latencyMs} ms</span>
                      <span>引擎：{v.engine} {v.engineVersion !== '—' ? v.engineVersion : ''}</span>
                      <span>{v.determinism === 'deterministic' ? '可重建' : v.determinism === 'version_stable' ? '同版本内一致' : '不可复现'}</span>
                    </div></details>
                  </td>
                </tr>
              })}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  )
}
