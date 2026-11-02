import type { AudioVersion, RoleAssignment } from '../types/api'

/** 版本类型的用户可读名称；内部角色名（TIMEBASE / RENDER_SOURCE）不直接展示给用户。 */
export const VERSION_LABEL: Record<string, string> = {
  original: '原始录音',
  denoised: '降噪版本',
  master: '剪辑成片',
  retake: '重录片段',
}

/**
 * 时间基准条 — 常驻窄条，不可折叠、不可关闭。
 *
 * 为什么给它一个永久的位置：时间基准被静默改错是这个系统最难发现的失败模式
 * （ADR-024 / ADR-052）——不报错，只剪错。把它做成界面上一直在场的物件，
 * 是让这类错误在第一眼就能被人看见的最便宜的办法。
 *
 * TIMEBASE 带锁标记，因为它一经冻结就不可变更。
 * 复核播放器和渲染用的是同一个 RENDER_SOURCE 角色，之前分成"复核试听"/"渲染"
 * 两个格显示同一个值，纯重复没有信息量，合并成一格；版本相同、没有异常时也不再
 * 额外展示一句"同版本"——没有警告本身就是"没问题"，少一句更符合这条常驻窄条
 * 应该只在真正需要盯着看时才抢注意力的定位。
 */
export function TimebaseStrip({
  versions,
  roles,
}: {
  versions: AudioVersion[]
  roles: RoleAssignment[]
}) {
  const byId = new Map(versions.map((v) => [v.id, v]))
  const find = (r: RoleAssignment['role']) => roles.find((x) => x.role === r)

  const timebase = find('TIMEBASE')
  const render = find('RENDER_SOURCE')

  const tbVersion = timebase && byId.get(timebase.audioVersionId)
  const rdVersion = render && byId.get(render.audioVersionId)

  return (
    <div className="timebase" role="status" aria-label="音频版本角色">
      <div className="timebase__slot">
        <span className="timebase__key">时间轴</span>
        <span className="timebase__val">{tbVersion ? VERSION_LABEL[tbVersion.versionType] ?? tbVersion.versionType : '—'}</span>
        <span className="timebase__lock" title="剪辑、转写和字幕都以这条时间轴计时，录制完成后固定不变">
          ▮
        </span>
      </div>

      <div className="timebase__sep" />

      <div className="timebase__slot">
        <span className="timebase__key">剪辑音源</span>
        <span className="timebase__val" title={rdVersion?.id}>{rdVersion ? VERSION_LABEL[rdVersion.versionType] ?? rdVersion.versionType : '—'}</span>
        {render?.upgradedFrom && (
          <span className="timebase__up" title="音频源已更新；之前生成的成片需要重新生成才能反映新版本">
            ↑ 已更新
          </span>
        )}
      </div>

      {(!rdVersion || !tbVersion || rdVersion.id !== tbVersion.id) && (
        <>
          <div className="timebase__sep" />
          <span className="timebase__note">
            {!rdVersion || !tbVersion ? '音频尚未就绪' : '剪辑使用处理后的音频，时间仍按原始录音计算'}
          </span>
        </>
      )}
    </div>
  )
}
