/**
 * 把生成的文件交给用户保存，按环境选择方式：
 * - 演示页（claude.ai 预览）：用预览环境的下载接口；它只接受部分扩展名，其他文件打成 zip；
 * - 安卓 App：WebView 不处理下载链接，写入 App 缓存后打开系统分享面板（可存到“文件”或发给其他 App）；
 * - 普通浏览器：照常下载。
 * 用户在确认框里取消（declined）不算错误。
 */
import { Capacitor } from '@capacitor/core'
import { Directory, Filesystem } from '@capacitor/filesystem'
import { Share } from '@capacitor/share'
import { zipSingleFile } from './zip'

const PREVIEW_ALLOWED = new Set(['txt', 'json', 'md', 'csv', 'zip'])

type DownloadsApi = { save(request: { filename: string; data: Blob | Uint8Array }): Promise<unknown> }
type ClaudeHost = { use(name: string): Promise<unknown> }

async function previewDownloads(): Promise<DownloadsApi | null> {
  const host = (window as unknown as { claude?: ClaudeHost }).claude
  if (!host?.use) return null
  try { return (await host.use('downloads')) as DownloadsApi | null } catch { return null }
}

/** 浏览器原生下载（a[download] + blob 链接）。 */
export function browserDownload(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  document.body.appendChild(link)
  link.click()
  link.remove()
  window.setTimeout(() => URL.revokeObjectURL(url), 1000)
}

function blobToBase64(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result).replace(/^data:[^,]*,/, ''))
    reader.onerror = () => reject(reader.error ?? new Error('读取文件失败'))
    reader.readAsDataURL(blob)
  })
}

export async function saveFile(blob: Blob, filename: string): Promise<void> {
  const downloads = await previewDownloads()
  if (downloads) {
    const extension = filename.split('.').pop()?.toLowerCase() ?? ''
    const packed = PREVIEW_ALLOWED.has(extension)
      ? { filename, data: blob as Blob | Uint8Array }
      : { filename: `${filename}.zip`, data: zipSingleFile(filename, new Uint8Array(await blob.arrayBuffer())) }
    try {
      await downloads.save(packed)
    } catch (error) {
      const code = (error as { code?: string })?.code
      if (code === 'declined') return
      throw new Error(code === 'rate_limited' ? '已有一个保存确认框，请先处理它' : '这里无法保存文件')
    }
    return
  }
  if (Capacitor.isNativePlatform()) {
    const written = await Filesystem.writeFile({ path: filename, data: await blobToBase64(blob), directory: Directory.Cache })
    try {
      await Share.share({ title: filename, files: [written.uri] })
    } catch (error) {
      // 用户关闭分享面板时插件会报“取消”，不当成错误
      if (!/cancel/i.test(String((error as Error)?.message ?? error))) throw error
    }
    return
  }
  browserDownload(blob, filename)
}

/** 是否需要先取回文件再保存（演示页与 App 里普通下载链接不起作用）。 */
export function needsManagedDownload(): boolean {
  return Capacitor.isNativePlatform() || import.meta.env.VITE_DEMO === '1'
}
