/**
 * 帮助中心 Markdown 解析与安全(FE-DOC-01)。
 *
 * 纯函数模块(无 React 依赖), 供 HelpPage 渲染与 Node 单元测试共用:
 * - splitBlocks: Markdown 文本 → 块序列(标题/段落/列表/表格/引用/代码块);
 * - safeHref:    链接 URL 安全检查(只放行 http(s)/mailto/相对/锚点, 过滤 javascript:);
 * - pageIdFromPath / resolveLocale / interpolate: 章节 id 与语言解析。
 */

export type BlockType =
  | 'h1'
  | 'h2'
  | 'h3'
  | 'h4'
  | 'h5'
  | 'h6'
  | 'p'
  | 'ul'
  | 'ol'
  | 'table'
  | 'blockquote'
  | 'code'
  | 'hr'

export interface Block {
  type: BlockType
  raw: string
}

/** 链接 URL 安全检查: 只放行 http(s)/mailto/相对路径/锚点, 其余(含 javascript:)丢弃。 */
export function safeHref(href: string): string | null {
  const trimmed = href.trim()
  if (!trimmed) return null
  if (trimmed.startsWith('#') || trimmed.startsWith('/')) return trimmed
  if (/^(https?:|mailto:)/i.test(trimmed)) return trimmed
  return null
}

/** 章节 id: 指南 README → 指南名(user-guide); 其余 → basename(去 .md)。 */
export function pageIdFromPath(path: string): string {
  if (path.endsWith('README.md')) return path.split('/')[0]
  return path.replace(/\.md$/, '').split('/').pop() || ''
}

/** 浏览器语言 → manifest locale; 无对应语言返回 null(不静默冒充)。
 *
 * 匹配规则: 完全一致优先, 其次语言前缀匹配(界面 'zh' ↔ manifest 'zh-CN')。
 */
export function resolveLocale(available: string[], locale: string): string | null {
  if (available.includes(locale)) return locale
  const prefix = available.find((l) => l.startsWith(`${locale}-`))
  return prefix ?? null
}

/** 占位插值 {name}(与 i18n interpolate 同构)。 */
export function interpolate(template: string, params?: Record<string, unknown>): string {
  if (!params) return template
  return template.replace(/\{(\w+)\}/g, (m, name: string) =>
    params[name] === undefined ? m : String(params[name]),
  )
}

/** Markdown 文本 → 块序列。 */
export function splitBlocks(content: string): Block[] {
  const lines = content.split('\n')
  const blocks: Block[] = []
  let i = 0
  while (i < lines.length) {
    const line = lines[i]
    if (!line.trim()) {
      i++
      continue
    }
    // fenced code block
    if (/^```/.test(line)) {
      const buf = [line]
      let j = i + 1
      while (j < lines.length && !/^```\s*$/.test(lines[j])) {
        buf.push(lines[j])
        j++
      }
      if (j < lines.length) buf.push(lines[j]) // closing fence
      blocks.push({ type: 'code', raw: buf.join('\n') })
      i = j + 1
      continue
    }
    // 标题
    const mH = /^(#{1,6})\s+(.*)$/.exec(line)
    if (mH) {
      blocks.push({ type: `h${mH[1].length}` as BlockType, raw: mH[2] })
      i++
      continue
    }
    // 水平线
    if (/^---+$/.test(line) || /^\*\*\*+$/.test(line)) {
      blocks.push({ type: 'hr', raw: line })
      i++
      continue
    }
    // 列表(ul/ol)
    const mList = /^(\s*)([-*]|\d+[.)])\s+/.exec(line)
    if (mList) {
      const ordered = /\d/.test(mList[2])
      const buf: string[] = []
      const indent = mList[1].length
      let j = i
      while (j < lines.length) {
        const l = lines[j]
        if (!l.trim()) break
        const mm = /^(\s*)([-*]|\d+[.)])\s+(.*)$/.exec(l)
        if (mm && mm[1].length === indent) buf.push(mm[3])
        else if (mm && mm[1].length > indent) {
          // 子列表折叠到父项文本(渲染为段落延续)
          buf[buf.length - 1] += ` ${l.trim()}`
        } else break
        j++
      }
      blocks.push({ type: ordered ? 'ol' : 'ul', raw: buf.join('\n') })
      i = j
      continue
    }
    // 引用
    if (/^>\s?/.test(line)) {
      const buf: string[] = []
      let j = i
      while (j < lines.length && /^>\s?/.test(lines[j])) {
        buf.push(lines[j].replace(/^>\s?/, ''))
        j++
      }
      blocks.push({ type: 'blockquote', raw: buf.join('\n') })
      i = j
      continue
    }
    // 表格(表头行 + 分隔行 + 数据行)
    if (
      /\|/.test(line) &&
      i + 1 < lines.length &&
      /^\s*\|?[\s:|-]+\|?\s*$/.test(lines[i + 1]) &&
      lines[i + 1].includes('-')
    ) {
      const buf: string[] = []
      let j = i
      while (j < lines.length && /\|/.test(lines[j])) {
        buf.push(lines[j])
        j++
      }
      blocks.push({ type: 'table', raw: buf.join('\n') })
      i = j
      continue
    }
    // 段落(累积至空行/块级起点)
    const buf: string[] = []
    let j = i
    while (j < lines.length) {
      const l = lines[j]
      if (!l.trim()) break
      if (/^(#{1,6})\s|^```|^>|^\s*([-*]|\d+[.)])\s+|\|/.test(l)) break
      buf.push(l)
      j++
    }
    blocks.push({ type: 'p', raw: buf.join('\n') })
    i = j
  }
  return blocks
}
