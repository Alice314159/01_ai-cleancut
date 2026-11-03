/**
 * 安卓 App 构建（npm run build:app，VITE_APP=1）的运行时配置。
 * App 里没有同源后端：首次打开时选择“连接电脑上的 CleanCut”或“先看演示”，
 * 选择记在本机，切换时清除并重新加载。普通网页构建中 IS_APP 恒为 false，相关代码会被摇树删除。
 */
export const IS_APP = import.meta.env.VITE_APP === '1'

const SERVER_KEY = 'cleancut.app.server'
const MODE_KEY = 'cleancut.app.mode'

export type AppMode = 'server' | 'demo' | null

function read(key: string): string | null {
  try { return localStorage.getItem(key) } catch { return null }
}

function write(key: string, value: string | null) {
  try {
    if (value === null) localStorage.removeItem(key)
    else localStorage.setItem(key, value)
  } catch { /* 存不下时本次仍可用 */ }
}

export function appMode(): AppMode {
  const mode = read(MODE_KEY)
  return mode === 'server' || mode === 'demo' ? mode : null
}

export function appServer(): string | null {
  return appMode() === 'server' ? read(SERVER_KEY) : null
}

/** 规范化用户输入的地址：补 http://、去掉末尾斜杠；不合法时返回 null。 */
export function normalizeServer(input: string): string | null {
  const trimmed = input.trim()
  if (!trimmed) return null
  const withScheme = /^https?:/i.test(trimmed) ? trimmed : `http://${trimmed}`
  try {
    const url = new URL(withScheme)
    if (!url.hostname) return null
    return `${url.protocol}//${url.host}`
  } catch {
    return null
  }
}

export function useServer(url: string) { write(SERVER_KEY, url); write(MODE_KEY, 'server') }
export function useDemo() { write(MODE_KEY, 'demo') }
export function resetAppMode() { write(MODE_KEY, null) }
