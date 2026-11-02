import { defineConfig, type Plugin } from 'vite'
import react from '@vitejs/plugin-react'

/**
 * 把构建产物内联成单个 HTML 文件。
 * 用途：把设计稿交给不装 Node 的人（PM、QA、外部评审）直接双击打开。
 * 只在 build 时生效，dev 不受影响。不引入额外依赖。
 */
function singleFile(): Plugin {
  return {
    name: 'cleancut-single-file',
    enforce: 'post',
    apply: 'build',
    generateBundle(_options, bundle) {
      const html = bundle['index.html']
      if (!html || html.type !== 'asset') return
      let source = String(html.source)

      for (const [name, chunk] of Object.entries(bundle)) {
        if (name === 'index.html') continue
        if (chunk.type === 'chunk') {
          const inline = `<script type="module">\n${guard(chunk.code, 'script')}\n</script>`
          source = source.replace(
            new RegExp(`<script[^>]*src="[^"]*${escapeRe(name)}"[^>]*></script>`),
            // 必须用函数形式：压缩后的 React 代码里含 `$&`，
            // 用字符串替换会被当成"整个匹配"展开，把 script 标签注入进 JS，产物直接坏掉。
            () => inline,
          )
          delete bundle[name]
        } else if (name.endsWith('.css')) {
          const inline = `<style>\n${guard(String(chunk.source), 'style')}\n</style>`
          source = source.replace(
            new RegExp(`<link[^>]*href="[^"]*${escapeRe(name)}"[^>]*>`),
            () => inline,
          )
          delete bundle[name]
        }
      }
      html.source = source
    },
  }
}

function escapeRe(s: string) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

/** 内容里若出现闭合标签会提前终止内联块，转义掉。 */
function guard(code: string, tag: 'script' | 'style') {
  return code.split(`</${tag}`).join(`<\\/${tag}`)
}

export default defineConfig({
  base: './',
  plugins: [react(), singleFile()],
  build: {
    target: 'es2020',
    cssCodeSplit: false,
    assetsInlineLimit: 1024 * 1024,
    rollupOptions: { output: { inlineDynamicImports: true } },
  },
})
