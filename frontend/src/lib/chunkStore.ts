/**
 * 未确认录音分片的本机持久化（IndexedDB）。页面被关闭或刷新时，下次打开同一会话可继续补传。
 * 浏览器不支持或被禁止时全部操作静默变为空操作，不影响正常录制。
 */
import type { Chunk } from './chunkQueue'

const DB_NAME = 'cleancut-recording'
const STORE = 'chunks'
let dbPromise: Promise<IDBDatabase | null> | null = null

function open(): Promise<IDBDatabase | null> {
  if (dbPromise) return dbPromise
  dbPromise = new Promise((resolve) => {
    try {
      if (typeof indexedDB === 'undefined') { resolve(null); return }
      const request = indexedDB.open(DB_NAME, 1)
      request.onupgradeneeded = () => { request.result.createObjectStore(STORE, { keyPath: ['sessionId', 'seq'] }) }
      request.onsuccess = () => resolve(request.result)
      request.onerror = () => resolve(null)
      request.onblocked = () => resolve(null)
    } catch { resolve(null) }
  })
  return dbPromise
}

async function run<T>(mode: IDBTransactionMode, body: (store: IDBObjectStore) => IDBRequest<T> | void): Promise<T | undefined> {
  const db = await open()
  if (!db) return undefined
  return new Promise((resolve) => {
    try {
      const tx = db.transaction(STORE, mode)
      const request = body(tx.objectStore(STORE))
      tx.oncomplete = () => resolve(request ? request.result : undefined)
      tx.onerror = () => resolve(undefined)
      tx.onabort = () => resolve(undefined)
    } catch { resolve(undefined) }
  })
}

const range = (sessionId: string, from = 0, to = Number.MAX_SAFE_INTEGER) => IDBKeyRange.bound([sessionId, from], [sessionId, to])

export const chunkStore = {
  put(sessionId: string, chunk: Chunk) {
    return run('readwrite', (store) => store.put({ sessionId, seq: chunk.seq, data: chunk.data }))
  },
  removeUpTo(sessionId: string, seq: number) {
    return run('readwrite', (store) => store.delete(range(sessionId, 0, seq)))
  },
  clear(sessionId: string) {
    return run('readwrite', (store) => store.delete(range(sessionId)))
  },
  async list(sessionId: string): Promise<Chunk[]> {
    const rows = await run<{ seq: number; data: ArrayBuffer }[]>('readonly', (store) => store.getAll(range(sessionId)))
    return (rows ?? []).map((row) => ({ seq: row.seq, data: row.data }))
  },
}
