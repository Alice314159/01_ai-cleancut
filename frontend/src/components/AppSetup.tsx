import { useState, type FormEvent } from 'react'
import { normalizeServer, useDemo, useServer } from '../lib/appConfig'
import { normalizeCode, setAccessToken } from '../lib/auth'

/** 安卓 App 首次打开：连接电脑上运行的 CleanCut，或先看演示。 */
export function AppSetup() {
  const [address, setAddress] = useState('')
  const [code, setCode] = useState('')
  const [checking, setChecking] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const connect = async (event: FormEvent) => {
    event.preventDefault()
    const url = normalizeServer(address)
    if (!url) { setError('请填写电脑的地址，例如 192.168.1.23:8001'); return }
    setChecking(true); setError(null)
    const controller = new AbortController()
    const timer = window.setTimeout(() => controller.abort(), 5000)
    try {
      const token = normalizeCode(code)
      const response = await fetch(`${url}/api/auth/check`, { signal: controller.signal, headers: token ? { 'X-CleanCut-Token': token } : {} })
      if (response.status === 401 || response.status === 429) {
        const body = await response.json().catch(() => null)
        setError(body?.detail ?? '配对码不正确')
        return
      }
      if (!response.ok) throw new Error(String(response.status))
      setAccessToken(token || null)
      useServer(url)
      window.location.reload()
    } catch {
      setError(`连不上 ${url}。请确认电脑上已运行 ./start.sh --app，手机和电脑在同一个 Wi-Fi，且电脑防火墙允许 8001 端口。`)
    } finally {
      window.clearTimeout(timer)
      setChecking(false)
    }
  }

  return (
    <main className="app-setup">
      <div className="app-setup__brand">CleanCut<span>.</span></div>
      <h1>连接电脑上的 CleanCut</h1>
      <p>转写、降噪和剪辑都在你的电脑上完成，手机负责录音、试听和剪辑操作。</p>
      <ol className="app-setup__steps">
        <li>在电脑的项目目录运行 <code>./start.sh --app</code></li>
        <li>手机连上和电脑相同的 Wi-Fi</li>
        <li>把终端里显示的“App 连接地址”和“配对码”填在下面</li>
      </ol>
      <form onSubmit={connect} className="app-setup__form">
        <input aria-label="电脑地址" inputMode="url" autoCapitalize="off" autoCorrect="off" placeholder="192.168.1.23:8001"
          value={address} onChange={(event) => setAddress(event.target.value)} disabled={checking} />
        <input aria-label="配对码" autoCapitalize="characters" autoCorrect="off" spellCheck={false} placeholder="配对码 XXXX-XXXX"
          value={code} onChange={(event) => setCode(event.target.value)} disabled={checking} />
        <button className="btn btn--primary" type="submit" disabled={checking}>{checking ? '正在连接…' : '连接'}</button>
      </form>
      {error && <p className="app-setup__error" role="alert">{error}</p>}
      <p className="app-setup__hint">在电脑上的安卓模拟器里测试时填 <code>10.0.2.2:8001</code>；在电脑浏览器里测试时填 <code>127.0.0.1:8001</code>。</p>
      <div className="app-setup__demo">
        <span>暂时没有电脑？</span>
        <button className="btn" type="button" onClick={() => { useDemo(); window.location.reload() }}>先看演示</button>
      </div>
    </main>
  )
}
