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
