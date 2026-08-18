/**
 * 原生 SVG 图表组件(不引入图表库)。
 *
 * - LineChart:多序列折线(逐时曲线),超出 maxPoints 自动抽稀(8760 点 -> 约 1000 点)。
 * - ScatterChart:Pareto 候选点散点图,支持键盘/鼠标选择。
 *
 * 颜色使用设计系统语义色的十六进制值,与 styles.css 令牌一致。
 */

import { useMemo } from 'react'

const W = 720
const PAD = { top: 10, right: 14, bottom: 26, left: 58 }

export interface LineSeries {
  key: string
  label: string
  color: string
  values: (number | null)[]
}

export interface LineChartProps {
  series: LineSeries[]
  /** 无障碍描述。 */
  ariaLabel: string
  /** Y 轴标题(可选)。 */
  yLabel?: string
  /** 最多渲染点数(超出按步长抽稀)。 */
  maxPoints?: number
  /** 高度(px,viewBox 逻辑单位)。 */
  height?: number
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

function formatAxisValue(v: number): string {
  const abs = Math.abs(v)
  if (abs >= 10000 || (abs > 0 && abs < 0.01)) return v.toExponential(1).replace('e+', 'e')
  return String(Math.round(v * 100) / 100)
}

/** 多序列折线图。 */
export function LineChart({ series, ariaLabel, yLabel, maxPoints = 1000, height = 240 }: LineChartProps) {
  const plot = useMemo(() => {
    const n = series.reduce((max, s) => Math.max(max, s.values.length), 0)
    const step = Math.max(1, Math.ceil(n / maxPoints))
    const sampled = series.map((s) => ({ ...s, values: downsample(s.values, step) }))
    let min = Infinity
    let max = -Infinity
    for (const s of sampled) {
      for (const v of s.values) {
        if (v === null || v === undefined || Number.isNaN(v)) continue
        if (v < min) min = v
        if (v > max) max = v
      }
    }
    if (!Number.isFinite(min) || !Number.isFinite(max)) {
      min = 0
      max = 1
    }
    if (min === max) {
      min -= 1
      max += 1
    }
    const span = max - min
    const count = sampled.reduce((m, s) => Math.max(m, s.values.length), 0)
    return { sampled, step, yMin: min - span * 0.08, yMax: max + span * 0.08, count }
  }, [series, maxPoints])

  const { sampled, step, yMin, yMax, count } = plot
  const innerW = W - PAD.left - PAD.right
  const innerH = height - PAD.top - PAD.bottom
  const x = (i: number) => PAD.left + (count <= 1 ? innerW / 2 : (i / (count - 1)) * innerW)
  const y = (v: number) => PAD.top + ((yMax - v) / (yMax - yMin)) * innerH

  const ticks = 4
  const yTicks = Array.from({ length: ticks + 1 }, (_, i) => yMin + ((yMax - yMin) / ticks) * i)

  return (
    <figure className="ies-chart">
      {sampled.length > 0 ? (
        <figcaption className="ies-chart__legend">
          {sampled.map((s) => (
            <span key={s.key} className="ies-chart__legend-item">
              <span className="ies-chart__swatch" style={{ background: s.color }} aria-hidden="true" />
              {s.label}
            </span>
          ))}
        </figcaption>
      ) : null}
      <svg viewBox={`0 0 ${W} ${height}`} className="ies-chart__svg" role="img" aria-label={ariaLabel}>
        {yTicks.map((tv, i) => (
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
                H{Math.round(i * step)}
              </text>
            ))
          : null}
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
        {sampled.map((s) => (
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
