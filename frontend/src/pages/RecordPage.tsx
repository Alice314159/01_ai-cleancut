import { useEffect, useMemo } from 'react'
import { Card, Chip, KV, Stat } from '../components/Bits'
import { GuardBanner } from '../components/GuardBanner'
import { LevelMeter } from '../components/LevelMeter'
import { WaveformRibbon } from '../components/WaveformRibbon'
import { envelope as mockEnvelope } from '../mock/data'
import { dur, tc } from '../lib/format'
import { useRecordingSocket } from '../lib/useRecordingSocket'
import type { SessionDetail } from '../lib/api'

const FALLBACK_WAVE = mockEnvelope(190)

/** 时钟漂移只对 30 秒以上、正常停止的录音测量；百分比更直观（1% = 10000 ppm）。 */
function clockDriftLabel(drift: SessionDetail['clockDrift']): string {
  if (!drift) return '未测量（录音不足 30 秒或非正常停止）'
  const ppm = Number(drift.clockDriftPpm)
  const pct = `${ppm >= 0 ? '+' : ''}${(ppm / 10000).toFixed(2)}%`
  return Math.abs(ppm) > 20000 ? `偏差 ${pct}，可能是采样率异常` : `偏差 ${pct}，正常`
}

export function RecordPage({
  sessionId,
  session,
  onSessionChange,
  onContinue,
}: {
  sessionId: string
  session: SessionDetail | null
  onSessionChange: () => void
  onContinue: () => void
}) {
  const {
    captureState,
    connError,
    notice,
    pendingSeconds,
    rmsDbfs,
    peakDbfs,
    committedDurationMs,
    liveEnvelope,
    qualityEvents,
    stage,
    start,
    pause,
    resume,
    stop,
  } = useRecordingSocket(sessionId)

  useEffect(() => {
    if (stage?.stage === 'capture' || stage?.stage === 'finalized') onSessionChange()
  }, [stage, onSessionChange])

  const warning = qualityEvents.find((e) => e.severity === 'warning')
  const notices = qualityEvents.filter((e) => e.severity === 'notice')
  const cp = session?.captureProcessing

  const handleStop = async () => {
    await stop()
    onSessionChange()
  }

  const stateLabel = useMemo(() => {
    switch (captureState) {
      case 'RECORDING':
        return { text: '● 录制中', tone: 'provisional' as const }
      case 'PAUSED':
        return { text: '已暂停', tone: undefined }
      case 'RECONNECTING':
        return { text: '网络重连中', tone: 'danger' as const }
      case 'STOPPED':
        return { text: '已停止', tone: 'committed' as const }
      default:
        return { text: '未开始', tone: undefined }
    }
  }, [captureState])

  return (
    <>
      <div className="page-head">
        <div>
          <div className="eyebrow">01 · 录制</div>
          <h1>语音录制</h1>
          <p>
            录制过程中检测音量、削波和突发噪声。录完后可在复核页启动本机转写并生成剪辑建议；录制中暂不识别文字或口误。
          </p>
        </div>
        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
          <Chip tone={stateLabel.tone}>{stateLabel.text}</Chip>
        </div>
      </div>

      {(notice || (pendingSeconds > 0 && captureState !== 'STOPPED')) && (
        <div className="card upload-status" role="status">
          {notice && <p>{notice}</p>}
          {pendingSeconds > 0 && <p className="card__hint">待上传约 {pendingSeconds} 秒（已保存在本机，不会丢失）</p>}
        </div>
      )}
      {(captureState === 'STOPPED' || session?.captureStatus === 'STOPPED') && (
        <Card title="录音已结束" hint="下一步转写文字、选择需要删除的内容，再试听导出。降噪可按需使用。">
          <button className="btn btn--primary" onClick={onContinue}>下一步：转写与剪辑</button>
        </Card>
      )}
      {connError && (
        <div className="card" style={{ marginBottom: 'var(--sp-4)', color: 'tomato' }}>
          {connError}
        </div>
      )}

      {warning && (
        <div style={{ marginBottom: 'var(--sp-4)' }}>
          <GuardBanner event={warning} />
        </div>
      )}

      <div style={{ marginBottom: 'var(--sp-4)' }}>
        <WaveformRibbon
          title="录制波形"
          hint={
            liveEnvelope.length > 0
              ? '波形柱来自实时电平采样（每 2.5 秒一根），不是装饰曲线'
              : '尚未开始录制，下方为占位曲线'
          }
          envelope={liveEnvelope.length > 0 ? liveEnvelope : FALLBACK_WAVE}
          durationMs={Math.max(committedDurationMs, 1000)}
          committedMs={committedDurationMs}
          playheadMs={committedDurationMs}
        />
      </div>

      <div className="grid grid--main" style={{ marginBottom: 'var(--sp-4)' }}>
        <Card
          title="录制控制"
          action={<span className="mono" style={{ color: 'var(--text-faint)' }}>{tc(committedDurationMs)}</span>}
        >
          <div className="grid grid--3" style={{ marginBottom: 'var(--sp-4)' }}>
            <Stat k="已录" v={dur(committedDurationMs)} />
            <Stat k="已确认" v={dur(committedDurationMs)} />
            <Stat k="质量提醒" v={String(qualityEvents.length)} note={warning ? '含严重警告' : undefined} />
          </div>

          <div style={{ marginBottom: 'var(--sp-4)' }}>
            <LevelMeter rmsDbfs={rmsDbfs} peakDbfs={peakDbfs} />
          </div>

          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
            {captureState === 'IDLE' && (
              <button className="btn btn--primary" onClick={start}>
                开始录制
              </button>
            )}
            {captureState === 'RECORDING' && (
              <>
                <button className="btn" onClick={pause}>
                  暂停
                </button>
                <button className="btn btn--primary" onClick={handleStop}>
                  停止并终态化
                </button>
              </>
            )}
            {captureState === 'PAUSED' && (
              <>
                <button className="btn btn--primary" onClick={resume}>
                  继续
                </button>
                <button className="btn" onClick={handleStop}>
                  停止并终态化
                </button>
              </>
            )}
            {captureState === 'RECONNECTING' && (
              <button className="btn" onClick={handleStop} title="会先尝试补传本机缓存的录音">
                停止（网络恢复后补传）
              </button>
            )}
            {captureState === 'STOPPED' && <Chip tone="committed">已停止，可前往「复核」页</Chip>}
          </div>
          <p className="card__hint" style={{ marginTop: 'var(--sp-3)', marginBottom: 0 }}>
            停止后会完成最后一片上传并分析录音。网络中断时录音在本机继续并自动重连补传；页面被关闭时，重新打开此会话会补传未上传的部分。
          </p>
        </Card>

        <Card title="输入设备" hint="开始录制后读取浏览器报告的设备设置；未提供的处理状态显示为未知。">
          {session ? (
            <>
              <KV k="采样率" v={session.captureStatus === 'IDLE' ? '尚未读取' : `${session.actualSampleRate / 1000} kHz · ${session.channelCount} 声道`} />
              <KV k="编码" v={session.captureStatus === 'IDLE' ? '尚未读取' : session.mimeType} />
              {session.liveAnalysisStatus === 'FINALIZED' && (
                <KV k="录音时钟" v={clockDriftLabel(session.clockDrift)} tone={Math.abs(Number(session.clockDrift?.clockDriftPpm ?? 0)) > 20000 ? 'warn' : undefined} />
              )}
              {cp && (
                <>
                  <KV k="回声消除" v={cp.echoCancellationActual === 'true' ? '开' : cp.echoCancellationActual === 'false' ? '关' : '未知（未录制）'} />
                  <KV k="噪声抑制" v={cp.noiseSuppressionActual === 'true' ? '开' : cp.noiseSuppressionActual === 'false' ? '关' : '未知（未录制）'} />
                  <KV k="自动增益" v={cp.autoGainControlActual === 'true' ? '开' : cp.autoGainControlActual === 'false' ? '关' : '未知（未录制）'} />
                </>
              )}
            </>
          ) : (
            <p className="card__hint">会话信息加载中…</p>
          )}
        </Card>
      </div>

      <Card title="守卫提醒" hint="提醒不打断录制，你可以忽略它们继续说。">
        <div style={{ display: 'grid', gap: 8 }}>
          {notices.length === 0 && <p className="card__hint">暂无提醒。</p>}
          {notices.map((e) => (
            <GuardBanner key={e.id} event={e} />
          ))}
        </div>
      </Card>
    </>
  )
}
