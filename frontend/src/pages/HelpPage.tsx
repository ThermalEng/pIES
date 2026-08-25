/**
 * 帮助中心(FE-DOC-01: manual/ 唯一正文来源)。
 *
 * - 静态可读: 不依赖登录/项目数据/后端; 内容来自构建期生成的
 *   /help/manifest.json(仓库 manual/ 编译产物);
 * - 路由 /help/*: /help 与 /help/:lang/:pageId(深链接可直接打开并刷新恢复);
 * - 桌面端树形侧栏(使用者指南/开发者指南/更新日志三个一级节点), 移动端可展开目录;
 * - Markdown 渲染: 标题/段落/列表/表格/引用/链接/行内代码/fenced code block;
 *   默认禁用原始 HTML, 过滤 javascript: 等不安全 URL, 外部链接标识;
 *   内部相对链接转客户端路由, 不触发整页刷新;
 * - 标题锚点、前后页、当前节点高亮(aria-current)、键盘焦点完整;
 * - 当前语言无对应章节时明确提示可用语言, 不把中文静默冒充英文版本。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { ReactNode, RefObject } from 'react'
import { Link, NavLink, useNavigate, useParams } from 'react-router-dom'

import { useI18n } from '../i18n'
import { pt } from '../i18n/pageMessages'
import { Icon } from '../components/ui'

/** 构建期生成的帮助中心 manifest(public/help/manifest.json)。 */
export interface HelpManifest {
  appVersion: string
  generatedAt: string
  locales: string[]
  trees: Record<string, HelpNode[]>
  pages: Record<string, HelpPage>
}

export interface HelpNode {
  title: string
  path: string
  children: HelpNode[]
}

export interface HelpPage {
  title: string
  path: string
  content: string
  updatedAt: string
}

/** 章节 id / 语言解析 / 占位插值 / Markdown 解析与安全: 见 lib/helpMarkdown.ts。 */
import {
  interpolate,
  pageIdFromPath,
  resolveLocale,
  safeHref,
  splitBlocks,
} from '../lib/helpMarkdown'
import type { Block } from '../lib/helpMarkdown'

/** 事件委托: 捕获节点内 <a>/<button> 点击, 阻止应用外部导航。 */
function useCaptureClicks(
  ref: RefObject<HTMLElement | null>,
  onLink: (href: string) => void,
) {
  useEffect(() => {
    const el = ref.current
    if (!el) return
    const handler = (ev: MouseEvent) => {
      const target = (ev.target as HTMLElement).closest('a, button')
      if (!target) return
      if (target.tagName === 'A') {
        const href = (target as HTMLAnchorElement).getAttribute('href') || ''
        if (href.startsWith('#') || href.startsWith('/help')) {
          // 客户端内跳转
          ev.preventDefault()
          if (href.startsWith('/help')) onLink(href)
        }
      }
    }
    el.addEventListener('click', handler)
    return () => el.removeEventListener('click', handler)
  }, [ref, onLink])
}

/** 递归渲染目录树节点。 */
function TreeNode({
  node,
  locale,
  current,
  onNavigate,
}: {
  node: HelpNode
  locale: string
  current: string
  onNavigate: () => void
}) {
  const id = pageIdFromPath(node.path)
  const isActive = id === current
  return (
    <li className="ies-help__nav-item">
      <NavLink
        to={`/help/${locale}/${id}`}
        className="ies-help__nav-link"
        aria-current={isActive ? 'page' : undefined}
        onClick={onNavigate}
      >
        {node.title}
      </NavLink>
      {node.children.length > 0 && (
        <ul className="ies-help__nav-list">
          {node.children.map((child, i) => (
            <TreeNode key={i} node={child} locale={locale} current={current} onNavigate={onNavigate} />
          ))}
        </ul>
      )}
    </li>
  )
}

