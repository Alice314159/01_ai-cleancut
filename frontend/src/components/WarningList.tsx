import type { Warning } from '../types/api'

/**
 * 任务级警告。与 API 错误分属两套命名空间（ADR-032）——
 * 警告不改变请求状态码，只出现在这里和任务结果的 warnings[] 里。
 */
export function WarningList({ warnings }: { warnings: Warning[] }) {
  if (warnings.length === 0) {
    return <p className="card__hint">本次处理没有产生警告。</p>
  }
  return (
    <div>
      {warnings.map((w) => (
        <div className="warnrow" key={w.code}>
          <span className="warnrow__code">{w.code}</span>
          <span className="warnrow__msg">{w.message}</span>
        </div>
      ))}
    </div>
  )
}
