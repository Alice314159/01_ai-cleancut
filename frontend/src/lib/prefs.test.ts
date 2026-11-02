import { afterEach, describe, expect, it, vi } from 'vitest'
import { isBoolean, oneOf, readPref, writePref } from './prefs'

function memoryStorage() {
  const data = new Map<string, string>()
  return {
    getItem: (k: string) => data.get(k) ?? null,
    setItem: (k: string, v: string) => { data.set(k, v) },
  }
}

afterEach(() => vi.unstubAllGlobals())

describe('prefs', () => {
  it('round-trips valid values', () => {
    vi.stubGlobal('localStorage', memoryStorage())
    writePref('bitrate', 256)
    expect(readPref('bitrate', 192, oneOf(96, 128, 192, 256, 320))).toBe(256)
  })

  it('falls back when the stored value is invalid or missing', () => {
    vi.stubGlobal('localStorage', memoryStorage())
    expect(readPref('smooth', false, isBoolean)).toBe(false)
    writePref('bitrate', 999)
    expect(readPref('bitrate', 192, oneOf(96, 128, 192, 256, 320))).toBe(192)
  })

  it('survives storage that throws', () => {
    vi.stubGlobal('localStorage', { getItem: () => { throw new Error('denied') }, setItem: () => { throw new Error('denied') } })
    expect(() => writePref('x', 1)).not.toThrow()
    expect(readPref('x', 'fallback', (v): v is string => typeof v === 'string')).toBe('fallback')
  })
})
