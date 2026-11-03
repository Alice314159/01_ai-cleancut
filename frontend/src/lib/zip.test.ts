import { describe, expect, it } from 'vitest'
import { crc32, zipSingleFile } from './zip'

describe('zip', () => {
  it('computes the standard CRC-32', () => {
    expect(crc32(new TextEncoder().encode('123456789'))).toBe(0xcbf43926)
  })

  it('stores one file with local header, central directory and end record', () => {
    const data = new TextEncoder().encode('hello')
    const zip = zipSingleFile('成片.srt', data)
    const view = new DataView(zip.buffer)
    const name = new TextEncoder().encode('成片.srt')
    expect(view.getUint32(0, true)).toBe(0x04034b50)
    expect(view.getUint32(18, true)).toBe(5)
    expect(new TextDecoder().decode(zip.slice(30, 30 + name.length))).toBe('成片.srt')
    expect(new TextDecoder().decode(zip.slice(30 + name.length, 35 + name.length))).toBe('hello')
    expect(view.getUint32(zip.length - 22, true)).toBe(0x06054b50)
    expect(zip.length).toBe(30 + name.length + 5 + 46 + name.length + 22)
  })
})
