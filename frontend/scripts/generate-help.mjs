#!/usr/bin/env node
/**
 * 帮助中心 manifest 生成器(FE-DOC-01)。
 *
 * 在构建阶段从仓库 manual/ 生成 `frontend/public/help/manifest.json`:
 * - 解析 manual/SUMMARY.md 得到可用语言列表;
 * - 解析各 SUMMARY.<locale>.md 得到目录树(使用者指南/开发者指南/更新日志三个一级节点 +
 *   各章节, 章节 id = 文件 basename, 跨语言保持稳定);
 * - 读取每个章节文件正文(Markdown)与最后修改时间;
 * - 生成物由 vite 拷贝进 dist, 不作为第二份手工维护源码提交
 *   (frontend/public/help/ 在 .gitignore 中排除)。
 *
 * 运行:npm run build 构建阶段(Docker 与本机一致); 环境变量 MANUAL_DIR、
 * PRODUCT_PYPROJECT 可分别覆盖 manual/ 与产品 pyproject.toml 的位置。
 */

import { readFileSync, statSync, writeFileSync, mkdirSync } from 'node:fs'
import { basename, dirname, join, resolve } from 'node:path'

const MANUAL_DIR = resolve(process.env.MANUAL_DIR || join(process.cwd(), '..', 'manual'))
const PRODUCT_PYPROJECT = resolve(
  process.env.PRODUCT_PYPROJECT || join(process.cwd(), '..', 'backend', 'pyproject.toml'),
)
const OUT_FILE = join(process.cwd(), 'public', 'help', 'manifest.json')

/** 从产品唯一版本源 backend/pyproject.toml 读取三段式版本号。 */
function readProductVersion() {
  const pyproject = readFileSync(PRODUCT_PYPROJECT, 'utf8')
  const match = /^version\s*=\s*"(\d+\.\d+\.\d+)"\s*$/m.exec(pyproject)
  if (!match) {
    throw new Error(`无法从 ${PRODUCT_PYPROJECT} 读取三段式产品版本号`)
  }
  return match[1]
}

/** 解析 "- [标题](相对路径)" 链接列表, 缩进(2 空格)表示层级。 */
function parseLinkTree(text) {
  const root = []
  const stack = [{ level: -1, children: root }]
  for (const raw of text.split('\n')) {
    const m = /^(\s*)- \[([^\]]+)\]\(([^)]+\.md)\)\s*$/.exec(raw)
    if (!m) continue
    const level = Math.floor(m[1].length / 2)
    const node = { title: m[2].trim(), path: m[3].trim(), children: [] }
    while (stack.length > 1 && stack[stack.length - 1].level >= level) stack.pop()
    stack[stack.length - 1].children.push(node)
    stack.push({ level, children: node.children })
  }
  return root
}

/** 语言入口 → locale 列表(manual/SUMMARY.md 顶层链接)。 */
function parseLocales(summaryText) {
  const locales = []
  for (const raw of summaryText.split('\n')) {
    const m = /^-\s*\[[^\]]*\]\((SUMMARY\.([^.]+)\.md)\)\s*$/.exec(raw)
    if (m) locales.push(m[2])
  }
  return locales
}

function main() {
  const summary = readFileSync(join(MANUAL_DIR, 'SUMMARY.md'), 'utf8')
  const locales = parseLocales(summary)
  if (!locales.length) {
    throw new Error(`manual/SUMMARY.md 未登记任何语言目录(SUMMARY.<locale>.md)`)
  }

  const trees = {}
  const pages = {}

  for (const locale of locales) {
    const treeText = readFileSync(join(MANUAL_DIR, `SUMMARY.${locale}.md`), 'utf8')
    const tree = parseLinkTree(treeText)
    trees[locale] = tree

    // 展平(含子节点): 章节 id = basename(去扩展名); README 用路径首段做指南 id
    // (跨语言保持稳定: zh-CN/en-US 同一指南/章节 id 一致)
    const walk = (nodes, guideId) => {
      for (const node of nodes) {
        const isReadme = node.path.endsWith('README.md')
        const id = isReadme ? node.path.split('/')[0] : basename(node.path, '.md')
        if (!id || id === 'README') throw new Error(`无法推导章节 id: ${node.path}`)
        const abs = join(MANUAL_DIR, node.path)
        const stat = statSync(abs)
        pages[`${locale}/${id}`] = {
          title: node.title,
          path: node.path,
          content: readFileSync(abs, 'utf8'),
          updatedAt: stat.mtime.toISOString(),
        }
        if (node.children.length) walk(node.children, id)
      }
    }
    walk(tree, '')
  }

  const manifest = {
    appVersion: readProductVersion(),
    generatedAt: new Date().toISOString(),
    locales,
    trees,
    pages,
  }
  mkdirSync(dirname(OUT_FILE), { recursive: true })
  writeFileSync(OUT_FILE, JSON.stringify(manifest, null, 2))
  console.log(
    `[help] manifest 生成: ${OUT_FILE} (${locales.join(', ')} 语言, ` +
      `${Object.keys(pages).length} 章节)`,
  )
}

main()
