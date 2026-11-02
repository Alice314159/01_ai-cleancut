import { useRef } from 'react'

/**
 * 导入已有音频/视频文件，作为"录后处理"会话的起点——跳过实时录制，
 * 直接把文件交给后端做和录制终态化完全一样的解码 + 质量分析。
 *
 * 用真正的 <button> 触发一个完全隐藏的 <input type=file>（ref.click()），
 * 而不是用 <label> 包一个 hidden input——后者会让文件选择控件同时从 tab 顺序
 * 和无障碍树里消失，键盘和屏幕阅读器用户将完全无法触发导入。
 */
export function ImportAudioButton({
  onImport,
  busy,
}: {
  onImport: (file: File) => void
  busy: boolean
}) {
  const inputRef = useRef<HTMLInputElement>(null)
  return (
    <>
      <button type="button" className="btn" disabled={busy} onClick={() => inputRef.current?.click()}>
        {busy ? '导入中…' : '导入音频'}
      </button>
      <input
        ref={inputRef}
        type="file"
        accept="audio/*,video/*"
        hidden
        onChange={(event) => {
          const file = event.target.files?.[0]
          event.target.value = ''
          if (file) onImport(file)
        }}
      />
    </>
  )
}
