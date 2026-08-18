/**
 * 独立教程页(REQ-HELP-002)。
 *
 * 设计约定:
 * - 纯静态页面:不 import src/api/client.ts,不调用任何后端接口,不接收项目数据,
 *   不承担业务计算或权限判断;计算服务不可用时仍可阅读。
 * - 内容完全由 src/data/tutorial.ts 的静态双语数据驱动,渲染与数据分离。
 * - 布局:顶部程序版本号 + 语言切换;左侧可滚动目录导航(滚动监听高亮当前章节);
 *   右侧正文内容。样式复用设计系统令牌与类(见 styles.css 教程章节)。
 * - 无障碍:导航为真实锚点链接、焦点可见、aria-current 标识当前章节,
 *   平滑滚动遵循 prefers-reduced-motion。
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'

import { useI18n } from '../i18n'
import { Alert, Button, Icon } from '../components/ui'
import {
  APP_VERSION,
  TUTORIAL_META,
  TUTORIAL_SECTIONS,
  type Bilingual,
  type TutorialBlock,
  type TutorialSection,
} from '../data/tutorial'

/** 双语文本按当前语言渲染。 */
function LText({ value, className }: { value: Bilingual; className?: string }) {
  const { locale } = useI18n()
  return <span className={className}>{value[locale]}</span>
}

