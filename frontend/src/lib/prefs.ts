import { useCallback, useState } from 'react'

const PREFIX = 'cleancut.pref.'

/** 读取偏好；隐私模式或存储被禁用时静默回落到默认值。校验不通过（旧版本残留、被手改）同样回落。 */
export function readPref<T>(key: string, fallback: T, valid: (value: unknown) => value is T): T {
  try {
    const raw = localStorage.getItem(PREFIX + key)
    if (raw == null) return fallback
    const value: unknown = JSON.parse(raw)
    return valid(value) ? value : fallback
  } catch {
    return fallback
  }
}

export function writePref(key: string, value: unknown): void {
  try {
    localStorage.setItem(PREFIX + key, JSON.stringify(value))
  } catch {
    // 存不下也不影响本次使用。
  }
}

/** 像 useState，但值会记在本机浏览器里，下次打开默认带出（例如生成与导出选项）。 */
export function usePref<T>(key: string, fallback: T, valid: (value: unknown) => value is T) {
  const [value, setValue] = useState<T>(() => readPref(key, fallback, valid))
  const update = useCallback((next: T) => { setValue(next); writePref(key, next) }, [key])
  return [value, update] as const
}

export const isBoolean = (value: unknown): value is boolean => typeof value === 'boolean'
export const oneOf = <T,>(...options: readonly T[]) => (value: unknown): value is T => options.includes(value as T)
