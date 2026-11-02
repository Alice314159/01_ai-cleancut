import type { ReactNode } from 'react'

export function Card({
  id,
  title,
  action,
  hint,
  children,
}: {
  id?: string
  title?: string
  action?: ReactNode
  hint?: string
  children: ReactNode
}) {
  return (
    <section id={id} tabIndex={id ? -1 : undefined} className="card">
      {(title || action) && (
        <div className="card__title">
          {title && <h2>{title}</h2>}
          {action}
        </div>
      )}
      {hint && <p className="card__hint">{hint}</p>}
      {children}
    </section>
  )
}

export function KV({ k, v, tone }: { k: ReactNode; v: ReactNode; tone?: 'ok' | 'warn' | 'bad' }) {
  return (
    <div className="kv">
      <span className="kv__k">{k}</span>
      <span className={`kv__v${tone ? ` kv__v--${tone}` : ''}`}>{v}</span>
    </div>
  )
}

export function Stat({ k, v, note, tone }: { k: string; v: ReactNode; note?: string; tone?: 'ok' | 'warn' | 'bad' }) {
  return (
    <div className={`stat${tone ? ` stat--${tone}` : ''}`}>
      <div className="stat__k">{k}</div>
      <div className={`stat__v${tone ? ` stat__v--${tone}` : ''}`}>{v}</div>
      {note && <div className="stat__note">{note}</div>}
    </div>
  )
}

export function Chip({
  tone,
  children,
}: {
  tone?: 'committed' | 'provisional' | 'protect' | 'danger'
  children: ReactNode
}) {
  return <span className={`chip${tone ? ` chip--${tone}` : ''}`}>{children}</span>
}
