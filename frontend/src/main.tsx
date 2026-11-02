import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App'
import { installDemo } from './demo/server'
import './styles/app.css'

function mount() {
  createRoot(document.getElementById('root')!).render(
    <StrictMode>
      <App />
    </StrictMode>,
  )
}

// 演示构建（npm run build:demo）在浏览器内模拟后端，打包成单个 HTML 即可离线预览。
// 条件在构建时就是常量，正常构建会把演示代码整个摇树删掉（构建后可 grep 确认）。
if (import.meta.env.VITE_DEMO === '1') installDemo()
mount()

// “添加到主屏幕”后可像 App 一样打开。仅在 https / localhost 下注册；
// 本地文件、内嵌预览等环境不支持时静默跳过。
if ('serviceWorker' in navigator && window.isSecureContext && /^https?:$/.test(window.location.protocol)) {
  window.addEventListener('load', () => { navigator.serviceWorker.register('./sw.js').catch(() => undefined) })
}
