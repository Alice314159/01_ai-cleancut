/**
 * 输入电平表。三条刻度线对应实时质量守卫的判定阈值（Baseline §5.2.5）：
 *   -45 dBFS 偏低   -12 dBFS 目标区下沿   -1 dBFS 削波风险
 * 刻度画在表上而不是写在说明里，用户调增益时才知道往哪调。
 */
export function LevelMeter({ rmsDbfs, peakDbfs }: { rmsDbfs: number; peakDbfs: number }) {
  const toPct = (dbfs: number) => Math.max(0, Math.min(100, ((dbfs + 60) / 60) * 100))
  return (
    <div>
      <div
        className="meter"
        role="meter"
        aria-label="输入电平"
        aria-valuenow={Math.round(rmsDbfs)}
        aria-valuemin={-60}
        aria-valuemax={0}
        aria-valuetext={`${rmsDbfs.toFixed(1)} dBFS`}
      >
        <span className="meter__fill" style={{ width: `${toPct(rmsDbfs)}%` }} />
        <span className="meter__tick" style={{ left: `${toPct(-45)}%` }} />
        <span className="meter__tick" style={{ left: `${toPct(-12)}%` }} />
        <span className="meter__tick" style={{ left: `${toPct(-1)}%` }} />
      </div>
      <div className="meter__scale">
        <span>-60</span>
        <span>-45 偏低</span>
        <span>-12 目标</span>
        <span>-1 削波</span>
      </div>
      <div className="meter__scale" style={{ marginTop: 6 }}>
        <span>
          RMS <b className="mono">{rmsDbfs.toFixed(1)}</b> dBFS
        </span>
        <span>
          峰值 <b className="mono">{peakDbfs.toFixed(1)}</b> dBFS
        </span>
      </div>
    </div>
  )
}
