/**
 * 任务选择解析定向测试(纯函数, 无浏览器依赖)。
 *
 * 覆盖 `src/lib/taskSelection.ts` 的裁决语义：
 * 1. 默认取完成顺序最晚的已完成计算(最新尝试 finished_at，与有无证据无关)；
 * 2. 早提交慢完成 T1 / 晚提交快完成 T2 下默认选中 T1(完成最晚)，
 *    导出与结果页同规则，不再按 requested_at 分歧；
 * 2b. 完成最晚者无可用结果时返回 null，不回退更早任务；
 * 3. 显式选择已完成任务只绑定该任务；
 * 4. 显式选择未完成/无结果/未知任务返回 null，永不回退到其他任务；
 * 4a2. 未完成任务即使携带有证据行也不得绑定（状态门禁）；
 * 5. 空详情返回 null。
 * 6. selectDefaultTaskId 在 12 条列表项上确定完成最晚者（早创建晚完成胜出、
 *    时刻持平按 id 倒序、缺席最末、空输入 null），供钩子走完历史分页后调用；
 *    截断窗口回归由本组覆盖（旧 slice(0,10) 在此规模下必错）。
 *
 * 运行（Docker，复用已缓存的 playwright 镜像，不新增镜像/依赖）：
 *   docker run --rm -v "$PWD/frontend:/app:ro" --entrypoint sh \
 *     mcr.microsoft.com/playwright:v1.54.2-noble -c "
 *     mkdir -p /tmp/tsel && cp /app/src/lib/taskSelection.ts /tmp/tsel/ &&
 *     cp /app/src/types.ts /tmp/tsel/ && cp /app/tests/task_selection.test.mjs /tmp/tsel/ &&
 *     sed -i \"s#'../src/lib/taskSelection.ts'#'./taskSelection.ts'#; s#from '../types'#from './types.ts'#\" /tmp/tsel/taskSelection.ts /tmp/tsel/task_selection.test.mjs &&
 *     node /tmp/tsel/task_selection.test.mjs"
 * （node22 自带类型剥离；仅给相对导入补机械后缀，逻辑与仓库文件一致。）
 */

import { selectDefaultTaskId, selectEvidencePackage } from '../src/lib/taskSelection.ts'

let failures = 0
function check(name, cond, detail = '') {
  if (!cond) failures++
  console.log(`[${cond ? 'PASS' : 'FAIL'}] ${name}${detail ? ` — ${detail}` : ''}`)
}

/** 最小任务详情桩：只含选择解析消费的 id/status/evidence/attempts。 */
function detail(id, packageIds, finishedAt) {
  const completed = finishedAt !== null && finishedAt !== undefined
  return {
    id,
    project_id: 7,
    type: 'calc',
    status: completed ? 'completed' : 'running',
    evidence: packageIds.map((p) => ({ package_id: p, object_id: 100 + p, status: 'complete' })),
    attempts: completed ? [{ attempt_no: 1, status: 'succeeded', finished_at: finishedAt }] : [],
  }
}

// 早提交慢完成 T1(后完成 10:20) / 晚提交快完成 T2(先完成 10:08)
const t1 = detail(11, [2], '2026-10-14T10:20:00+00:00')
const t2 = detail(12, [1], '2026-10-14T10:08:00+00:00')

// 1. 默认取完成顺序最晚者（T1），不按提交顺序或包 id 猜
const dflt = selectEvidencePackage([t1, t2], null)
check('default picks latest completed', dflt?.taskId === 11 && dflt?.packageId === 2, JSON.stringify(dflt))

// 2. 顺序无关：输入顺序调换仍选中 T1
const dfltSwapped = selectEvidencePackage([t2, t1], null)
check(
  'default order-independent',
  dfltSwapped?.taskId === 11 && dfltSwapped?.packageId === 2,
  JSON.stringify(dfltSwapped),
)

// 2b. 完成最晚者无可用结果 → null，不回退到更早有结果的任务
const t3noevidence = detail(13, [], '2026-10-14T10:30:00+00:00')
const dfltNoFallback = selectEvidencePackage([t1, t2, t3noevidence], null)
check('latest completed without result never falls back', dfltNoFallback === null, JSON.stringify(dfltNoFallback))

// 3. 显式选择已完成任务只绑定该任务
const explicit = selectEvidencePackage([t1, t2], 12)
check('explicit binds that task', explicit?.taskId === 12 && explicit?.packageId === 1, JSON.stringify(explicit))

// 4a. 显式选择未完成任务 → null，不回退到 T1/T2
const unfinished = selectEvidencePackage([t1, t2, detail(13, [])], 13)
check('explicit unfinished never falls back', unfinished === null, JSON.stringify(unfinished))

// 4a2. 显式选择未完成但携带有证据行的任务 → 仍为 null（只有已完成
// 才可绑定展示/导出；状态门禁在解析层，不依赖上游是否过滤）。
const runningWithEvidence = { ...detail(14, [7]), status: 'running', attempts: [] }
const blocked = selectEvidencePackage([t1, t2, runningWithEvidence], 14)
check('explicit unfinished with evidence never binds', blocked === null, JSON.stringify(blocked))

// 4b. 显式选择未知任务 → null
const unknown = selectEvidencePackage([t1, t2], 999)
check('explicit unknown never falls back', unknown === null, JSON.stringify(unknown))

// 5. 空详情 → null
check('empty details null', selectEvidencePackage([], null) === null)

// 6. 无已完成任务 → 默认 null
check('no completed null', selectEvidencePackage([detail(13, [])], null) === null)

// 7. selectDefaultTaskId：12 条列表项（超旧 10 窗口）确定完成最晚者。
// 形状与后端列表项一致（id + completed_at；调用方已按类型过滤）。
const li = (id, completedAt) => ({ id, completed_at: completedAt })
const twelve = []
for (let i = 1; i <= 11; i += 1) {
  twelve.push(li(i, `2026-10-14T10:${String(i).padStart(2, '0')}:00+00:00`))
}
// id=12 无完成时刻（排最末）；顺序打乱，仍应选中完成最晚的 id=11。
twelve.push(li(12, null))
const twelveShuffled = [twelve[5], twelve[0], twelve[11], twelve[3], twelve[8], twelve[1], twelve[10], twelve[4], twelve[7], twelve[2], twelve[9], twelve[6]]
const winnerTwelve = selectDefaultTaskId(twelveShuffled)
check('twelve items pick latest completed', winnerTwelve === 11, JSON.stringify(winnerTwelve))

// 7b. 早创建晚完成胜出（与 id 顺序无关）：id=1 完成时刻最晚。
const lateOld = [li(2, '2026-10-14T10:08:00+00:00'), li(1, '2026-10-14T10:20:00+00:00')]
check('older created but later completed wins', selectDefaultTaskId(lateOld) === 1)
// 7c. 时刻持平按 id 倒序，缺席最末。
check('tie breaks by higher id', selectDefaultTaskId([li(3, '2026-10-14T10:00:00+00:00'), li(9, '2026-10-14T10:00:00+00:00')]) === 9)
check('null times sort last', selectDefaultTaskId([li(4, null), li(5, '2026-10-14T09:00:00+00:00')]) === 5)
check('all null picks highest id', selectDefaultTaskId([li(4, null), li(9, null)]) === 9)
check('empty items null', selectDefaultTaskId([]) === null)

if (failures > 0) {
  console.error(`${failures} check(s) failed`)
  process.exit(1)
}
console.log('all task-selection checks passed')
