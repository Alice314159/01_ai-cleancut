import { afterEach, describe, expect, it, vi } from 'vitest'
import { authHeaders, normalizeCode, setAccessToken, withToken } from './auth'

function memoryStorage() {
  const data = new Map<string, string>()
  return { getItem: (k: string) => data.get(k) ?? null, setItem: (k: string, v: string) => { data.set(k, v) }, removeItem: (k: string) => { data.delete(k) } }
}

afterEach(() => vi.unstubAllGlobals())

describe('pairing code', () => {
  it('normalizes dashes, spaces and case', () => {
    expect(normalizeCode(' k7q2-9xma ')).toBe('K7Q29XMA')
  })

  it('adds the code to headers and media URLs only when set', () => {
    vi.stubGlobal('localStorage', memoryStorage())
    expect(authHeaders()).toEqual({})
    expect(withToken('http://x/api/a')).toBe('http://x/api/a')
    setAccessToken('k7q2-9xma')
    expect(authHeaders()).toEqual({ 'X-CleanCut-Token': 'K7Q29XMA' })
    expect(withToken('http://x/api/a')).toBe('http://x/api/a?token=K7Q29XMA')
    expect(withToken('ws://x/ws/r/1?resume=1')).toBe('ws://x/ws/r/1?resume=1&token=K7Q29XMA')
    expect(withToken('blob:http://x/123')).toBe('blob:http://x/123')
  })
})