/** 单行内联标记(代码/强调/链接)。locale 用于内部 .md 链接 → 章节路由。 */
function renderInline(text: string, key: number, locale: string): ReactNode {
  // 行内代码(最优先, 内容原样)
  const codeRe = /`([^`]+)`/
  const mCode = codeRe.exec(text)
  if (mCode) {
    return (
      <span key={key}>
        {renderInline(text.slice(0, mCode.index), key * 10 + 1, locale)}
        <code className="ies-help__code-inline">{mCode[1]}</code>
        {renderInline(text.slice(mCode.index + mCode[0].length), key * 10 + 2, locale)}
      </span>
    )
  }
  // 链接 [text](href)
  const mLink = /\[([^\]]+)\]\(([^)\s]+)\)/.exec(text)
  if (mLink) {
    const rawHref = mLink[2]
    // FE-DOC-01 §12.3: 内部相对 .md 链接(如 README 的
    // "[快速开始](zh-CN/getting-started.md)")解析为客户端章节路由;
    // 其余链接经 safeHref 安全校验(只放行 http(s)/mailto/绝对/锚点)。
    let href: string | null = null
    if (/\.md(#.*)?$/.test(rawHref) && !/^(https?:|mailto:)/i.test(rawHref)) {
      const id = rawHref.replace(/\.md(#.*)?$/, '').split('/').pop() || ''
      href = id ? `/help/${locale}/${id}` : null
    } else {
      href = safeHref(rawHref)
    }
    return (
      <span key={key}>
        {renderInline(text.slice(0, mLink.index), key * 10 + 1, locale)}
        {href ? (
          <a
            href={href}
            className="ies-help__link"
            rel={/^https?:/i.test(href) ? 'noopener noreferrer' : undefined}
            target={/^https?:/i.test(href) ? '_blank' : undefined}
          >
            {mLink[1]}
          </a>
        ) : (
          <span className="ies-help__link-invalid">{mLink[1]}</span>
        )}
        {renderInline(text.slice(mLink.index + mLink[0].length), key * 10 + 2, locale)}
      </span>
    )
  }
  // 粗体 **text** / 强调 *text*
  const mStrong = /\*\*([^*]+)\*\*/.exec(text)
  if (mStrong) {
    return (
      <span key={key}>
        {renderInline(text.slice(0, mStrong.index), key * 10 + 1, locale)}
        <strong>{mStrong[1]}</strong>
        {renderInline(text.slice(mStrong.index + mStrong[0].length), key * 10 + 2, locale)}
      </span>
    )
  }
  return <span key={key}>{text}</span>
}

/** Markdown 块渲染器(标题/段落/列表/表格/引用/代码块)。
 *
 * locale 用于把相对 .md 内部链接解析为 /help/{locale}/{pageId} 客户端路由
 * (FE-DOC-01 §12.3); useCaptureClicks 捕获后经 onLink 导航, 不整页刷新。
 */
function MarkdownContent({ content, locale }: { content: string; locale: string }) {
  const blocks = useMemo(() => splitBlocks(content), [content])
  return (
    <div className="ies-help__markdown">
      {blocks.map((block, i) => (
        <MarkdownBlock key={i} block={block} locale={locale} />
      ))}
    </div>
  )
}

/** 渲染单个块。 */
function MarkdownBlock({ block, locale }: { block: Block; locale: string }) {
  switch (block.type) {
    case 'h1':
      return <h1 className="ies-help__h1">{renderInline(block.raw, 0, locale)}</h1>
    case 'h2':
      return <h2 className="ies-help__h2">{renderInline(block.raw, 0, locale)}</h2>
    case 'h3':
      return <h3 className="ies-help__h3">{renderInline(block.raw, 0, locale)}</h3>
    case 'h4':
      return <h4 className="ies-help__h4">{renderInline(block.raw, 0, locale)}</h4>
    case 'h5':
    case 'h6':
      return <h5 className="ies-help__h5">{renderInline(block.raw, 0, locale)}</h5>
    case 'p':
      return <p className="ies-help__p">{renderInline(block.raw, 0, locale)}</p>
    case 'ul':
      return (
        <ul className="ies-help__ul">
          {block.raw.split('\n').map((item, i) => (
            <li key={i}>{renderInline(item, i, locale)}</li>
          ))}
        </ul>
      )
    case 'ol':
      return (
        <ol className="ies-help__ol">
          {block.raw.split('\n').map((item, i) => (
            <li key={i}>{renderInline(item, i, locale)}</li>
          ))}
        </ol>
      )
    case 'blockquote':
      return (
        <blockquote className="ies-help__blockquote">{renderInline(block.raw, 0, locale)}</blockquote>
      )
    case 'table': {
      const rows = block.raw
        .split('\n')
        .filter((l) => l.trim() && !/^\s*\|?[\s:|-]+\|?\s*$/.test(l))
        .map((l) => l.replace(/^\s*\|/, '').replace(/\|\s*$/, '').split('|').map((c) => c.trim()))
      if (rows.length < 2) return null
      const [head, ...body] = rows
      return (
        <div className="ies-help__table-wrap">
          <table className="ies-help__table">
            <thead>
              <tr>
                {head.map((h, i) => (
                  <th key={i} scope="col">
                    {renderInline(h, i, locale)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {body.map((row, i) => (
                <tr key={i}>
                  {row.map((cell, j) => (
                    <td key={j}>{renderInline(cell, j, locale)}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )
    }
    case 'code': {
      const [, ...body] = block.raw.split('\n')
      // 去除末尾 closing fence(```)行; raw 以 ``` 开头并以 ``` 结束
      const lines = body.filter((l, idx) => idx < body.length - 1 || !/^\s*```\s*$/.test(l))
      return (
        <pre className="ies-help__code">
          <code>{lines.join('\n')}</code>
        </pre>
      )
    }
    case 'hr':
      return <hr className="ies-help__hr" />
  }
}

/** 帮助中心页(默认导出, App.tsx 惰性加载)。 */
export default function HelpPage() {
  const { t, locale } = useI18n()
  const { lang, pageId } = useParams<{ lang?: string; pageId?: string }>()
  const [manifest, setManifest] = useState<HelpManifest | null>(null)
  const [navOpen, setNavOpen] = useState(false)
  const contentRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    let alive = true
    fetch('/help/manifest.json', { cache: 'no-cache' })
      .then((res) => {
        if (!res.ok) throw new Error(`manifest 加载失败: ${res.status}`)
        return res.json() as Promise<HelpManifest>
      })
      .then((m) => {
        if (alive) setManifest(m)
      })
      .catch((err) => {
        if (alive) setManifest(null)
        console.error('[help] manifest 加载失败:', err)
      })
    return () => {
      alive = false
    }
  }, [])

  // 语言解析: URL 显式 locale → 界面语言 → 可用语言优先
  const currentLocale = useMemo(() => {
    if (!manifest) return null
    if (lang && manifest.locales.includes(lang)) return lang
    return resolveLocale(manifest.locales, locale)
  }, [manifest, lang, locale])

  // 当前章节 id: URL pageId → 语言内存在 → 指南 README → 首个章节
  const tree = manifest && currentLocale ? manifest.trees[currentLocale] : null
  const flatPages = useMemo(() => {
    const out: string[] = []
    const walk = (nodes: HelpNode[]) => {
      for (const n of nodes) {
        out.push(pageIdFromPath(n.path))
        if (n.children.length) walk(n.children)
      }
    }
    if (tree) walk(tree)
    return out
  }, [tree])

  const currentId = useMemo(() => {
    if (!flatPages.length) return null
    if (pageId && flatPages.includes(pageId)) return pageId
    return flatPages[0]
  }, [pageId, flatPages])

  const currentPage =
    manifest && currentLocale && currentId ? manifest.pages[`${currentLocale}/${currentId}`] : null

  // 章节定位(当前目录树中位置)
  const position = useMemo(() => {
    const idx = flatPages.indexOf(currentId || '')
    return { prev: idx > 0 ? flatPages[idx - 1] : null, next: idx >= 0 && idx < flatPages.length - 1 ? flatPages[idx + 1] : null }
  }, [flatPages, currentId])

  // 移动端目录打开时点击章节关闭
  const closeNav = useCallback(() => setNavOpen(false), [])
  const navigate = useNavigate()
  // 内容区点击捕获: 内部锚点/章节路由链接走客户端导航(不整页刷新)
  useCaptureClicks(contentRef, navigate)

  if (!manifest) {
    return (
      <div className="ies-help">
        <main className="ies-help__main">
          <div className="ies-page-placeholder" role="status">
            <Icon name="info" size={24} />
            <p>{t('ies.common.loading')}</p>
          </div>
        </main>
      </div>
    )
  }

  const navTitle = pt('ies.help.nav_title')

  return (
    <div className="ies-help">

      <div className="ies-help__body">
        {/* 移动端目录开关 */}
        <button
          type="button"
          className="ies-help__nav-toggle"
          aria-expanded={navOpen}
          aria-controls="help-nav"
          onClick={() => setNavOpen((v) => !v)}
        >
          <Icon name="question" size={14} />
          <span>{navTitle}</span>
        </button>

        <nav
          id="help-nav"
          className={`ies-help__nav${navOpen ? ' ies-help__nav--open' : ''}`}
          aria-label={navTitle}
        >
          {tree?.map((node, i) => (
            <ul key={i} className="ies-help__nav-list ies-help__nav-list--root">
              <TreeNode node={node} locale={currentLocale!} current={currentId || ''} onNavigate={closeNav} />
            </ul>
          ))}
          {!tree && (
            <p className="ies-help__missing">
              {interpolate(pt('ies.help.locale_missing'), { locales: manifest.locales.join(', ') })}
            </p>
          )}
        </nav>

        <main className="ies-help__content" ref={contentRef}>
          {currentPage ? (
            <article className="ies-help__article">
              <h1 className="ies-help__title">{currentPage.title}</h1>
              <MarkdownContent content={currentPage.content} locale={currentLocale!} />
              <nav className="ies-help__pager" aria-label={pt('ies.help.pager')}>
                {position.prev && (
                  <Link to={`/help/${currentLocale}/${position.prev}`} className="ies-btn ies-btn--ghost ies-btn--sm">
                    ← {pt('ies.help.prev')}
                  </Link>
                )}
                <span className="ies-help__meta">
                  {interpolate(pt('ies.help.updated'), {
                    date: new Date(currentPage.updatedAt).toLocaleDateString(locale === 'en' ? 'en-US' : 'zh-CN'),
                  })}
                </span>
                {position.next && (
                  <Link to={`/help/${currentLocale}/${position.next}`} className="ies-btn ies-btn--ghost ies-btn--sm">
                    {pt('ies.help.next')} →
                  </Link>
                )}
              </nav>
            </article>
          ) : (
            <div className="ies-page-placeholder">
              <h1 className="ies-page-title">{pt('ies.help.not_found')}</h1>
              <p className="ies-page-subtitle">
                {interpolate(pt('ies.help.locale_missing'), { locales: manifest.locales.join(', ') })}
              </p>
            </div>
          )}
        </main>
      </div>
    </div>
  )
}
