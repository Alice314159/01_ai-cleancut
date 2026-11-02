import type { EditDecision } from '../types/api'

/**
 * 剪辑撤销 / 重做。每条记录只存"受影响的决策"在操作前后的状态：
 * before 里有、after 里没有 = 这次操作删掉的；反之 = 新增的；两边都有 = 改了接受状态。
 */
export type HistoryEntry = { label: string; before: EditDecision[]; after: EditDecision[] }

export type RestorePlan = { target: EditDecision[]; remove: string[]; expectedRevisions: Record<string, number> }

export const HISTORY_LIMIT = 50

export function makeEntry(label: string, previous: EditDecision[], next: EditDecision[]): HistoryEntry | null {
  const prev = new Map(previous.map((d) => [d.id, d]))
  const curr = new Map(next.map((d) => [d.id, d]))
  const ids = new Set([...prev.keys(), ...curr.keys()])
  const before: EditDecision[] = []
  const after: EditDecision[] = []
  for (const id of ids) {
    const a = prev.get(id)
    const b = curr.get(id)
    if (a && b && a.accepted === b.accepted) continue
    if (a) before.push(a)
    if (b) after.push(b)
  }
  return before.length || after.length ? { label, before, after } : null
}

/** 从 from 状态回到 to 状态需要的恢复请求；修订号取自当前服务器状态（current）。 */
export function planRestore(to: EditDecision[], from: EditDecision[], current: EditDecision[]): RestorePlan {
  const toIds = new Set(to.map((d) => d.id))
  const now = new Map(current.map((d) => [d.id, d]))
  const remove = from.filter((d) => !toIds.has(d.id)).map((d) => d.id)
  const expectedRevisions: Record<string, number> = {}
  for (const id of [...toIds, ...remove]) {
    const live = now.get(id)
    if (live) expectedRevisions[id] = live.revision ?? 0
  }
  return { target: to, remove, expectedRevisions }
}

export function push(stack: HistoryEntry[], entry: HistoryEntry | null): HistoryEntry[] {
  return entry ? [...stack, entry].slice(-HISTORY_LIMIT) : stack
}
