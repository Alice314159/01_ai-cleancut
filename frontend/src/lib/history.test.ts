import { describe, expect, it } from 'vitest'
import type { EditDecision } from '../types/api'
import { HISTORY_LIMIT, makeEntry, planRestore, push } from './history'

const d = (id: string, accepted: boolean | null, revision = 0) =>
  ({ id, startMs: 0, endMs: 100, action: 'DELETE_SELECTION', reason: '', text: '', confidence: 1, source: 'user',
    hardProtected: false, reviewRequired: true, accepted, revision }) as EditDecision

describe('makeEntry', () => {
  it('keeps only affected decisions', () => {
    const entry = makeEntry('接受', [d('a', null), d('b', true)], [d('a', true, 1), d('b', true)])
    expect(entry?.before.map((x) => x.id)).toEqual(['a'])
    expect(entry?.after.map((x) => [x.id, x.accepted])).toEqual([['a', true]])
  })

  it('records additions and removals', () => {
    const entry = makeEntry('删除选中', [d('a', true)], [d('a', true), d('n', true)])
    expect(entry?.before).toEqual([])
    expect(entry?.after.map((x) => x.id)).toEqual(['n'])
  })

  it('returns null when nothing changed', () => {
    expect(makeEntry('无变化', [d('a', true)], [d('a', true, 3)])).toBeNull()
  })
})

describe('planRestore', () => {
  it('undoing an addition removes it with the live revision', () => {
    const entry = makeEntry('删除选中', [], [d('n', true)])!
    expect(planRestore(entry.before, entry.after, [d('n', true, 0)])).toEqual({ target: [], remove: ['n'], expectedRevisions: { n: 0 } })
  })

  it('redoing an addition recreates it without a revision check', () => {
    const entry = makeEntry('删除选中', [], [d('n', true)])!
    expect(planRestore(entry.after, entry.before, [])).toEqual({ target: [d('n', true)], remove: [], expectedRevisions: {} })
  })

  it('undoing a change targets the previous state using the current revision', () => {
    const entry = makeEntry('接受', [d('a', null, 0)], [d('a', true, 1)])!
    const plan = planRestore(entry.before, entry.after, [d('a', true, 4)])
    expect(plan.target[0].accepted).toBeNull()
    expect(plan.expectedRevisions).toEqual({ a: 4 })
  })
})

describe('push', () => {
  it('caps the stack', () => {
    let stack = [] as ReturnType<typeof push>
    for (let i = 0; i < HISTORY_LIMIT + 5; i += 1) stack = push(stack, makeEntry(String(i), [], [d(`x${i}`, true)]))
    expect(stack).toHaveLength(HISTORY_LIMIT)
    expect(stack[0].label).toBe('5')
  })
})
