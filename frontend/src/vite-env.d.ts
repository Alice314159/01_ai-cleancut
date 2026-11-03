/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_BASE?: string
  /** '1' 表示演示构建：浏览器内模拟后端 */
  readonly VITE_DEMO?: string
  /** '1' 表示安卓 App 构建：服务器地址运行时填写，可选演示模式 */
  readonly VITE_APP?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
