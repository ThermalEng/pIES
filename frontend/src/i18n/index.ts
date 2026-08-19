/**
 * 国际化:双语消息表 + I18nProvider + useI18n()。
 *
 * - 键结构统一为 "ies.<域>.<...>",默认语言 zh。
 * - t(key, params) 负责 {name} 占位插值;缺失键回退返回键本身(开发期 console.warn)。
 * - 诊断/错误渲染:translateDiagnostic / translateError 直接消费后端
 *   Diagnostic 与 ApiError 的 message_key + params + fix_hint_key(契约 P3:
 *   后端只输出消息键与参数,文案仅存在于前端 locale 资源)。
 */

import { createContext, createElement, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'

import type { ApiError, Diagnostic } from '../types'
import { messagesEn } from './messages_en'
import { messagesZh } from './messages_zh'

export type Locale = 'zh' | 'en'

export type MessageKey = string

/** 语言偏好存储键。 */
const LOCALE_STORAGE_KEY = 'iesplan.locale'

/** 消息表注册表:语言 -> 键表。 */
const MESSAGES: Record<Locale, Record<string, string>> = {
  zh: messagesZh,
  en: messagesEn,
}

/** 当前语言(模块级,供非 React 环境如 lib/format 使用)。 */
let currentLocale: Locale = 'zh'

function resolveLocale(preferred: string | null): Locale {
  return preferred === 'en' ? 'en' : 'zh'
}

/** 读取持久化的语言偏好(缺省 zh)。 */
export function getLocale(): Locale {
  return currentLocale
}

/** 渲染插值参数:文案内 {name} 占位符替换为 params 中的值。 */
function interpolate(template: string, params?: Record<string, unknown> | null): string {
  if (!params) return template
  return template.replace(/\{(\w+)\}/g, (match, name: string) => {
    const value = params[name]
    return value === undefined || value === null ? match : String(value)
  })
}

/**
 * 模块级翻译函数(可在 hook 之外使用,如 lib/format)。
 * @param key      消息键(如 ies.diag.data.ts_dup)
 * @param params   插值参数(与后端 params 字段一致)
 * @param locale   指定语言(缺省用当前语言)
 */
export function translate(
  key: MessageKey,
  params?: Record<string, unknown> | null,
  locale?: Locale,
): string {
  const lang = locale ?? currentLocale
  const table = MESSAGES[lang]
  const template = table[key]
  if (template === undefined) {
    // 缺失键:开发期提示,运行期回退键名(便于排查后端新增键)。
    if (import.meta.env?.DEV) {
      console.warn(`[i18n] 缺失消息键: ${key} (${lang})`)
    }
    return key
  }
  return interpolate(template, params)
}

/** 将后端诊断对象渲染为文案(消息 + 可选修复建议)。 */
export function translateDiagnostic(diag: Diagnostic): string {
  return translate(diag.message_key, diag.params)
}

/** 将后端错误信封(ApiError)渲染为文案。 */
export function translateError(err: ApiError): string {
  const raw = translate(err.message_key, err.params)
  // 兜底: 文案含未替换的 {reason} 占位时,使用 err.code + status 注入,避免把模板原样返回。
  if (raw.includes('{reason}')) {
    const fallbackReason = err.code && err.code !== 'API-UNKNOWN' ? err.code : `HTTP ${err.status || 0}`
    return raw.replace(/\{reason\}/g, fallbackReason)
  }
  return raw
}

interface I18nContextValue {
  /** 当前语言。 */
  locale: Locale
  /** 切换语言(持久化并更新 document.lang)。 */
  setLocale: (locale: Locale) => void
  /** 翻译(基于当前语言)。 */
  t: (key: MessageKey, params?: Record<string, unknown>) => string
  /** 同 t,但可显式指定语言。 */
  translate: (key: MessageKey, params?: Record<string, unknown>, locale?: Locale) => string
}

const I18nContext = createContext<I18nContextValue | null>(null)

function readInitialLocale(): Locale {
  if (typeof window === 'undefined') return 'zh'
  return resolveLocale(window.localStorage.getItem(LOCALE_STORAGE_KEY))
}

/** 国际化 Provider:包裹应用根节点。 */
export function I18nProvider({ children }: { children: ReactNode }) {
  const [locale, setLocaleState] = useState<Locale>(readInitialLocale)

  useEffect(() => {
    currentLocale = locale
    document.documentElement.lang = locale === 'zh' ? 'zh-CN' : 'en'
  }, [locale])

  const setLocale = useCallback((next: Locale) => {
    setLocaleState(next)
    try {
      window.localStorage.setItem(LOCALE_STORAGE_KEY, next)
    } catch {
      // 隐私模式下 localStorage 不可用,仅内存生效
    }
  }, [])

  const value = useMemo<I18nContextValue>(
    () => ({
      locale,
      setLocale,
      t: (key, params) => translate(key, params, locale),
      translate: (key, params, lang) => translate(key, params, lang ?? locale),
    }),
    [locale, setLocale],
  )

  // 本文件为 .ts(非 .tsx),故用 createElement 而非 JSX 渲染 Provider
  return createElement(I18nContext.Provider, { value }, children)
}

/**
 * 使用国际化:t(key, params) 翻译当前语言文案。
 * 必须在 <I18nProvider> 内调用。
 */
export function useI18n(): I18nContextValue {
  const ctx = useContext(I18nContext)
  if (!ctx) {
    throw new Error('useI18n 必须在 <I18nProvider> 内使用')
  }
  return ctx
}
