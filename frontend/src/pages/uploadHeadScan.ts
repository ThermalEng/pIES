/** 上传文件头判定编排(纯逻辑, 无 React, 供 DataPage 与 node 回归共用)。
 *
 * 时序契约(复审裁决):
 * 1. 文件头判定完成前不可提交 —— 调用方以 `scanning` 状态禁用提交;
 * 2. 新选择使旧读取结果失效 —— 世代号守卫, 旧世代 settled 直接丢弃;
 * 3. 读取失败给可见错误 —— `ok: false` 上报, 调用方展示, 不静默回退 generic。
 */

export type HeadScanMode = 'generic' | 'device-data'

export interface HeadScanDecision {
  mode: HeadScanMode
  resolution: string | null
}

/** 检测文件头 schema 标记(仅用于显示与组装请求, 后端仍权威校验)。 */
export function decideHeadMode(head: string): HeadScanDecision {
  let schema: string | null = null
  let resolution: string | null = null
  for (const line of head.split('\n')) {
    const stripped = line.trim()
    if (!stripped) continue
    if (!stripped.startsWith('#')) break
    const body = stripped.slice(1).trim()
    const idx = body.indexOf(':')
    if (idx < 0) continue
    const key = body.slice(0, idx).trim()
    const value = body.slice(idx + 1).trim()
    if (key === 'schema' && schema === null) schema = value
    if (key === 'resolution' && resolution === null) resolution = value
  }
  if (schema === 'ies.device-data' && resolution) {
    return { mode: 'device-data', resolution }
  }
  return { mode: 'generic', resolution: null }
}

export type HeadScanOutcome =
  | { ok: true; decision: HeadScanDecision }
  | { ok: false }

export interface HeadScanSink {
  onSettled(seq: number, outcome: HeadScanOutcome): void
}

export type HeadReader = (file: File) => Promise<string>

const HEAD_BYTES = 4096

function defaultReadHead(file: File): Promise<string> {
  return file.slice(0, HEAD_BYTES).text()
}

/** 创建一次对话框生命期内的判定器(调用方持有单例, 世代号跨选择单调递增)。 */
/** 头判定状态: 提交可否进行的唯一判据(`ready` 以外一律不可提交)。
 *
 * - `empty`: 未选文件;
 * - `pending`: 已选文件, 头判定进行中;
 * - `ready`: 头判定成功完成(本轮选择有效);
 * - `failed`: 头读取失败, 本轮选择作废, 只有重新选择并成功判定才可恢复。
 */
export type HeadScanStatus = 'empty' | 'pending' | 'ready' | 'failed'

export type HeadScanEvent = 'select' | 'clear' | 'ok' | 'fail' | 'reset'

/** 状态迁移(纯函数): `failed` 只能经 `select`(重新选择)回到 `pending`,
 * 经 `ok` 才能到 `ready`; 世代过期由调用方按 `isCurrent` 先行丢弃。 */
export function nextScanStatus(current: HeadScanStatus, event: HeadScanEvent): HeadScanStatus {
  if (event === 'reset' || event === 'clear') return 'empty'
  if (event === 'select') return 'pending'
  if (current !== 'pending') return current
  if (event === 'ok') return 'ready'
  return 'failed'
}

/** 该状态是否允许提交(按钮禁用与 submit 内守卫共用此判据)。 */
export function canSubmitScan(status: HeadScanStatus): boolean {
  return status === 'ready'
}

export function createHeadScan(sink: HeadScanSink, readHead: HeadReader = defaultReadHead) {
  let seq = 0
  return {
    /** 开始一次判定并返回世代号; `file` 为空表示清除选择, 直接以 generic 结算。 */
    scan(file: File | null): number {
      seq += 1
      const mine = seq
      if (!file) {
        sink.onSettled(mine, { ok: true, decision: { mode: 'generic', resolution: null } })
        return mine
      }
      readHead(file).then(
        (head) => sink.onSettled(mine, { ok: true, decision: decideHeadMode(head) }),
        () => sink.onSettled(mine, { ok: false }),
      )
      return mine
    },
    /** 该世代是否仍为最新(调用方丢弃旧世代 settled)。 */
    isCurrent(s: number): boolean {
      return s === seq
    },
    /** 作废进行中的判定(对话框重置时调用, 迟到 settled 一律丢弃)。 */
    invalidate(): void {
      seq += 1
    },
  }
}
