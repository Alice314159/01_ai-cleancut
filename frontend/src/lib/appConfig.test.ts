import { afterEach, describe, expect, it, vi } from 'vitest'
import { appMode, appServer, normalizeServer, resetAppMode, useDemo, useServer } from './appConfig'

function memoryStorage() {
  const data = new Map<string, string>()
  return { getItem: (k: string) => data.get(k) ?? null, setItem: (k: string, v: string) => { data.set(k, v) }, removeItem: (k: string) => { data.delete(k) } }
}

afterEach(() => vi.unstubAllGlobals())

describe('normalizeServer', () => {
  it('adds http:// and strips trailing slashes and paths', () => {
    expect(normalizeServer('192.168.1.23:8001')).toBe('http://192.168.1.23:8001')
    expect(normalizeServer(' http://192.168.1.23:8001/ ')).toBe('http://192.168.1.23:8001')
    expect(normalizeServer('https://mac.local:8001/api')).toBe('https://mac.local:8001')
  })

  it('rejects empty or malformed input', () => {
    expect(normalizeServer('   ')).toBeNull()
    expect(normalizeServer('http://')).toBeNull()
  })
})

describe('app mode', () => {
  it('remembers the chosen server or demo mode and can be reset', () => {
    vi.stubGlobal('localStorage', memoryStorage())
    expect(appMode()).toBeNull()
    useServer('http://192.168.1.23:8001')
    expect(appMode()).toBe('server')
    expect(appServer()).toBe('http://192.168.1.23:8001')
    useDemo()
    expect(appMode()).toBe('demo')
    expect(appServer()).toBeNull()
    resetAppMode()
    expect(appMode()).toBeNull()
  })
})
