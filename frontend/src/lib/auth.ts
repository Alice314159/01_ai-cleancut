/**
 * 局域网配对码（电脑以 --lan / --app 启动时终端显示）。保存在本机浏览器 / App 里，
 * 随每个请求发送：fetch 用请求头，<audio>/<img>/下载链接/WebSocket 用查询参数。
 * 链接里带 #pair=XXXX-XXXX 时自动保存并从地址栏移除。
 */
const KEY = 'cleancut.accessToken'
export const PAIRING_EVENT = 'cleancut:pairing-required'

export function normalizeCode(code: string): string {
  return code.replace(/[\s-]/g, '').toUpperCase()
}

export function getAccessToken(): string | null {
  try { return localStorage.getItem(KEY) } catch { return null }
}

export function setAccessToken(code: string | null) {
  try {
    if (code) localStorage.setItem(KEY, normalizeCode(code))
    else localStorage.removeItem(KEY)
  } catch { /* 存不下时本次会话里重新输入即可 */ }
}

/** 启动时调用：读取 #pair=… 并清掉，避免配对码留在地址栏或被分享出去。 */
export function captureFromHash(): void {
  if (typeof window === 'undefined') return
  const match = /(?:^#|&)pair=([^&]+)/.exec(window.location.hash)
  if (!match) return
  setAccessToken(decodeURIComponent(match[1]))
  history.replaceState(null, '', window.location.pathname + window.location.search)
}

export function authHeaders(): Record<string, string> {
  const token = getAccessToken()
  return token ? { 'X-CleanCut-Token': token } : {}
}

/** 给媒体 / 下载 / WebSocket 链接加上配对码（这些请求无法自定义请求头）。 */
export function withToken(url: string): string {
  const token = getAccessToken()
  if (!token || url.startsWith('blob:') || url.startsWith('data:')) return url
  return `${url}${url.includes('?') ? '&' : '?'}token=${encodeURIComponent(token)}`
}

/** 后端返回“需要配对码”时通知界面弹出输入框。 */
export function notifyPairingRequired(message: string) {
  if (typeof window !== 'undefined') window.dispatchEvent(new CustomEvent(PAIRING_EVENT, { detail: message }))
}