/** 单个内容块渲染(数据驱动,无逻辑)。 */
function renderBlock(block: TutorialBlock, key: number): ReactNode {
  switch (block.type) {
    case 'p':
      return (
        <p key={key} className="ies-tutorial__text">
          <LText value={block.text} />
        </p>
      )
    case 'h3':
      return (
        <h3 key={key} className="ies-tutorial__section-subtitle">
          <LText value={block.text} />
        </h3>
      )
    case 'ul':
    case 'ol':
      return (
        <ListBlock key={key} ordered={block.type === 'ol'} items={block.items} />
      )
    case 'table':
      return (
        <div key={key} className="ies-table-wrap">
          <table className="ies-table">
            <thead className="ies-table__head">
              <tr>
                {block.headers.map((h, i) => (
                  <th key={i} scope="col">
                    <LText value={h} />
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="ies-table__body">
              {block.rows.map((row, i) => (
                <tr key={i}>
                  {row.map((cell, j) => (
                    <td key={j}>
                      <LText value={cell} />
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )
    case 'note':
      return (
        <Alert
          key={key}
          variant={block.variant}
          title={block.title ? <LText value={block.title} /> : undefined}
        >
          <LText value={block.text} />
        </Alert>
      )
    case 'code':
      return (
        <pre key={key} className="ies-tutorial__code">
          <code>
            <LText value={block.text} />
          </code>
        </pre>
      )
    case 'steps':
      return (
        <ol key={key} className="ies-tutorial__steps">
          {block.items.map((item, i) => (
            <li key={i} className="ies-tutorial__step">
              <span className="ies-tutorial__step-num" aria-hidden="true">
                {i + 1}
              </span>
              <div className="ies-tutorial__step-body">
                <span className="ies-tutorial__step-title">
                  <LText value={item.title} />
                </span>
                <span className="ies-tutorial__step-desc">
                  <LText value={item.desc} />
                </span>
              </div>
            </li>
          ))}
        </ol>
      )
  }
}

function ListBlock({ ordered, items, key }: { ordered: boolean; items: Bilingual[]; key: number }) {
  const Tag = ordered ? 'ol' : 'ul'
  return (
    <Tag key={key} className="ies-tutorial__list">
      {items.map((item, i) => (
        <li key={i}>
          <LText value={item} />
        </li>
      ))}
    </Tag>
  )
}

/** 章节渲染:h2 标题 + 内容块序列。 */
function SectionView({ section }: { section: TutorialSection }) {
  const { locale } = useI18n()
  return (
    <section
      id={section.id}
      className="ies-tutorial__section"
      aria-labelledby={`${section.id}-title`}
    >
      <h2 id={`${section.id}-title`} className="ies-tutorial__section-title">
        {section.title[locale]}
      </h2>
      {section.blocks.map((block, i) => renderBlock(block, i))}
    </section>
  )
}

/** 独立教程页(默认导出,App.tsx 惰性加载)。 */
export default function TutorialPage() {
  const { t, locale, setLocale } = useI18n()
  const meta = TUTORIAL_META
  const [activeId, setActiveId] = useState<string>(TUTORIAL_SECTIONS[0].id)
  const sectionEls = useRef<Map<string, HTMLElement> | null>(null)

  /** 滚动监听:当前章节 = 顶部越过阈值线(120px)的最后一个章节。 */
  const computeActive = useCallback(() => {
    const els = sectionEls.current
    if (!els) return
    const threshold = 120
    let current = TUTORIAL_SECTIONS[0].id
    for (const section of TUTORIAL_SECTIONS) {
      const el = els.get(section.id)
      if (el && el.getBoundingClientRect().top <= threshold) current = section.id
    }
    setActiveId((prev) => (prev === current ? prev : current))
  }, [])

  useEffect(() => {
    const map = new Map<string, HTMLElement>()
    for (const section of TUTORIAL_SECTIONS) {
      const el = document.getElementById(section.id)
      if (el) map.set(section.id, el)
    }
    sectionEls.current = map
    computeActive()

    let raf = 0
    const onScroll = () => {
      if (raf) return
      raf = requestAnimationFrame(() => {
        raf = 0
        computeActive()
      })
    }
    window.addEventListener('scroll', onScroll, { passive: true })
    window.addEventListener('resize', onScroll, { passive: true })
    return () => {
      window.removeEventListener('scroll', onScroll)
      window.removeEventListener('resize', onScroll)
      if (raf) cancelAnimationFrame(raf)
    }
  }, [computeActive])

  /** 目录跳转:平滑滚动(遵循 prefers-reduced-motion)。 */
  const scrollToSection = (id: string) => {
    const el = document.getElementById(id)
    if (!el) return
    const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    el.scrollIntoView({ behavior: reduce ? 'auto' : 'smooth', block: 'start' })
  }

  return (
    <div className="ies-app ies-tutorial">
      <header className="ies-topbar">
        <Link to="/" className="ies-topbar__brand" aria-label={t('ies.nav.app_title')}>
          <span className="ies-topbar__brand-mark" aria-hidden="true">
            IES
          </span>
          <span>{t('ies.nav.app_title')}</span>
        </Link>
        <nav className="ies-topbar__nav" aria-label={t('ies.nav.tutorial')}>
          <Link to="/tutorial" className="ies-topbar__link" aria-current="page">
            {t('ies.nav.tutorial')}
          </Link>
        </nav>
        <div className="ies-topbar__spacer" />
        <div className="ies-topbar__actions">
          <span className="ies-tutorial__version" title={meta.versionLabel[locale]}>
            <Icon name="info" size={13} />
            <span>{meta.versionLabel[locale]}</span>
            <span className="ies-tutorial__version-value">{APP_VERSION}</span>
          </span>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setLocale(locale === 'zh' ? 'en' : 'zh')}
            aria-label={t('ies.nav.language')}
          >
            {locale === 'zh' ? 'EN' : '中文'}
          </Button>
          <Link to="/" className="ies-btn ies-btn--secondary ies-btn--sm">
            {meta.backToApp[locale]}
          </Link>
        </div>
      </header>

      <div className="ies-tutorial__body">
        <nav className="ies-tutorial__nav" aria-label={meta.tocTitle[locale]}>
          <div className="ies-tutorial__nav-title">{meta.tocTitle[locale]}</div>
          <ul className="ies-tutorial__nav-list">
            {TUTORIAL_SECTIONS.map((section) => (
              <li key={section.id}>
                <a
                  href={`#${section.id}`}
                  className="ies-tutorial__nav-link"
                  aria-current={activeId === section.id ? 'true' : undefined}
                  onClick={(event) => {
                    event.preventDefault()
                    scrollToSection(section.id)
                  }}
                >
                  {section.title[locale]}
                </a>
              </li>
            ))}
          </ul>
        </nav>

        <main className="ies-tutorial__content">
          <article className="ies-tutorial__article">
            <Alert variant="info" title={meta.offlineTitle[locale]}>
              {meta.offlineNote[locale]}
            </Alert>
            {TUTORIAL_SECTIONS.map((section) => (
              <SectionView key={section.id} section={section} />
            ))}
            <footer className="ies-tutorial__footer">{meta.footer[locale]}</footer>
          </article>
        </main>
      </div>
    </div>
  )
}
