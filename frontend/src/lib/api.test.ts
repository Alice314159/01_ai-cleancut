import { afterEach, describe, expect, it, vi } from 'vitest'
import { api, DecisionConflictError, filenameFromDisposition } from './api'

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('api request handling', () => {
  it('returns the parsed JSON body on success', async () => {
    const session = { id: 's_1', title: '未命名录制' }
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse(200, session)))

    await expect(api.getSession('s_1')).resolves.toEqual(session)
  })

  it('maps a 404 to null instead of throwing, for endpoints that use requestOrNull', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse(404, { detail: 'quality report not ready yet' })))

    await expect(api.getQuality('s_1')).resolves.toBeNull()
  })

  it('still throws a 404 for endpoints that do not treat "not found" as absence', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse(404, { detail: 'session not found' })))

    await expect(api.getSession('missing')).rejects.toThrow('session not found')
  })

  it('surfaces the backend detail string as the error message', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse(422, { detail: '删除区间超出录音时长' })))

    await expect(api.addManualDecision('s_1', 0, 1000)).rejects.toThrow('删除区间超出录音时长')
  })

  it('falls back to the HTTP status text when the error body is not JSON', async () => {
    const response = new Response('not json', { status: 500, statusText: 'Internal Server Error' })
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response))

    await expect(api.getSession('s_1')).rejects.toThrow('not json')
  })

  it('raises a DecisionConflictError carrying the server\'s current decision on a 409 conflict', async () => {
    const current = { id: 'd_1', revision: 3, accepted: true }
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(
      jsonResponse(409, { detail: { code: 'decision_conflict', current } }),
    ))

    const promise = api.updateDecision('s_1', 'd_1', false, 2)
    await expect(promise).rejects.toBeInstanceOf(DecisionConflictError)
    await promise.catch((e: DecisionConflictError) => {
      expect(e.current).toEqual(current)
    })
  })

  it('does not mistake an unrelated 409 for a decision conflict', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse(409, { detail: '增强处理中，请完成后再渲染' })))

    const promise = api.render('s_1')
    await expect(promise).rejects.not.toBeInstanceOf(DecisionConflictError)
    await expect(promise).rejects.toThrow('增强处理中，请完成后再渲染')
  })

  it('sends importAudio as multipart form data without a manual Content-Type', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(201, { id: 's_1' }))
    vi.stubGlobal('fetch', fetchMock)

    const file = new File(['fake audio bytes'], 'clip.wav', { type: 'audio/wav' })
    await api.importAudio('s_1', file)

    const [, init] = fetchMock.mock.calls[0]
    expect(init.body).toBeInstanceOf(FormData)
    expect((init.body as FormData).get('file')).toBe(file)
    // Setting Content-Type ourselves would strip fetch's auto-generated multipart
    // boundary and break the upload — this is the exact regression to guard against.
    expect(init.headers ?? {}).not.toHaveProperty('Content-Type')
  })

  it('surfaces the backend detail on a failed import instead of a generic network error', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse(409, { detail: '该会话已导入过音频，无法重复导入' })))

    const file = new File(['x'], 'clip.wav', { type: 'audio/wav' })
    await expect(api.importAudio('s_1', file)).rejects.toThrow('该会话已导入过音频，无法重复导入')
  })
})

describe('filenameFromDisposition', () => {
  it('prefers the RFC 5987 encoded name so Chinese titles survive', () => {
    expect(filenameFromDisposition(`attachment; filename*=utf-8''%E7%AC%AC%E4%B8%80%E6%9C%9F-%E6%88%90%E7%89%87.mp3`)).toBe('第一期-成片.mp3')
  })

  it('falls back to the plain filename parameter', () => {
    expect(filenameFromDisposition('attachment; filename="episode-1.m4a"')).toBe('episode-1.m4a')
  })

  it('returns null when no name is given', () => {
    expect(filenameFromDisposition(null)).toBeNull()
    expect(filenameFromDisposition('inline')).toBeNull()
  })
})

describe('exportAudio', () => {
  it('requests the chosen format and bitrate and returns the server filename', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response('mp3-bytes', {
      status: 200, headers: { 'Content-Disposition': `attachment; filename*=utf-8''a%E6%88%90%E7%89%87.mp3` },
    }))
    vi.stubGlobal('fetch', fetchMock)
    const result = await api.exportAudio('rs_1', 'av_1', 'mp3', 256)
    expect(fetchMock.mock.calls[0][0]).toContain('/api/sessions/rs_1/versions/av_1/export?format=mp3&bitrate=256')
    expect(result.filename).toBe('a成片.mp3')
    expect(await result.blob.text()).toBe('mp3-bytes')
  })

  it('surfaces the server error message', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse(503, { detail: '本机 ffmpeg 缺少 aac 编码器' })))
    await expect(api.exportAudio('rs_1', 'av_1', 'm4a', 192)).rejects.toThrow('缺少 aac 编码器')
  })
})
