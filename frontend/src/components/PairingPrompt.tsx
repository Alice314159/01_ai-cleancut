import { useEffect, useState, type FormEvent } from 'react'
import { PAIRING_EVENT, setAccessToken } from '../lib/auth'

/**
 * 电脑以 --lan / --app 启动时后端要求配对码。任何请求被拒都会触发这个对话框，
 * 输入终端显示的配对码后保存在本机并重新加载。
 */
export function PairingPrompt() {
  const [message, setMessage] = useState<string | null>(null)
  const [code, setCode] = useState('')

  useEffect(() => {
    const show = (event: Event) => setMessage((event as CustomEvent<string>).detail || '需要配对码')
    window.addEventListener(PAIRING_EVENT, show)
    return () => window.removeEventListener(PAIRING_EVENT, show)
  }, [])

  if (!message) return null
  const submit = (event: FormEvent) => {
    event.preventDefault()
    if (!code.trim()) return
    setAccessToken(code)
    window.location.reload()
  }
  return (
    <div className="pairing-backdrop">
      <form className="pairing-dialog" role="dialog" aria-modal="true" aria-labelledby="pairing-title" onSubmit={submit}>
        <h2 id="pairing-title">输入配对码</h2>
        <p>{message}</p>
        <p className="card__hint">配对码显示在电脑上运行 <code>./start.sh --lan</code> 或 <code>--app</code> 的终端里，形如 K7Q2-9XMA。</p>
        <input aria-label="配对码" autoFocus autoCapitalize="characters" autoCorrect="off" spellCheck={false} placeholder="XXXX-XXXX"
          value={code} onChange={(event) => setCode(event.target.value)} />
        <button className="btn btn--primary" type="submit" disabled={!code.trim()}>确定</button>
      </form>
    </div>
  )
}
