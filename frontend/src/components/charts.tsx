/**
 * 原生 SVG 图表组件(不引入图表库)。
 *
 * - LineChart:多序列折线(计算步序列, 横轴为步序号),超出 maxPoints 自动抽稀, 支持可选图例开关。
 * - BarChart:分组柱状图(正/负值绕零轴展示),支持悬停/键盘聚焦读出精确封存字串。
 * - ComboChart:柱形 + 折线组合图(用于净现金流与累计现金流叠图)。
 * - CategoryLineChart:离散类目多序列折线图(用于月度/年度过程曲线),自适应抽稀横轴刻度。
 * - ScatterChart:Pareto 候选点散点图,支持键盘/鼠标选择。
 *
 * 颜色使用设计系统语义色变量,随 styles.css 主题令牌切换。
 */

import { useEffect, useMemo, useState } from 'react'

const W = 880
const PAD = { top: 14, right: 20, bottom: 32, left: 66 }

export interface LineSeries {
  key: string
  label: string
  color: string
  values: (number | null)[]
}

export interface CategorySeries {
  key: string
  label: string
  color: string
  positiveColor?: string
  negativeColor?: string
  dashed?: boolean
  values: (number | null)[]
  rawValues?: (string | null)[]
}

export interface LineChartProps {
  series: LineSeries[]
  /** 无障碍描述。 */
  ariaLabel: string
  /** Y 轴标题(可选)。 */
  yLabel?: string
  /** X 轴标题(可选, 计算步序号说明)。 */
  xLabel?: string
  /** 当前窗口首点的原始计算步，不重编号。 */
  xOffset?: number
  /** 最多渲染点数(超出按步长抽稀)。 */
  maxPoints?: number
  /** 高度(px,viewBox 逻辑单位)。 */
  height?: number
  /** 允许点击图例开关单条曲线(仅隐藏折线，保留图例项以利重开)。 */
  toggleable?: boolean
  /** 全部恢复显示按钮文案。 */
  showAllLabel?: string
}

/** 将后端字符串/数字安全转换为用于 SVG 几何定位的有限数字；无效/缺失返回 null，永不伪造为 0。 */
export function parseChartNumber(raw: string | number | null | undefined): number | null {
  if (raw === null || raw === undefined) return null
  if (typeof raw === 'number') return Number.isFinite(raw) ? raw : null
  const trimmed = raw.trim()
  if (!trimmed) return null
  const parsed = Number(trimmed)
  return Number.isFinite(parsed) ? parsed : null
}

/** 根据非空有限数值计算绘图 Y 轴上下界与 5 个均匀刻度。 */
export function computeAxisRange(
  values: (number | null)[],
  includeZero = true,
): { yMin: number; yMax: number; ticks: number[] } {
  let min = Infinity
  let max = -Infinity
  for (const v of values) {
    if (v === null || v === undefined || !Number.isFinite(v)) continue
    if (v < min) min = v
    if (v > max) max = v
  }
  if (!Number.isFinite(min) || !Number.isFinite(max)) {
    return { yMin: 0, yMax: 1, ticks: [0, 0.25, 0.5, 0.75, 1] }
  }
  if (includeZero) {
    min = Math.min(min, 0)
    max = Math.max(max, 0)
  }
  let yMin: number
  let yMax: number
  if (min === max) {
    if (min === 0) {
      yMin = 0
      yMax = 1
    } else {
      const pad = Math.abs(min) * 0.15
      yMin = includeZero ? Math.min(0, min - pad) : min - pad
      yMax = includeZero ? Math.max(0, max + pad) : max + pad
    }
  } else {
    const span = max - min
    yMin = includeZero && min === 0 ? 0 : min - span * 0.08
    yMax = includeZero && max === 0 ? 0 : max + span * 0.08
  }
  if (yMin >= yMax) {
    yMin -= 0.5
    yMax += 0.5
  }
  const ticks = Array.from({ length: 5 }, (_, i) => yMin + ((yMax - yMin) / 4) * i)
  return { yMin, yMax, ticks }
}

