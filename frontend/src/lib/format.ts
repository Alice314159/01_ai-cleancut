/** 时间码统一 mm:ss.mmm——三位毫秒，因为切点精度目标就在毫秒量级。 */
export function tc(ms: number): string {
  const sign = ms < 0 ? '-' : ''
  const v = Math.abs(Math.round(ms))
  const m = Math.floor(v / 60000)
  const s = Math.floor((v % 60000) / 1000)
  const f = v % 1000
  return `${sign}${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}.${String(f).padStart(3, '0')}`
}

/**
 * 时长用人读格式——用于"已录 12 分 04 秒""预计缩短 0.3 秒"这类叙述位置。
 * 不足一分钟按秒显示到 0.1 秒（整数时不带 .0），否则显示"X 分 XX 秒"。
 */
export function dur(ms: number): string {
  const tenths = Math.round(Math.max(0, ms) / 100)
  if (tenths < 600) {
    const seconds = tenths / 10
    return `${Number.isInteger(seconds) ? seconds : seconds.toFixed(1)} 秒`
  }
  const total = Math.round(ms / 1000)
  const m = Math.floor(total / 60)
  const s = total % 60
  return `${m} 分 ${String(s).padStart(2, '0')} 秒`
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
  COMPRESS_SILENCE: '压缩停顿',
  KEEP_PROTECTED: '保护保留',
}

/** 生成成片时会被剪掉的决策类型：各类删除 + 压缩停顿（区间本身就是停顿中要删掉的部分）。 */
export function isCutAction(action: string): boolean {
  return action.startsWith('DELETE_') || action === 'COMPRESS_SILENCE'
}

export const LOUDNESS_TARGET_LABEL: Record<string, string> = {
  podcast: '播客 / 流媒体（-16 LUFS）',
  broadcast: '广播 EBU R128（-23 LUFS）',
}

/** 成片响度结果的一句话说明。 */
export function loudnessSummary(result: {
  targetLufs: number; inputLufs: number | null; outputLufs: number | null; gainDb: number; limitedBy: string | null
}): string {
  if (result.inputLufs == null || result.outputLufs == null) return '成片近乎静音，未调整响度。'
  const gain = `${result.gainDb > 0 ? '+' : ''}${result.gainDb.toFixed(1)} dB`
  const base = `响度 ${result.inputLufs.toFixed(1)} → ${result.outputLufs.toFixed(1)} LUFS（${gain}）`
  if (result.limitedBy === 'peak') return `${base}，为避免削波未完全达到 ${result.targetLufs} LUFS。`
  if (result.limitedBy === 'max_gain') return `${base}，原录音过轻，提升已达上限，未完全达到 ${result.targetLufs} LUFS。`
  return `${base}，已达到目标。`
}

export const GRADE_LABEL: Record<string, string> = {
  EXCELLENT: '优',
  GOOD: '良',
  FAIR: '一般',
  POOR: '差',
  UNUSABLE: '不可用',
}
