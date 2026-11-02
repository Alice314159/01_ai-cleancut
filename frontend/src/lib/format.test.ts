import { describe, expect, it } from 'vitest'
import { db, dur, pct, tc, toDbfs } from './format'

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
