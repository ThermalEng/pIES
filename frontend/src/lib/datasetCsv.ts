/**
 * 数据集 CSV 工具:标准模板与合成数据生成。
 *
 * 与设计输入 §8(数据约束)及 §15.3(时序数据字段)对齐:
 * - 标准非闰年 365 天;分辨率 15/30/60 分钟;
 * - 首列为时间戳(本地墙钟时间,ISO8601 无偏移后缀,由数据集声明的固定 UTC 偏移解释);
 * - 字段:电/热/冷负荷(kWh)、环境温度(°C)、水平面总辐照度(W/m²)、
 *   购电价格(元/kWh)、电网排放因子(kgCO₂/kWh);
 * - 生成函数均为纯函数(确定性合成,便于复现与测试)。
 */

export interface ResolutionOption {
  /** 分辨率标识,对齐 dataset_versions.resolution('15min'/'30min'/'1h')。 */
  value: string
  /** 时间轴粒度,对齐 Timeline 类型。 */
  timeline: 'quarter_hourly' | 'custom' | 'hourly'
  /** 每段分钟数。 */
  minutes: number
}

/** 支持的分辨率(设计输入 §8.1:15 分钟、30 分钟、1 小时)。 */
export const RESOLUTION_OPTIONS: readonly ResolutionOption[] = [
  { value: '15min', timeline: 'quarter_hourly', minutes: 15 },
  { value: '30min', timeline: 'custom', minutes: 30 },
  { value: '1h', timeline: 'hourly', minutes: 60 },
]

/** 标准非闰年天数(设计输入 §8.1:年度计算采用标准非闰年 365 天)。 */
export const STANDARD_YEAR_DAYS = 365

export function resolutionOption(value: string): ResolutionOption {
  return RESOLUTION_OPTIONS.find((r) => r.value === value) ?? RESOLUTION_OPTIONS[2]
}

/** 某分辨率下全年期望行数(365 天 × 24 小时 × 60 分钟 / 段长)。 */
export function expectedRows(resolution: ResolutionOption): number {
  return ((STANDARD_YEAR_DAYS * 24 * 60) / resolution.minutes) | 0
}

/** 标准表头(与后端 STANDARD_FIELDS 列序一致: timestamp + 7 个标准字段)。 */
export const HEADERS = [
  'timestamp',
  'e_load',
  'h_load',
  'c_load',
  't_ambient',
  'ghi',
  'electricity_price',
  'grid_emission_factor',
] as const

const MONTH_DAYS = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]

/** 一年内第 dayIndex 天(0 起)对应的月/日(非闰年)。 */
function monthDayOf(dayIndex: number): { month: number; day: number } {
  let m = 0
  let d = dayIndex
  while (m < 11 && d >= MONTH_DAYS[m]) {
    d -= MONTH_DAYS[m]
    m += 1
  }
  return { month: m + 1, day: d + 1 }
}

/** 本地墙钟时间戳(无偏移后缀,由数据集 fixed_utc_offset_minutes 解释)。 */
function localIso(month: number, day: number, hour: number, minute: number): string {
  const p2 = (n: number) => String(n).padStart(2, '0')
  return `2025-${p2(month)}-${p2(day)}T${p2(hour)}:${p2(minute)}:00`
}

/** 确定性伪噪声(0-1),避免 Math.random 使合成数据不可复现。 */
function noise(day: number, step: number): number {
  return Math.abs(Math.sin(day * 127.1 + step * 311.7)) % 1
}

/**
 * 标准 CSV 模板:表头 + 3 行示例(1 月 1 日 00:00-02:00)。
 * 后端模板接口不可用时前端回退使用(见 DataPage 模板下载)。
 */
export function csvTemplate(): string {
  const lines: string[] = [HEADERS.join(',')]
  for (let h = 0; h < 3; h += 1) {
    lines.push(
      [
        localIso(1, 1, h, 0),
        '800.00',
        '600.00',
        '400.00',
        '10.0',
        '0.0',
        '0.3580',
        '0.5810',
      ].join(','),
    )
  }
  return `${lines.join('\n')}\n`
}

/**
 * 合成全年数据(标准非闰年 365 天,按分辨率)。
 * 典型小型工业园区场景(设计输入 §15.1):工作日负荷高、冬季供热、夏季供冷。
 */
export function syntheticCsv(resolution: ResolutionOption): string {
  const lines: string[] = [HEADERS.join(',')]
  const stepsPerDay = (24 * 60) / resolution.minutes
  for (let day = 0; day < STANDARD_YEAR_DAYS; day += 1) {
    const { month, day: dayOfMonth } = monthDayOf(day)
    const weekdayFactor = day % 7 < 5 ? 1 : 0.22
    // 季节性温度:冬季约 2°C,夏季约 26°C
    const seasonal = 14 - 12 * Math.cos((2 * Math.PI * (day - 15)) / STANDARD_YEAR_DAYS)
    for (let step = 0; step < stepsPerDay; step += 1) {
      const minuteOfDay = (step * resolution.minutes) % (24 * 60)
      const hour = minuteOfDay / 60
      const n = noise(day, step)
      // 园区日负荷曲线:夜间 30%,白天(06-22 时)峰值
      const dailyShape = hour >= 7 && hour <= 20 ? 0.5 + 0.5 * Math.sin((Math.PI * (hour - 7)) / 13) : 0.3
      const eLoad = weekdayFactor * (420 + 320 * dailyShape) * (1 + 0.05 * n)
      const tAmb = seasonal + 3 * Math.sin((2 * Math.PI * hour) / 24 - Math.PI / 2) + 2 * n
      const hLoad =
        weekdayFactor * 380 * Math.max(0, (14 - tAmb) / 14) * (hour >= 5 && hour <= 11 ? 1.25 : 0.6)
      const cLoad =
        weekdayFactor * 420 * Math.max(0, (tAmb - 18) / 10) * (hour >= 9 && hour <= 19 ? 1.2 : 0.5)
      const ghi = hour >= 6 && hour <= 18 ? 900 * Math.sin((Math.PI * (hour - 6)) / 12) * (0.85 + 0.15 * n) : 0
      const price =
        (hour >= 8 && hour <= 11) || (hour >= 18 && hour <= 21)
          ? 1.05
          : hour >= 22 || hour < 7
            ? 0.35
            : 0.55
      lines.push(
        [
          localIso(month, dayOfMonth, Math.floor(hour), minuteOfDay % 60),
          eLoad.toFixed(2),
          hLoad.toFixed(2),
          cLoad.toFixed(2),
          tAmb.toFixed(1),
          ghi.toFixed(1),
          price.toFixed(4),
          '0.5810',
        ].join(','),
      )
    }
  }
  return `${lines.join('\n')}\n`
}
