/**
 * 演示模式用的合成音频：按"词"的时间表生成类似说话节奏的音节音（带包络和音高起伏），
 * 词与词之间是轻微底噪。不追求像人声，只要试听、波形、剪辑前后的长度变化看得出来。
 */
export const DEMO_RATE = 16000

export type Word = { text: string; startMs: number; endMs: number }

export function synthesize(words: Word[], durationMs: number, noise = 0.004, seed = 1): Float32Array {
  const total = Math.round((durationMs / 1000) * DEMO_RATE)
  const pcm = new Float32Array(total)
  let state = seed
  const random = () => { state = (state * 1664525 + 1013904223) % 4294967296; return state / 4294967296 - 0.5 }
  for (let i = 0; i < total; i += 1) pcm[i] = random() * 2 * noise
  words.forEach((word, index) => {
    const start = Math.round((word.startMs / 1000) * DEMO_RATE)
    const end = Math.min(total, Math.round((word.endMs / 1000) * DEMO_RATE))
    const length = Math.max(1, end - start)
    const base = 150 + ((index * 37) % 90)
    // 每个字一个音节：中文按字数切，英文按整体一个音节
    const syllables = Math.max(1, Math.min(6, [...word.text.replace(/[，。！？、,.!?\s]/g, '')].length))
    for (let i = 0; i < length; i += 1) {
      const t = i / DEMO_RATE
      const position = (i / length) * syllables
      const within = position - Math.floor(position)
      const envelope = Math.sin(Math.PI * within) ** 0.8
      const pitch = base * (1 + 0.08 * Math.sin(2 * Math.PI * 3 * t + index))
      const tone = Math.sin(2 * Math.PI * pitch * t) + 0.45 * Math.sin(4 * Math.PI * pitch * t) + 0.2 * Math.sin(6 * Math.PI * pitch * t)
      pcm[start + i] += 0.22 * envelope * tone
    }
  })
  return pcm
}

/** 按删除区间拼接，返回剪后的 PCM。 */
export function applyCuts(pcm: Float32Array, cuts: { startMs: number; endMs: number }[]): Float32Array {
  const keep: [number, number][] = []
  let cursor = 0
  for (const cut of cuts) {
    const start = Math.round((cut.startMs / 1000) * DEMO_RATE)
    const end = Math.round((cut.endMs / 1000) * DEMO_RATE)
    if (start > cursor) keep.push([cursor, start])
    cursor = Math.max(cursor, end)
  }
  if (cursor < pcm.length) keep.push([cursor, pcm.length])
  const out = new Float32Array(keep.reduce((sum, [a, b]) => sum + b - a, 0))
  let offset = 0
  for (const [a, b] of keep) { out.set(pcm.subarray(a, b), offset); offset += b - a }
  return out
}

export function scale(pcm: Float32Array, gain: number): Float32Array {
  return pcm.map((v) => Math.max(-1, Math.min(1, v * gain)))
}

export function toWav(pcm: Float32Array): Blob {
  const buffer = new ArrayBuffer(44 + pcm.length * 2)
  const view = new DataView(buffer)
  const ascii = (offset: number, text: string) => [...text].forEach((c, i) => view.setUint8(offset + i, c.charCodeAt(0)))
  ascii(0, 'RIFF'); view.setUint32(4, 36 + pcm.length * 2, true); ascii(8, 'WAVE')
  ascii(12, 'fmt '); view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true)
  view.setUint32(24, DEMO_RATE, true); view.setUint32(28, DEMO_RATE * 2, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true)
  ascii(36, 'data'); view.setUint32(40, pcm.length * 2, true)
  pcm.forEach((v, i) => view.setInt16(44 + i * 2, Math.round(Math.max(-1, Math.min(1, v)) * 32767), true))
  return new Blob([buffer], { type: 'audio/wav' })
}

export function envelope(pcm: Float32Array, buckets: number): number[] {
  const size = Math.max(1, Math.floor(pcm.length / buckets))
  const out: number[] = []
  for (let b = 0; b < buckets; b += 1) {
    let peak = 0
    for (let i = b * size; i < Math.min(pcm.length, (b + 1) * size); i += 1) peak = Math.max(peak, Math.abs(pcm[i]))
    out.push(Math.min(1, peak * 2.5))
  }
  return out
}
