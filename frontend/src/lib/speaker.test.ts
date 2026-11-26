import { describe, expect, it } from 'vitest'
import { speakerName, speakerSummary } from './format'

describe('说话人显示', () => {
  it('编号转字母，与后端导出一致', () => {
    expect([0, 1, 25, 26].map((n) => speakerName(n))).toEqual(['说话人 A', '说话人 B', '说话人 Z', '说话人 AA'])
  })
  it('用户起的名字优先', () => {
    expect(speakerName(1, { 1: '嘉宾' })).toBe('嘉宾')
    expect(speakerName(0, { 1: '嘉宾' })).toBe('说话人 A')
    expect(speakerName(0, null)).toBe('说话人 A')
  })
  it('质量报告里的说话人一栏', () => {
    expect(speakerSummary('unavailable', null)).toBeNull()
    expect(speakerSummary(undefined, null)).toBeNull()
    expect(speakerSummary('unavailable', null, true)).toBe('未区分（可在剪辑页开始）')
    expect(speakerSummary('running', null)).toBe('正在区分…')
    expect(speakerSummary('done', 2)).toBe('2 位')
    expect(speakerSummary('done', 0)).toBe('未检测到')
    expect(speakerSummary('failed', null)).toBe('区分失败')
  })
})