/** 为密集离散横轴均匀挑选刻度下标，始终保留首末标签。 */
export function pickTickIndices(length: number, maxTicks = 8): number[] {
  if (length <= 0) return []
  if (length <= maxTicks || maxTicks <= 1) {
    return maxTicks <= 1 ? [0] : Array.from({ length }, (_, i) => i)
  }
  const seen = new Set<number>()
  const out: number[] = []
  for (let i = 0; i < maxTicks; i += 1) {
    const idx = Math.round((i * (length - 1)) / (maxTicks - 1))
    if (!seen.has(idx)) {
      seen.add(idx)
      out.push(idx)
    }
  }
  if (!seen.has(length - 1)) out.push(length - 1)
  return out
}

function downsample(values: (number | null)[], step: number): (number | null)[] {
  if (step <= 1) return values
  const out: (number | null)[] = []
  for (let i = 0; i < values.length; i += step) out.push(values[i])
  return out
}

/** 将序列转换为折线点串(null 断开为多段)。 */
function toPoints(
  values: (number | null)[],
  x: (i: number) => number,
  y: (v: number) => number,
): string {
  const parts: string[] = []
  let seg: string[] = []
  values.forEach((v, i) => {
    if (v === null || v === undefined || Number.isNaN(v)) {
      if (seg.length > 1) parts.push(seg.join(' '))
      seg = []
      return
    }
    seg.push(`${x(i).toFixed(1)},${y(v).toFixed(1)}`)
  })
  if (seg.length > 1) parts.push(seg.join(' '))
  return parts.join(' ')
}

function toPolylineSegments(
  values: (number | null)[],
  x: (i: number) => number,
  y: (v: number) => number,
): string[] {
  const segments: string[] = []
  let seg: string[] = []
  values.forEach((v, i) => {
    if (v === null || v === undefined || Number.isNaN(v)) {
      if (seg.length > 1) segments.push(seg.join(' '))
      seg = []
      return
    }
    seg.push(`${x(i).toFixed(1)},${y(v).toFixed(1)}`)
  })
  if (seg.length > 1) segments.push(seg.join(' '))
  return segments
}

function formatAxisValue(v: number): string {
  const abs = Math.abs(v)
  if (abs >= 1e9 || (abs > 0 && abs < 0.01)) return v.toExponential(1).replace('e+', 'e')
  if (abs >= 1e6) return `${Number((v / 1e6).toFixed(2))}M`
  if (abs >= 1e4) return `${Number((v / 1e3).toFixed(1))}k`
  return String(Math.round(v * 100) / 100)
}

function formatReadoutValue(s: CategorySeries, idx: number, unit?: string): string {
  const raw = s.rawValues?.[idx]
  if (raw !== undefined && raw !== null && raw !== '') {
    return unit ? `${raw} ${unit}` : raw
  }
  const num = s.values[idx]
  if (num === null || num === undefined || !Number.isFinite(num)) return '—'
  const text = formatAxisValue(num)
  return unit ? `${text} ${unit}` : text
}

