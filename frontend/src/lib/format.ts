/**
 * 数值/单位/日期时间格式化。
 *
 * - 单位显示:中文用翻译键(ies.unit.*),英文直接用符号;locale 由 i18n 模块
 *   提供(getLocale),非 React 环境亦可调用。
 * - 金额按币种(CNY/USD)与语言格式化。
 * - 日期时间默认按浏览器本地时区展示;项目场景可传固定 UTC 偏移(分钟),
 *   与后端"项目固定偏移"约定一致(数据时间戳均按项目偏移解释)。
 */

import { getLocale, translate } from '../i18n'
import type { Locale } from '../i18n'

// ---------------------------------------------------------------------------
// Intl 缓存
// ---------------------------------------------------------------------------

function intlLocale(locale?: Locale): string {
  return (locale ?? getLocale()) === 'zh' ? 'zh-CN' : 'en-US'
}

const numberCache = new Map<string, Intl.NumberFormat>()
const dateTimeCache = new Map<string, Intl.DateTimeFormat>()

function numberFormat(locale: string, options?: Intl.NumberFormatOptions): Intl.NumberFormat {
  const key = locale + JSON.stringify(options)
  let fmt = numberCache.get(key)
  if (!fmt) {
    fmt = new Intl.NumberFormat(locale, options)
    numberCache.set(key, fmt)
  }
  return fmt
}

function dateTimeFormat(locale: string, options?: Intl.DateTimeFormatOptions): Intl.DateTimeFormat {
  const key = locale + JSON.stringify(options)
  let fmt = dateTimeCache.get(key)
  if (!fmt) {
    fmt = new Intl.DateTimeFormat(locale, options)
    dateTimeCache.set(key, fmt)
  }
  return fmt
}

// ---------------------------------------------------------------------------
// 数值
// ---------------------------------------------------------------------------

export interface NumberOptions {
  /** 小数位(缺省按值自动取 0-3 位)。 */
  digits?: number
  /** 千分位分隔。 */
  group?: boolean
  /** 符号位。 */
  sign?: 'auto' | 'always'
  /** 百分比/科学等样式。 */
  style?: 'decimal' | 'percent'
}

/** 通用数值格式化(按当前语言)。 */
export function formatNumber(value: number | null | undefined, opts: NumberOptions = {}): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  const digits = opts.digits ?? Math.min(3, Math.max(0, Math.abs(value) >= 100 ? 0 : Math.abs(value) >= 10 ? 1 : 2))
  return numberFormat(intlLocale(), {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
    useGrouping: opts.group ?? true,
    style: opts.style === 'percent' ? 'percent' : 'decimal',
    signDisplay: opts.sign === 'always' ? 'always' : 'auto',
  }).format(value)
}

/** 百分比(0.1234 -> 12.34%)。 */
export function formatPercent(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  return numberFormat(intlLocale(), {
    style: 'percent',
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(value)
}

// ---------------------------------------------------------------------------
// 能量 / 功率 / 温度
// ---------------------------------------------------------------------------

/** 字节数可读化(B/KB/MB/GB; 存储健康视图用)。 */
export function formatBytes(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let v = value
  let unit = 0
  while (v >= 1024 && unit < units.length - 1) {
    v /= 1024
    unit += 1
  }
  const digits = unit === 0 ? 0 : v >= 100 ? 0 : v >= 10 ? 1 : 2
  return `${numberFormat(intlLocale(), {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
    useGrouping: true,
  }).format(v)} ${units[unit]}`
}

/** 二氧化碳排放格式化(kg -> tCO2)。 */
export function formatCo2(valueKg: number | null | undefined): string {
  if (valueKg === null || valueKg === undefined || Number.isNaN(valueKg)) return '—'
  return `${formatNumber(valueKg / 1000, { digits: 2 })} ${unitLabel('ies.unit.co2')}`
}

/** 单位文案:中文取翻译键,英文直接符号。 */
export function unitLabel(unitKey: string): string {
  const lang = getLocale()
  if (lang === 'zh') return translate(unitKey)
  const direct = unitKey.replace(/^ies\.unit\./, '')
  return direct
}

// ---------------------------------------------------------------------------
// 金额
// ---------------------------------------------------------------------------

export interface MoneyOptions {
  /** 小数位(缺省 2;大额自动归整)。 */
  digits?: number
  /** 是否显示货币符号。 */
  symbol?: boolean
  /** 财务场景显示千分位。 */
  group?: boolean
}

/** 金额格式化(按币种与语言)。 */
export function formatMoney(
  value: number | null | undefined,
  currency: 'CNY' | 'USD' = 'CNY',
  opts: MoneyOptions = {},
): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  const digits = opts.digits ?? (Math.abs(value) >= 100_000_000 ? 0 : 2)
  const sign = value < 0 ? '-' : ''
  const abs = Math.abs(value)
  const body = numberFormat(intlLocale(), {
    style: 'decimal',
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
    useGrouping: opts.group ?? true,
  }).format(abs)
  return `${sign}${symbolOf(currency)}${body}`
}

function symbolOf(currency: 'CNY' | 'USD'): string {
  if (getLocale() === 'zh') return currency === 'CNY' ? '¥' : 'US$'
  return currency === 'CNY' ? 'CN¥' : '$'
}

// ---------------------------------------------------------------------------
// 日期时间
// ---------------------------------------------------------------------------

export interface DateTimeOptions {
  /** 以项目固定 UTC 偏移(分钟)解释时间戳;缺省用浏览器本地时区。 */
  utcOffsetMinutes?: number
  /** 是否包含秒。 */
  withSeconds?: boolean
}

function toLocalDate(iso: string, utcOffsetMinutes?: number): Date {
  const date = new Date(iso)
  if (utcOffsetMinutes === undefined) return date
  // 按项目固定偏移展示:先转 UTC 毫秒,再偏移(仅展示用途)
  return new Date(date.getTime() + utcOffsetMinutes * 60_000)
}

/** 日期 + 时间(YYYY-MM-DD HH:mm)。 */
export function formatDateTime(iso: string | null | undefined, opts: DateTimeOptions = {}): string {
  if (!iso) return '—'
  const date = toLocalDate(iso, opts.utcOffsetMinutes)
  return dateTimeFormat(intlLocale(), {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: opts.withSeconds ? '2-digit' : undefined,
    hour12: false,
  }).format(date)
}

/** 仅日期。 */
export function formatDate(iso: string | null | undefined, utcOffsetMinutes?: number): string {
  if (!iso) return '—'
  return dateTimeFormat(intlLocale(), {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).format(toLocalDate(iso, utcOffsetMinutes))
}

/** 相对时间(刚刚 / N 分钟前 / N 小时前 / N 天前)。 */
export function formatRelativeTime(iso: string | null | undefined, now: number = Date.now()): string {
  if (!iso) return '—'
  const deltaMs = now - new Date(iso).getTime()
  const minutes = Math.floor(deltaMs / 60_000)
  const zh = getLocale() === 'zh'
  if (minutes < 1) return zh ? '刚刚' : 'just now'
  if (minutes < 60) return zh ? `${minutes} 分钟前` : `${minutes} min ago`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return zh ? `${hours} 小时前` : `${hours} h ago`
  const days = Math.floor(hours / 24)
  return zh ? `${days} 天前` : `${days} d ago`
}
