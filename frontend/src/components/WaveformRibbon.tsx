import type { EditDecision } from '../types/api'
import { tc } from '../lib/format'

/**
 * 标注波形带 — 两层结构，每一层都承载确定的含义，没有装饰层：
 *
 *   波形       committed / provisional 用颜色区分
 *   下 gutter  终态化后的剪辑决策     → 判定
 */
export function WaveformRibbon({
  envelope,
  durationMs,
  committedMs,
  decisions = [],
  playheadMs,
  title,
  hint,
}: {
  envelope: number[]
  durationMs: number
  committedMs?: number
  decisions?: EditDecision[]
  playheadMs?: number
  title: string
  hint?: string
}) {
  const committed = committedMs ?? durationMs
  const provStartPct = (committed / durationMs) * 100
  const provWidthPct = 100 - provStartPct
  const pos = (ms: number) => `${(ms / durationMs) * 100}%`
  const width = (a: number, b: number) => `${((b - a) / durationMs) * 100}%`
  const committedBars = Math.round((committed / durationMs) * envelope.length)

  return (
    <section className="ribbon" aria-label={title}>
      <header className="ribbon__head">
        <div>
          <div className="eyebrow">{title}</div>
          {hint && <div style={{ color: 'var(--text-dim)', fontSize: 'var(--step--1)' }}>{hint}</div>}
        </div>
        <div className="ribbon__legend">
          <span>
            <i style={{ background: 'var(--sig-committed)' }} />
            已确认 / 重录
          </span>
          <span>
            <i style={{ background: 'var(--sig-provisional)' }} />
            待定 / 提示
          </span>
          <span>
            <i style={{ background: 'var(--sig-cut)' }} />
            删除
          </span>
          <span>
            <i style={{ background: 'var(--sig-protect)' }} />
            保护
          </span>
        </div>
      </header>

      <div className="ribbon__body">
        <div className="ribbon__track">
          {/* 上层：波形 */}
          <div className="wave">
            {envelope.map((v, i) => (
              <span
                key={i}
                className={`wave__bar${i >= committedBars ? ' wave__bar--prov' : ''}`}
                style={{ height: `${Math.round(v * 100)}%` }}
              />
            ))}
            {provWidthPct > 0.5 && (
              <span
                className="wave__prov-mask"
                style={{ left: `${provStartPct}%`, width: `${provWidthPct}%` }}
                aria-label="待定区间，尾部转写仍可能被后续窗口修正"
              />
            )}
            {playheadMs !== undefined && (
              <span className="playhead" style={{ left: pos(playheadMs) }}>
                <span className="playhead__tc mono">{tc(playheadMs)}</span>
              </span>
            )}
          </div>

          {/* 下层：剪辑决策 */}
          <div className="gutter gutter--decisions" aria-label="剪辑决策区间">
            {decisions.filter((d) => d.accepted !== false || d.hardProtected || d.action === 'KEEP_PROTECTED').map((d) => (
              <span
                key={d.id}
                className={`seg seg--${
                  d.hardProtected || d.action === 'KEEP_PROTECTED' ? 'protect' : d.accepted === null ? 'review' : d.action === 'REPLACE_AUDIO' ? 'replace' : 'cut'
                }`}
                style={{ left: pos(d.startMs), width: width(d.startMs, d.endMs) }}
                title={`${d.text} · ${d.reason}`}
              />
            ))}
          </div>

          <div className="ruler">
            <span>00:00.000</span>
            <span>{tc(durationMs / 2)}</span>
            <span>{tc(durationMs)}</span>
          </div>
        </div>
      </div>
    </section>
  )
}