/** 多序列折线图。 */
export function LineChart({
  series,
  ariaLabel,
  yLabel,
  xLabel,
  xOffset = 0,
  maxPoints = 1000,
  height = 240,
  toggleable = false,
  showAllLabel = '显示全部',
}: LineChartProps) {
  const seriesSignature = useMemo(() => series.map((s) => s.key).join('\0'), [series])
  const [hiddenKeys, setHiddenKeys] = useState<Set<string>>(() => new Set())
  useEffect(() => {
    setHiddenKeys(new Set())
  }, [seriesSignature])

  const activeSeries = useMemo(
    () => (toggleable ? series.filter((s) => !hiddenKeys.has(s.key)) : series),
    [series, toggleable, hiddenKeys],
  )

  const plot = useMemo(() => {
    const n = series.reduce((max, s) => Math.max(max, s.values.length), 0)
    const step = Math.max(1, Math.ceil(n / maxPoints))
    const sampledAll = series.map((s) => ({ ...s, values: downsample(s.values, step) }))
    const sampledVisible = activeSeries.map((s) => ({ ...s, values: downsample(s.values, step) }))
    const rangeSource = sampledVisible.length > 0 ? sampledVisible : sampledAll
    const flatValues: (number | null)[] = []
    for (const s of rangeSource) {
      for (const v of s.values) flatValues.push(v)
    }
    const { yMin, yMax, ticks } = computeAxisRange(flatValues, false)
    const count = sampledAll.reduce((m, s) => Math.max(m, s.values.length), 0)
    return { sampledAll, sampledVisible, step, yMin, yMax, ticks, count }
  }, [series, activeSeries, maxPoints])

  const { sampledAll, sampledVisible, step, yMin, yMax, ticks, count } = plot
  const innerW = W - PAD.left - PAD.right
  const innerH = height - PAD.top - PAD.bottom
  const x = (i: number) => PAD.left + (count <= 1 ? innerW / 2 : (i / (count - 1)) * innerW)
  const y = (v: number) => PAD.top + ((yMax - v) / (yMax - yMin)) * innerH

  const toggleKey = (key: string) => {
    setHiddenKeys((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  return (
    <figure className="ies-chart">
      {sampledAll.length > 0 ? (
        <figcaption className="ies-chart__legend">
          {sampledAll.map((s) => {
            const hidden = toggleable && hiddenKeys.has(s.key)
            if (toggleable) {
              return (
                <button
                  key={s.key}
                  type="button"
                  className={`ies-chart__legend-item ies-chart__legend-btn${hidden ? ' ies-chart__legend-btn--hidden' : ''}`}
                  aria-pressed={!hidden}
                  onClick={() => toggleKey(s.key)}
                >
                  <span className="ies-chart__swatch" style={{ background: s.color }} aria-hidden="true" />
                  {s.label}
                </button>
              )
            }
            return (
              <span key={s.key} className="ies-chart__legend-item">
                <span className="ies-chart__swatch" style={{ background: s.color }} aria-hidden="true" />
                {s.label}
              </span>
            )
          })}
          {toggleable && hiddenKeys.size > 0 ? (
            <button
              type="button"
              className="ies-chart__legend-reset"
              onClick={() => setHiddenKeys(new Set())}
            >
              {showAllLabel}
            </button>
          ) : null}
        </figcaption>
      ) : null}
      <svg viewBox={`0 0 ${W} ${height}`} className="ies-chart__svg" role="img" aria-label={ariaLabel}>
        {ticks.map((tv, i) => (
          <g key={i}>
            <line
              x1={PAD.left}
              x2={W - PAD.right}
              y1={y(tv)}
              y2={y(tv)}
              className="ies-chart__grid"
            />
            <text x={PAD.left - 6} y={y(tv) + 3} className="ies-chart__tick" textAnchor="end">
              {formatAxisValue(tv)}
            </text>
          </g>
        ))}
        {count > 0
          ? [0, Math.max(0, count - 1)].map((i) => (
              <text
                key={i}
                x={x(i)}
                y={height - 6}
                className="ies-chart__tick"
                textAnchor={i === 0 ? 'start' : 'end'}
              >
                {xOffset + Math.round(i * step)}
              </text>
            ))
          : null}
        {xLabel ? (
          <text
            x={W / 2}
            y={height - 6}
            className="ies-chart__tick"
            textAnchor="middle"
          >
            {xLabel}
          </text>
        ) : null}
        {yLabel ? (
          <text
            x={9}
            y={height / 2}
            className="ies-chart__axis-title"
            transform={`rotate(-90 9 ${height / 2})`}
            textAnchor="middle"
          >
            {yLabel}
          </text>
        ) : null}
        {sampledVisible.map((s) => (
          <polyline
            key={s.key}
            className="ies-chart__line"
            fill="none"
            stroke={s.color}
            strokeWidth="1.5"
            strokeLinejoin="round"
            strokeLinecap="round"
            points={toPoints(s.values, x, y)}
          />
        ))}
      </svg>
    </figure>
  )
}

export interface BarChartProps {
  categories: string[]
  series: CategorySeries[]
  ariaLabel: string
  yLabel?: string
  xLabel?: string
  unit?: string
  height?: number
  hoverHint?: string
}

/** 分组柱状图(支持正负双向绕零轴绘制与悬停/聚焦显示精确封存字串)。 */
export function BarChart({
  categories,
  series,
  ariaLabel,
  yLabel,
  xLabel,
  unit,
  height = 250,
  hoverHint,
}: BarChartProps) {
  return (
    <ComboChart
      categories={categories}
      bars={series}
      lines={[]}
      ariaLabel={ariaLabel}
      yLabel={yLabel}
      xLabel={xLabel}
      unit={unit}
      height={height}
      hoverHint={hoverHint}
    />
  )
}

export interface ComboChartProps {
  categories: string[]
  bars: CategorySeries[]
  lines: CategorySeries[]
  ariaLabel: string
  yLabel?: string
  xLabel?: string
  unit?: string
  height?: number
  hoverHint?: string
}

/** 柱线组合图(例如净现金流柱形 + 累计现金流折线)。 */
export function ComboChart({
  categories,
  bars,
  lines,
  ariaLabel,
  yLabel,
  xLabel,
  unit,
  height = 260,
  hoverHint,
}: ComboChartProps) {
  const [activeIdx, setActiveIdx] = useState<number | null>(null)
  const count = categories.length

  const plot = useMemo(() => {
    const flat: (number | null)[] = []
    for (const s of bars) {
      for (const v of s.values) flat.push(v)
    }
    for (const s of lines) {
      for (const v of s.values) flat.push(v)
    }
    return computeAxisRange(flat, true)
  }, [bars, lines])

  const allSeries = useMemo(() => [...bars, ...lines], [bars, lines])
  const tickIndices = useMemo(() => pickTickIndices(count, 8), [count])

  if (count === 0 || allSeries.length === 0) return null

  const { yMin, yMax, ticks } = plot
  const innerW = W - PAD.left - PAD.right
  const innerH = height - PAD.top - PAD.bottom
  const slotW = innerW / count
  const slotX = (cIdx: number) => PAD.left + cIdx * slotW
  const centerX = (i: number) => PAD.left + (i + 0.5) * slotW
  const y = (v: number) => PAD.top + ((yMax - v) / (yMax - yMin)) * innerH
  const zeroY = y(Math.min(Math.max(0, yMin), yMax))
  const seriesCount = bars.length
  const barGap = 2
  const groupW = slotW * 0.76
  const barW =
    seriesCount > 0
      ? Math.min(32, Math.max(3, (groupW - barGap * (seriesCount - 1)) / seriesCount))
      : 0
  const actualGroupW =
    seriesCount > 0 ? seriesCount * barW + Math.max(0, seriesCount - 1) * barGap : 0
  const groupStart = (cIdx: number) => slotX(cIdx) + (slotW - actualGroupW) / 2

  return (
    <figure className="ies-chart" onMouseLeave={() => setActiveIdx(null)}>
      <figcaption className="ies-chart__legend">
        {allSeries.map((s) => (
          <span key={s.key} className="ies-chart__legend-item">
            <span className="ies-chart__swatch" style={{ background: s.color }} aria-hidden="true" />
            {s.label}
          </span>
        ))}
      </figcaption>
      <svg viewBox={`0 0 ${W} ${height}`} className="ies-chart__svg" role="img" aria-label={ariaLabel}>
        {ticks.map((tv, i) => (
          <g key={i}>
            <line x1={PAD.left} x2={W - PAD.right} y1={y(tv)} y2={y(tv)} className="ies-chart__grid" />
            <text x={PAD.left - 6} y={y(tv) + 3} className="ies-chart__tick" textAnchor="end">
              {formatAxisValue(tv)}
            </text>
          </g>
        ))}
        {yMin <= 0 && yMax >= 0 ? (
          <line x1={PAD.left} x2={W - PAD.right} y1={y(0)} y2={y(0)} className="ies-chart__zero" />
        ) : null}
        {tickIndices.map((idx) => (
          <text
            key={idx}
            x={centerX(idx)}
            y={height - 10}
            className="ies-chart__tick"
            textAnchor={idx === 0 ? 'start' : idx === count - 1 ? 'end' : 'middle'}
          >
            {categories[idx]}
          </text>
        ))}
        {xLabel ? (
          <text x={W / 2} y={height - 1} className="ies-chart__axis-title" textAnchor="middle">
            {xLabel}
          </text>
        ) : null}
        {yLabel ? (
          <text
            x={10}
            y={PAD.top + innerH / 2}
            className="ies-chart__axis-title"
            transform={`rotate(-90 10 ${PAD.top + innerH / 2})`}
            textAnchor="middle"
          >
            {yLabel}
          </text>
        ) : null}
        {categories.map((cat, idx) => (
          <rect
            key={`band-${idx}`}
            x={PAD.left + idx * slotW}
            y={PAD.top}
            width={slotW}
            height={innerH}
            className={`ies-chart__hover-band${activeIdx === idx ? ' ies-chart__hover-band--active' : ''}`}
            tabIndex={0}
            role="button"
            aria-label={`${cat}: ${allSeries.map((s) => `${s.label} ${formatReadoutValue(s, idx, unit)}`).join(', ')}`}
            onMouseEnter={() => setActiveIdx(idx)}
            onFocus={() => setActiveIdx(idx)}
          />
        ))}
        {bars.map((s, bIdx) =>
          s.values.map((v, idx) => {
            if (v === null || v === undefined || !Number.isFinite(v)) return null
            const yVal = y(v)
            const rectY = Math.min(yVal, zeroY)
            const rectH = Math.max(1.5, Math.abs(zeroY - yVal))
            const startX = groupStart(idx) + bIdx * (barW + barGap)
            const fill =
              v < 0 && s.negativeColor
                ? s.negativeColor
                : v >= 0 && s.positiveColor
                  ? s.positiveColor
                  : s.color
            return (
              <rect
                key={`${s.key}-${idx}`}
                x={startX}
                y={rectY}
                width={barW}
                height={rectH}
                rx={2}
                fill={fill}
                className="ies-chart__bar"
                opacity={activeIdx === null || activeIdx === idx ? 0.88 : 0.45}
                style={{ pointerEvents: 'none' }}
              >
                <title>{`${categories[idx]} · ${s.label}: ${formatReadoutValue(s, idx, unit)}`}</title>
              </rect>
            )
          }),
        )}
        {lines.map((s) => {
          const segs = toPolylineSegments(s.values, centerX, y)
          return (
            <g key={s.key} style={{ pointerEvents: 'none' }}>
              {segs.map((pts, segIdx) => (
                <polyline
                  key={segIdx}
                  className="ies-chart__line"
                  fill="none"
                  stroke={s.color}
                  strokeWidth="2"
                  strokeDasharray={s.dashed ? '5 3' : undefined}
                  strokeLinejoin="round"
                  strokeLinecap="round"
                  points={pts}
                />
              ))}
              {count <= 40
                ? s.values.map((v, idx) =>
                    v !== null && Number.isFinite(v) ? (
                      <circle
                        key={idx}
                        cx={centerX(idx)}
                        cy={y(v)}
                        r={activeIdx === idx ? 4.5 : 3}
                        fill={s.color}
                        stroke="var(--ies-color-surface)"
                        strokeWidth="1.2"
                      >
                        <title>{`${categories[idx]} · ${s.label}: ${formatReadoutValue(s, idx, unit)}`}</title>
                      </circle>
                    ) : null,
                  )
                : null}
            </g>
          )
        })}
      </svg>
      {activeIdx !== null && activeIdx < count ? (
        <div className="ies-chart__readout" aria-live="polite">
          <span className="ies-chart__readout-cat">{categories[activeIdx]}</span>
          {allSeries.map((s) => (
            <span key={s.key} className="ies-chart__readout-item">
              <span className="ies-chart__swatch" style={{ background: s.color }} aria-hidden="true" />
              {s.label}: {formatReadoutValue(s, activeIdx, unit)}
            </span>
          ))}
        </div>
      ) : hoverHint ? (
        <div className="ies-chart__readout">{hoverHint}</div>
      ) : null}
    </figure>
  )
}

export interface CategoryLineChartProps {
  categories: string[]
  series: CategorySeries[]
  ariaLabel: string
  yLabel?: string
  xLabel?: string
  unit?: string
  height?: number
  includeZero?: boolean
  toggleable?: boolean
  fillArea?: boolean
  showAllLabel?: string
  hoverHint?: string
}

/** 离散类目折线图(自适应横轴抽稀 + 图例开关 + 交互精确数值条)。 */
export function CategoryLineChart({
  categories,
  series,
  ariaLabel,
  yLabel,
  xLabel,
  unit,
  height = 240,
  includeZero = true,
  toggleable = true,
  fillArea = false,
  showAllLabel = '显示全部',
  hoverHint,
}: CategoryLineChartProps) {
  const seriesSignature = useMemo(() => series.map((s) => s.key).join('\0'), [series])
  const [hiddenKeys, setHiddenKeys] = useState<Set<string>>(() => new Set())
  const [activeIdx, setActiveIdx] = useState<number | null>(null)
  useEffect(() => {
    setHiddenKeys(new Set())
    setActiveIdx(null)
  }, [seriesSignature, categories.length])

  const activeSeries = useMemo(
    () => (toggleable ? series.filter((s) => !hiddenKeys.has(s.key)) : series),
    [series, toggleable, hiddenKeys],
  )
  const count = categories.length
  const plot = useMemo(() => {
    const source = activeSeries.length > 0 ? activeSeries : series
    const flat: (number | null)[] = []
    for (const s of source) {
      for (const v of s.values) flat.push(v)
    }
    return computeAxisRange(flat, includeZero)
  }, [activeSeries, series, includeZero])

  const tickIndices = useMemo(() => pickTickIndices(count, 8), [count])

  if (count === 0 || series.length === 0) return null

  const { yMin, yMax, ticks } = plot
  const innerW = W - PAD.left - PAD.right
  const innerH = height - PAD.top - PAD.bottom
  const x = (i: number) => PAD.left + (count <= 1 ? innerW / 2 : (i / (count - 1)) * innerW)
  const y = (v: number) => PAD.top + ((yMax - v) / (yMax - yMin)) * innerH
  const bandW = count <= 1 ? innerW : innerW / count

  const toggleKey = (key: string) => {
    setHiddenKeys((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  return (
    <figure className="ies-chart" onMouseLeave={() => setActiveIdx(null)}>
      <figcaption className="ies-chart__legend">
        {series.map((s) => {
          const hidden = toggleable && hiddenKeys.has(s.key)
          if (toggleable) {
            return (
              <button
                key={s.key}
                type="button"
                className={`ies-chart__legend-item ies-chart__legend-btn${hidden ? ' ies-chart__legend-btn--hidden' : ''}`}
                aria-pressed={!hidden}
                onClick={() => toggleKey(s.key)}
              >
                <span className="ies-chart__swatch" style={{ background: s.color }} aria-hidden="true" />
                {s.label}
              </button>
            )
          }
          return (
            <span key={s.key} className="ies-chart__legend-item">
              <span className="ies-chart__swatch" style={{ background: s.color }} aria-hidden="true" />
              {s.label}
            </span>
          )
        })}
        {toggleable && hiddenKeys.size > 0 ? (
          <button
            type="button"
            className="ies-chart__legend-reset"
            onClick={() => setHiddenKeys(new Set())}
          >
            {showAllLabel}
          </button>
        ) : null}
      </figcaption>
      <svg viewBox={`0 0 ${W} ${height}`} className="ies-chart__svg" role="img" aria-label={ariaLabel}>
        {ticks.map((tv, i) => (
          <g key={i}>
            <line x1={PAD.left} x2={W - PAD.right} y1={y(tv)} y2={y(tv)} className="ies-chart__grid" />
            <text x={PAD.left - 6} y={y(tv) + 3} className="ies-chart__tick" textAnchor="end">
              {formatAxisValue(tv)}
            </text>
          </g>
        ))}
        {yMin <= 0 && yMax >= 0 ? (
          <line x1={PAD.left} x2={W - PAD.right} y1={y(0)} y2={y(0)} className="ies-chart__zero" />
        ) : null}
        {tickIndices.map((idx) => (
          <text
            key={idx}
            x={x(idx)}
            y={height - 10}
            className="ies-chart__tick"
            textAnchor={idx === 0 && count > 1 ? 'start' : idx === count - 1 && count > 1 ? 'end' : 'middle'}
          >
            {categories[idx]}
          </text>
        ))}
        {xLabel ? (
          <text x={W / 2} y={height - 1} className="ies-chart__axis-title" textAnchor="middle">
            {xLabel}
          </text>
        ) : null}
        {yLabel ? (
          <text
            x={10}
            y={PAD.top + innerH / 2}
            className="ies-chart__axis-title"
            transform={`rotate(-90 10 ${PAD.top + innerH / 2})`}
            textAnchor="middle"
          >
            {yLabel}
          </text>
        ) : null}
        {categories.map((cat, idx) => (
          <rect
            key={`band-${idx}`}
            x={count <= 1 ? PAD.left : Math.max(PAD.left, x(idx) - bandW / 2)}
            y={PAD.top}
            width={bandW}
            height={innerH}
            className={`ies-chart__hover-band${activeIdx === idx ? ' ies-chart__hover-band--active' : ''}`}
            tabIndex={0}
            role="button"
            aria-label={`${cat}: ${activeSeries.map((s) => `${s.label} ${formatReadoutValue(s, idx, unit)}`).join(', ')}`}
            onMouseEnter={() => setActiveIdx(idx)}
            onFocus={() => setActiveIdx(idx)}
          />
        ))}
        {activeSeries.map((s) => {
          const segs = toPolylineSegments(s.values, x, y)
          const baselineY = y(Math.min(Math.max(0, yMin), yMax))
          return (
            <g key={s.key} style={{ pointerEvents: 'none' }}>
              {fillArea
                ? segs.map((pts, segIdx) => {
                    const tokens = pts.split(' ')
                    const firstX = tokens[0]?.split(',')[0]
                    const lastX = tokens[tokens.length - 1]?.split(',')[0]
                    if (!firstX || !lastX) return null
                    return (
                      <polygon
                        key={`area-${segIdx}`}
                        fill={s.color}
                        fillOpacity="0.18"
                        points={`${firstX},${baselineY.toFixed(1)} ${pts} ${lastX},${baselineY.toFixed(1)}`}
                      />
                    )
                  })
                : null}
              {segs.map((pts, segIdx) => (
                <polyline
                  key={segIdx}
                  className="ies-chart__line"
                  fill="none"
                  stroke={s.color}
                  strokeWidth="2"
                  strokeDasharray={s.dashed ? '5 3' : undefined}
                  strokeLinejoin="round"
                  strokeLinecap="round"
                  points={pts}
                />
              ))}
              {count <= 48
                ? s.values.map((v, idx) =>
                    v !== null && Number.isFinite(v) ? (
                      <circle
                        key={idx}
                        cx={x(idx)}
                        cy={y(v)}
                        r={activeIdx === idx ? 4.5 : 3}
                        fill={s.color}
                        stroke="var(--ies-color-surface)"
                        strokeWidth="1.2"
                      >
                        <title>{`${categories[idx]} · ${s.label}: ${formatReadoutValue(s, idx, unit)}`}</title>
                      </circle>
                    ) : null,
                  )
                : null}
            </g>
          )
        })}
      </svg>
      {activeIdx !== null && activeIdx < count ? (
        <div className="ies-chart__readout" aria-live="polite">
          <span className="ies-chart__readout-cat">{categories[activeIdx]}</span>
          {activeSeries.map((s) => (
            <span key={s.key} className="ies-chart__readout-item">
              <span className="ies-chart__swatch" style={{ background: s.color }} aria-hidden="true" />
              {s.label}: {formatReadoutValue(s, activeIdx, unit)}
            </span>
          ))}
        </div>
      ) : hoverHint ? (
        <div className="ies-chart__readout">{hoverHint}</div>
      ) : null}
    </figure>
  )
}

export interface ScatterPoint {
  id: string
  x: number
  y: number
  label: string
}

export interface ScatterChartProps {
  points: ScatterPoint[]
  ariaLabel: string
  xLabel: string
  yLabel: string
  selectedId?: string | null
  onSelect?: (id: string) => void
  width?: number
  height?: number
}

/** Pareto 候选点散点图(可选中点)。 */
export function ScatterChart({
  points,
  ariaLabel,
  xLabel,
  yLabel,
  selectedId,
  onSelect,
  width = W,
  height = 300,
}: ScatterChartProps) {
  const plot = useMemo(() => {
    let xMin = Infinity
    let xMax = -Infinity
    let yMin = Infinity
    let yMax = -Infinity
    for (const p of points) {
      if (p.x < xMin) xMin = p.x
      if (p.x > xMax) xMax = p.x
      if (p.y < yMin) yMin = p.y
      if (p.y > yMax) yMax = p.y
    }
    if (!Number.isFinite(xMin) || !Number.isFinite(xMax)) {
      xMin = 0
      xMax = 1
    }
    if (!Number.isFinite(yMin) || !Number.isFinite(yMax)) {
      yMin = 0
      yMax = 1
    }
    if (xMin === xMax) {
      xMin -= 1
      xMax += 1
    }
    if (yMin === yMax) {
      yMin -= 1
      yMax += 1
    }
    const padX = (xMax - xMin) * 0.08
    const padY = (yMax - yMin) * 0.08
    return { xMin: xMin - padX, xMax: xMax + padX, yMin: yMin - padY, yMax: yMax + padY }
  }, [points])

  const innerW = width - PAD.left - PAD.right
  const innerH = height - PAD.top - PAD.bottom
  const x = (v: number) => PAD.left + ((v - plot.xMin) / (plot.xMax - plot.xMin)) * innerW
  const y = (v: number) => PAD.top + ((plot.yMax - v) / (plot.yMax - plot.yMin)) * innerH

  if (points.length === 0) return null

  return (
    <svg
      viewBox={`0 0 ${width} ${height}`}
      className="ies-chart__svg"
      role="img"
      aria-label={ariaLabel}
    >
      <line
        x1={PAD.left}
        x2={PAD.left}
        y1={PAD.top}
        y2={PAD.top + innerH}
        className="ies-chart__axis"
      />
      <line
        x1={PAD.left}
        x2={PAD.left + innerW}
        y1={PAD.top + innerH}
        y2={PAD.top + innerH}
        className="ies-chart__axis"
      />
      <text
        x={PAD.left}
        y={PAD.top + innerH + 16}
        textAnchor="middle"
        className="ies-chart__tick"
      >
        {formatAxisValue(plot.xMin)}
      </text>
      <text
        x={PAD.left + innerW}
        y={PAD.top + innerH + 16}
        textAnchor="middle"
        className="ies-chart__tick"
      >
        {formatAxisValue(plot.xMax)}
      </text>
      <text x={PAD.left - 6} y={PAD.top + innerH} textAnchor="end" className="ies-chart__tick">
        {formatAxisValue(plot.yMin)}
      </text>
      <text x={PAD.left - 6} y={PAD.top} textAnchor="end" className="ies-chart__tick">
        {formatAxisValue(plot.yMax)}
      </text>
      <text
        x={PAD.left + innerW / 2}
        y={height - 2}
        textAnchor="middle"
        className="ies-chart__axis-title"
      >
        {xLabel}
      </text>
      <text
        x={9}
        y={PAD.top + innerH / 2}
        transform={`rotate(-90 9 ${PAD.top + innerH / 2})`}
        textAnchor="middle"
        className="ies-chart__axis-title"
      >
        {yLabel}
      </text>
      {points.map((p) => {
        const selected = p.id === selectedId
        return (
          <circle
            key={p.id}
            cx={x(p.x)}
            cy={y(p.y)}
            r={selected ? 6 : 4}
            className={
              selected
                ? 'ies-scatter__point ies-scatter__point--selected'
                : 'ies-scatter__point'
            }
            tabIndex={0}
            role="button"
            aria-label={p.label}
            aria-pressed={selected || undefined}
            onClick={() => onSelect?.(p.id)}
            onKeyDown={(event) => {
              if ((event.key === 'Enter' || event.key === ' ') && onSelect) {
                event.preventDefault()
                onSelect(p.id)
              }
            }}
          >
            <title>{p.label}</title>
          </circle>
        )
      })}
    </svg>
  )
}
