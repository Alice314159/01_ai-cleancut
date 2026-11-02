import type { QualityEvent } from '../types/api'
import { tc } from '../lib/format'

/**
 * 实时质量守卫提示。两级，都是非模态（ADR-049）：
 *   notice   低对比度、可无视，不打断说话节奏
 *   warning  高对比度、置顶，但仍不弹窗、不发声、不自动暂停录制
 * 打断创作者比漏一次提示的代价更大。
 */
export function GuardBanner({ event }: { event: QualityEvent }) {
  const isWarning = event.severity === 'warning'
  return (
    <div
      className={`guard${isWarning ? ' guard--warning' : ''}`}
      role={isWarning ? 'alert' : 'status'}
      aria-live={isWarning ? 'assertive' : 'polite'}
    >
      <span className="guard__lv">{isWarning ? '警告' : '提醒'}</span>
      <div className="guard__body">
        <p className="guard__msg">{event.message}</p>
        <p className="guard__meta">
          {tc(event.clientTimestampMs)}
          {event.metrics
            ? ' · ' +
              Object.entries(event.metrics)
                .map(([k, v]) => `${k}=${v}`)
                .join(' · ')
            : ''}
        </p>
      </div>
    </div>
  )
}
