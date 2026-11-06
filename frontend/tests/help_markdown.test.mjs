/**
 * 帮助中心单元测试(FE-DOC-01 验收: Markdown 表格/代码块/相对链接/标题锚点/
 * 非法 HTML 与不安全 URL 有渲染/安全测试)。
 *
 * 纯 Node 环境(无浏览器/无 React): 用 esbuild 把 lib/helpMarkdown.ts 打包为 CJS
 * 后直接调用 splitBlocks / safeHref / pageIdFromPath / resolveLocale。
 *
 * 运行(在 frontend 目录):
 *   npx esbuild src/lib/helpMarkdown.ts --bundle --format=cjs --outfile=/tmp/help-md.cjs
 *   node tests/help_markdown.test.mjs
 */

import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'

const require = createRequire(import.meta.url)

let failures = 0
function check(name, cond, detail = '') {
  const ok = cond ? 'PASS' : 'FAIL'
  if (!cond) failures++
  console.log(`[${ok}] ${name}${detail ? ` — ${detail}` : ''}`)
}

const md = require('/tmp/help-md.cjs')

// ---- splitBlocks: 标题/段落/列表/表格/引用/代码块 ----
const md1 = `# 标题一

段落文本。

- 项 A
- 项 B

1. 步骤一
2. 步骤二

> 引用内容

| 列1 | 列2 |
|-----|-----|
| a   | b   |

\`\`\`bash
echo hello
\`\`\`
`
const blocks = md.splitBlocks(md1)
const types = blocks.map((b) => b.type)
check('h1 解析', types[0] === 'h1' && blocks[0].raw === '标题一', JSON.stringify(types))
check('段落解析', types[1] === 'p' && blocks[1].raw === '段落文本。')
check('无序列表解析', types[2] === 'ul' && blocks[2].raw.includes('项 A'))
check('有序列表解析', types[3] === 'ol' && blocks[3].raw.includes('步骤一'))
check('引用解析', types[4] === 'blockquote' && blocks[4].raw === '引用内容')
check('表格解析', types[5] === 'table', `raw=${blocks[5]?.raw.slice(0, 20)}`)
check('代码块解析', types[6] === 'code' && blocks[6].raw.includes('echo hello'))

// ---- 表格分隔行过滤(渲染侧从 raw 提取数据行) ----
check('表格 raw 含分隔行', blocks[5].raw.includes('|-----|'))

// ---- 普通管道符不截断段落；真正的表格/水平线仍可紧接正文 ----
const pipeText = '正文包含 `purchase|sale` 与 mode: mandatory|optional。\n第二行继续使用 x|y。'
const pipeBlocks = md.splitBlocks(pipeText)
check('正文管道符保留为单段且完整消费', pipeBlocks.length === 1 &&
  pipeBlocks[0].type === 'p' && pipeBlocks[0].raw === pipeText)
const notTable = '普通段落\n说明 | 不是表头\n这里不是分隔行'
check('缺少表格分隔行时保持段落', md.splitBlocks(notTable)[0].raw === notTable)
const adjacentTable = md.splitBlocks('表格说明\n| 名称 | 值 |\n| --- | --- |\n| a | b |')
check('紧接段落的真实表格仍被识别', adjacentTable.length === 2 &&
  adjacentTable[0].raw === '表格说明' && adjacentTable[1].type === 'table' &&
  adjacentTable[1].raw.includes('| a | b |'))
const adjacentRule = md.splitBlocks('段落\n---\n下一段\n***')
check('紧接段落的水平线仍被识别', adjacentRule.map((b) => b.type).join(',') === 'p,hr,p,hr')

// ---- safeHref: 过滤 javascript: 等不安全 URL ----
check('https 放行', md.safeHref('https://example.com') === 'https://example.com')
check('相对路径放行', md.safeHref('/help/zh-CN/getting-started') === '/help/zh-CN/getting-started')
check('锚点放行', md.safeHref('#intro') === '#intro')
check('mailto 放行', md.safeHref('mailto:x@y.z') === 'mailto:x@y.z')
check('javascript: 拒绝', md.safeHref('javascript:alert(1)') === null)
check('空格 javascript: 拒绝', md.safeHref('javascript:alert(1) ') === null)
check('空 href 拒绝', md.safeHref('') === null)
check('data: 拒绝', md.safeHref('data:text/html;base64,PHNjcmlwdD4=') === null)
check('vbscript: 拒绝', md.safeHref('vbscript:msgbox(1)') === null)

// ---- pageIdFromPath: 章节 id 稳定(README → 指南名) ----
check('README → 指南 id', md.pageIdFromPath('user-guide/zh-CN/README.md') === 'user-guide')
check('章节 → basename', md.pageIdFromPath('developer-guide/zh-CN/contracts.md') === 'contracts')
check('en 同章节 id 一致', md.pageIdFromPath('developer-guide/en-US/contracts.md') === 'contracts')

// ---- resolveLocale: 缺失语言明确提示(不静默冒充) ----
check('zh-CN 可用', md.resolveLocale(['zh-CN'], 'zh-CN') === 'zh-CN')
check('zh 前缀匹配 zh-CN', md.resolveLocale(['zh-CN'], 'zh') === 'zh-CN')
check('en 缺失返回 null', md.resolveLocale(['zh-CN'], 'en') === null)

// ---- manifest 结构(构建期生成物) ----
try {
  const manifest = JSON.parse(readFileSync('public/help/manifest.json', 'utf8'))
  check('manifest 语言登记', Array.isArray(manifest.locales) && manifest.locales.includes('zh-CN'))
  check('manifest 产品版本为三段式', /^\d+\.\d+\.\d+$/.test(manifest.appVersion))
  check('manifest 三个一级文档入口', (() => {
    const t = manifest.trees['zh-CN']
    return (
      t.length === 3 &&
      t[0].title === '使用者指南' &&
      t[1].title === '开发者指南' &&
      t[2].title === '更新日志'
    )
  })())
  check('manifest 章节正文非空', (() => {
    const ids = Object.keys(manifest.pages).filter((k) => k.startsWith('zh-CN/'))
    return ids.length >= 36 && ids.every((k) => manifest.pages[k].content.length > 0)
  })())
  const pages = Object.values(manifest.pages)
  check('全部已发布帮助正文均可解析且不生成空段落', pages.every((page) => {
    const parsed = md.splitBlocks(page.content)
    return parsed.length > 0 && parsed.every((block) => block.type !== 'p' || block.raw.trim())
  }), `${pages.length} 篇`)
  check('manifest 深链接 id 集合 = 目录展平', (() => {
    const flat = []
    const walk = (nodes) => {
      for (const n of nodes) {
        flat.push(md.pageIdFromPath(n.path))
        if (n.children.length) walk(n.children)
      }
    }
    walk(manifest.trees['zh-CN'])
    return flat.every((id) => manifest.pages[`zh-CN/${id}`])
  })())
} catch (err) {
  failures++
  console.log(`[FAIL] manifest 生成物缺失或损坏: ${err.message}`)
}

console.log()
if (failures > 0) {
  console.log(`共 ${failures} 项失败`)
  process.exit(1)
}
console.log('帮助中心单元测试全部通过')
