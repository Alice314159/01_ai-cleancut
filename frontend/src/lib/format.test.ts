import { describe, expect, it } from 'vitest'
import { createdLabel, db, dur, isCutAction, loudnessSummary, pct, tc, toDbfs } from './format'

describe('tc', () => {
  it('formats zero as mm:ss.mmm', () => {
    expect(tc(0)).toBe('00:00.000')
  })

  it('pads minutes, seconds and milliseconds', () => {
    expect(tc(65432)).toBe('01:05.432')
  })

  it('prefixes a sign for negative offsets without dropping the leading zero', () => {
    expect(tc(-1500)).toBe('-00:01.500')
  })

  it('rounds sub-millisecond input the same way the cut boundaries are rounded', () => {
    expect(tc(999.6)).toBe('00:01.000')
  })
})

describe('dur', () => {
  it('omits the minutes segment under a minute', () => {
    expect(dur(45000)).toBe('45 秒')
  })

  it('shows zero seconds as "0 秒", not "0 分 00 秒"', () => {
    expect(dur(0)).toBe('0 秒')
  })

  it('pads seconds once minutes are shown', () => {
    expect(dur(65000)).toBe('1 分 05 秒')
  })
})

describe('pct', () => {
  it('defaults to one decimal place', () => {
    expect(pct(0.5)).toBe('50.0%')
  })

  it('honors an explicit digit count', () => {
    expect(pct(0.1234, 2)).toBe('12.34%')
  })
})

describe('db', () => {
  it('prefixes a plus sign for positive values', () => {
    expect(db(3.456)).toBe('+3.5')
  })

  it('relies on toFixed for the minus sign on negative values', () => {
    expect(db(-2)).toBe('-2.0')
  })

  it('does not add a plus sign for exactly zero', () => {
    expect(db(0)).toBe('0.0')
  })
})

describe('toDbfs', () => {
  it('floors silence and non-positive amplitudes at -60 dBFS instead of -Infinity', () => {
    expect(toDbfs(0)).toBe(-60)
    expect(toDbfs(-1)).toBe(-60)
  })

  it('maps full scale to 0 dBFS', () => {
    expect(toDbfs(1)).toBe(0)
  })

  it('clamps very quiet non-zero amplitudes at the -60 dBFS floor', () => {
    expect(toDbfs(0.0000001)).toBe(-60)
  })

  it('matches the standard 20*log10 amplitude-to-dBFS conversion', () => {
    expect(toDbfs(0.5)).toBeCloseTo(20 * Math.log10(0.5), 10)
  })
})

describe('createdLabel', () => {
  const now = new Date(2026, 10, 2, 15, 0)

  it('returns null for missing or invalid timestamps', () => {
    expect(createdLabel(null, now)).toBeNull()
    expect(createdLabel('not-a-date', now)).toBeNull()
  })

  it('shows only the time for sessions created today', () => {
    expect(createdLabel(new Date(2026, 10, 2, 9, 5).toISOString(), now)).toBe('今天 09:05')
  })

  it('shows month and day within the same year', () => {
    expect(createdLabel(new Date(2026, 0, 31, 23, 59).toISOString(), now)).toBe('1月31日 23:59')
  })

  it('includes the year for older sessions', () => {
    expect(createdLabel(new Date(2025, 11, 1, 8, 0).toISOString(), now)).toBe('2025年12月1日 08:00')
  })
})

describe('isCutAction', () => {
  it('treats deletions and pause compression as cuts', () => {
    expect(isCutAction('DELETE_SELECTION')).toBe(true)
    expect(isCutAction('COMPRESS_SILENCE')).toBe(true)
    expect(isCutAction('REPLACE_AUDIO')).toBe(false)
    expect(isCutAction('KEEP_PROTECTED')).toBe(false)
  })
})

describe('loudnessSummary', () => {
  const base = { targetLufs: -16, inputLufs: -26, outputLufs: -16, gainDb: 10, limitedBy: null }

  it('reports the achieved loudness and gain', () => {
    expect(loudnessSummary(base)).toBe('响度 -26.0 → -16.0 LUFS（+10.0 dB），已达到目标。')
  })

  it('explains when the peak ceiling prevented reaching the target', () => {
    expect(loudnessSummary({ ...base, outputLufs: -17.5, gainDb: 8.5, limitedBy: 'peak' })).toContain('为避免削波')
  })

  it('explains when the recording was too quiet', () => {
    expect(loudnessSummary({ ...base, inputLufs: -50, outputLufs: -30, gainDb: 20, limitedBy: 'max_gain' })).toContain('提升已达上限')
  })

  it('handles near-silent output', () => {
    expect(loudnessSummary({ ...base, inputLufs: null, outputLufs: null, gainDb: 0 })).toBe('成片近乎静音，未调整响度。')
  })
})
