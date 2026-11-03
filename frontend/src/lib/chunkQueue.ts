/**
 * 断线续传的待确认分片队列。每个录音分片带递增序号；服务端写入后回 ack，
 * 收到 ack 的分片才从队列移除。断线期间新分片继续入队，重连后从服务端给的序号补传。
 */
export type Chunk = { seq: number; data: ArrayBuffer }

export const CHUNK_MAGIC = [0x43, 0x43, 0x4b, 0x31] // "CCK1"

/** 帧格式：CCK1 + 4 字节大端序号 + 音频数据（与后端 recording_ws.CHUNK_MAGIC 对应）。 */
export function encodeFrame(seq: number, data: ArrayBuffer): ArrayBuffer {
  const out = new Uint8Array(8 + data.byteLength)
  out.set(CHUNK_MAGIC, 0)
  new DataView(out.buffer).setUint32(4, seq, false)
  out.set(new Uint8Array(data), 8)
  return out.buffer
}

export class ChunkQueue {
  private items: Chunk[] = []
  private next = 0

  get nextSeq() { return this.next }
  get size() { return this.items.length }
  get bytes() { return this.items.reduce((sum, c) => sum + c.data.byteLength, 0) }
  get pending(): readonly Chunk[] { return this.items }

  add(data: ArrayBuffer): Chunk {
    const chunk = { seq: this.next++, data }
    this.items.push(chunk)
    return chunk
  }

  /** 服务端确认写入了 seq（按序写入，所以之前的也都已写入）。返回被移除的分片。 */
  ack(seq: number): Chunk[] {
    const done = this.items.filter((c) => c.seq <= seq)
    this.items = this.items.filter((c) => c.seq > seq)
    return done
  }

  /** 服务端下一个需要的序号；更早的分片已在服务端，丢弃并返回剩余要补传的。 */
  resendFrom(nextSeq: number): Chunk[] {
    if (nextSeq > 0) this.ack(nextSeq - 1)
    return [...this.items]
  }

  /** 页面重新打开时，从本机存储恢复未确认的分片。 */
  restore(chunks: Chunk[]) {
    this.items = [...chunks].sort((a, b) => a.seq - b.seq)
    this.next = this.items.length ? this.items[this.items.length - 1].seq + 1 : 0
  }
}
