/** 时间码统一 mm:ss.mmm——三位毫秒，因为切点精度目标就在毫秒量级。 */
export function tc(ms: number): string {
  const sign = ms < 0 ? '-' : ''
  const v = Math.abs(Math.round(ms))
  const m = Math.floor(v / 60000)
  const s = Math.floor((v % 60000) / 1000)
  const f = v % 1000
  return `${sign}${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}.${String(f).padStart(3, '0')}`
}

/** 时长用人读格式，不带毫秒——用于"已录 12 分 04 秒"这类叙述位置。 */
export function dur(ms: number): string {
  const total = Math.round(ms / 1000)
  const m = Math.floor(total / 60)
  const s = total % 60
  return m > 0 ? `${m} 分 ${String(s).padStart(2, '0')} 秒` : `${s} 秒`
}

/**
 * 会话创建时间：今天只显示时分，今年显示月日时分，跨年带上年份。
 * 无效或缺失时间返回 null，由调用方决定是否显示。
 */
export function createdLabel(iso: string | null | undefined, now: Date = new Date()): string | null {
  if (!iso) return null
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return null
  const p2 = (n: number) => String(n).padStart(2, '0')
  const hm = `${p2(d.getHours())}:${p2(d.getMinutes())}`
  if (d.toDateString() === now.toDateString()) return `今天 ${hm}`
  const md = `${d.getMonth() + 1}月${d.getDate()}日 ${hm}`
  return d.getFullYear() === now.getFullYear() ? md : `${d.getFullYear()}年${md}`
}

export function pct(v: number, digits = 1): string {
  return `${(v * 100).toFixed(digits)}%`
}

export function db(v: number, digits = 1): string {
  return `${v > 0 ? '+' : ''}${v.toFixed(digits)}`
}

/** WS 电平帧的振幅（0..1）转 dBFS，用于 LevelMeter；-60 是电平表的下限，不是真实的数字静音。 */
export function toDbfs(amplitude: number): number {
  return amplitude <= 0 ? -60 : Math.max(-60, 20 * Math.log10(amplitude))
}

export const CONTENT_TYPE_LABEL: Record<string, string> = {
  single_speaker_speech: '单人口播',
  multi_speaker_speech: '多人对话',
  speech_with_music: '人声 + 背景音乐',
  speech_with_overlapping_noise: '人声 + 重叠干扰',
  music_dominant: '音乐为主',
  no_speech: '无人声',
  unknown: '未确定',
}

export const ACTION_LABEL: Record<string, string> = {
  REPLACE_AUDIO: '重录替换',
  DELETE_SELECTION: '删除选段',
  DELETE_REPETITION: '删除重读',
  DELETE_STUTTER: '删除口吃',
  DELETE_FILLER: '删除语气词',
  COMPRESS_SILENCE: '压缩静音',
  KEEP_PROTECTED: '保护保留',
}

export const GRADE_LABEL: Record<string, string> = {
  EXCELLENT: '优',
  GOOD: '良',
  FAIR: '一般',
  POOR: '差',
  UNUSABLE: '不可用',
}
