import { describe, expect, it } from 'vitest'
import { ChunkQueue, encodeFrame } from './chunkQueue'

const buf = (...bytes: number[]) => new Uint8Array(bytes).buffer

describe('encodeFrame', () => {
  it('prefixes magic and big-endian sequence number', () => {
    const frame = new Uint8Array(encodeFrame(258, buf(9, 8)))
    expect([...frame]).toEqual([0x43, 0x43, 0x4b, 0x31, 0, 0, 1, 2, 9, 8])
  })
})

describe('ChunkQueue', () => {
  it('assigns increasing sequence numbers and drops acked chunks', () => {
    const q = new ChunkQueue()
    q.add(buf(1)); q.add(buf(2)); q.add(buf(3))
    expect(q.ack(1).map((c) => c.seq)).toEqual([0, 1])
    expect(q.pending.map((c) => c.seq)).toEqual([2])
    expect(q.nextSeq).toBe(3)
  })

  it('resends from the server position after reconnecting', () => {
    const q = new ChunkQueue()
    for (let i = 0; i < 5; i += 1) q.add(buf(i))
    q.ack(0)
    expect(q.resendFrom(3).map((c) => c.seq)).toEqual([3, 4])
    expect(q.size).toBe(2)
    expect(q.resendFrom(0).map((c) => c.seq)).toEqual([3, 4])
  })

  it('restores stored chunks and continues numbering after them', () => {
    const q = new ChunkQueue()
    q.restore([{ seq: 7, data: buf(7) }, { seq: 6, data: buf(6) }])
    expect(q.pending.map((c) => c.seq)).toEqual([6, 7])
    expect(q.add(buf(8)).seq).toBe(8)
    expect(q.bytes).toBe(3)
  })
})
