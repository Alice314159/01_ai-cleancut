import type { ReactNode } from 'react'

export type RouteKey = 'record' | 'enhance' | 'review' | 'sessions'

export const ROUTES: {
  key: RouteKey
  label: string
  glyph: string
  /** 侧栏左侧的编号——只在流程真的有先后时才用编号，这里确实有 */
  step: string
  badge?: string
}[] = [
  // 按实际使用顺序：先在会话里新建/导入，再录制、（可选）增强，最后剪辑并导出。
  { key: 'sessions', label: '会话', glyph: '☰', step: '00' },
  { key: 'record', label: '录制', glyph: '●', step: '01' },
  { key: 'enhance', label: '增强', glyph: '≈', step: '02', badge: '可选' },
  { key: 'review', label: '剪辑导出', glyph: '✂', step: '03' },
]

/**
 * 桌面用左侧栏，移动用底部 tab。同一套页面组件，靠容器查询切换布局，
 * 不做两份实现——录制页在手机上要能用，是这个产品的基本前提而不是附加需求。
 */
export function AppShell({
  route,
  sessionId,
  onNavigate,
  strip,
  children,
}: {
  route: RouteKey
  sessionId: string | null
  onNavigate: (r: RouteKey) => void
  strip: ReactNode
  children: ReactNode
}) {
  return (
    <div className="app-root">
      <a className="skip-link" href="#main-content">
        跳转到主要内容
      </a>
      <div className="shell">
        <nav className="rail" aria-label="主导航">
          <div className="rail__brand">
            <div className="rail__mark">
              CleanCut<span>.</span>
            </div>
            <div className="rail__sub">中英文口播去重与剪辑</div>
          </div>

          <div className="rail__nav">
            {ROUTES.map((r) => (
              <button
                key={r.key}
                className="navitem"
                aria-current={route === r.key ? 'page' : undefined}
                onClick={() => onNavigate(r.key)}
              >
                <span className="navitem__idx mono">{r.step}</span>
                <span>{r.label}</span>
                {r.badge && <span className="navitem__badge">{r.badge}</span>}
              </button>
            ))}
          </div>

          <div className="rail__foot mono">
            {sessionId ?? '未选择会话'}
            <br />
            本机音频处理
          </div>
        </nav>

        <main className="main" id="main-content" tabIndex={-1}>
          {strip}
          <div className="main__scroll">
            <div className="mobile-brand">
              <div className="rail__mark">
                CleanCut<span>.</span>
              </div>
              <span className="chip mono">{sessionId ?? '未选择会话'}</span>
            </div>
            {children}
          </div>
        </main>
      </div>

      <nav className="tabbar" aria-label="主导航">
        {ROUTES.map((r) => (
          <button
            key={r.key}
            aria-current={route === r.key ? 'page' : undefined}
            onClick={() => onNavigate(r.key)}
          >
            <span className="tabbar__glyph">{r.glyph}</span>
            <span>{r.label}</span>
          </button>
        ))}
      </nav>
    </div>
  )
}
