/**
 * 上传文件头判定编排时序回归(B5 收口: DataPage inspectFile 异步竞态)。
 *
 * 覆盖(uploadHeadScan.ts, 与 DataPage 共用同一模块):
 * 1. 快速换文件时旧读取结果失效(世代守卫);
 * 2. 读取失败上报 ok:false(调用方展示可见错误, 不静默回退 generic);
 * 3. decideHeadMode 判定: step 自描述 / 通用 / 有 schema 无 resolution / 空头;
 * 4. 清除选择直接以 generic 结算。
 *
 * 纯 Node 环境(无浏览器/无 React): 用 esbuild 把 uploadHeadScan.ts 打包为
 * CJS 后直接调用(与 tests/help_markdown.test.mjs 同一模式)。
 *
 * 运行(在 frontend 目录, Docker 内):
 *   npx esbuild src/pages/uploadHeadScan.ts --bundle --format=cjs \
 *     --outfile=/tmp/upload-head-scan.cjs
 *   node tests/upload_head_scan.test.mjs
 */

import { createRequire } from 'node:module'

const require = createRequire(import.meta.url)
const mod = require('/tmp/upload-head-scan.cjs')

const { createHeadScan, decideHeadMode } = mod

let failures = 0
function check(name, cond) {
  if (cond) {
    console.log(`ok - ${name}`)
  } else {
    failures += 1
    console.error(`FAIL - ${name}`)
  }
}

const STEP_HEAD = [
  '# schema: ies.device-data',
  '# schema_version: 2.0.0',
  '# resolution: 1h',
  'step,electricity_demand',
  '0,48',
].join('\n')
const GENERIC_HEAD = 'timestamp,e_load\n2025-01-01 00:00,100\n'

// 3/4. 纯判定。
check('step 头判为 device-data', JSON.stringify(decideHeadMode(STEP_HEAD)) === JSON.stringify({ mode: 'device-data', resolution: '1h' }))
check('通用头判为 generic', JSON.stringify(decideHeadMode(GENERIC_HEAD)) === JSON.stringify({ mode: 'generic', resolution: null }))
check('有 schema 无 resolution 不判 step', decideHeadMode('# schema: ies.device-data\nstep,x\n').mode === 'generic')
check('空头判为 generic', decideHeadMode('').mode === 'generic')
check('首行非注释即停', decideHeadMode('timestamp,e_load\n# schema: ies.device-data\n').mode === 'generic')

// 1. 旧世代 settled 被丢弃(调用方按 isCurrent 过滤, 此处模拟组件接线)。
{
  const settled = []
  let releaseA
  const pendingA = new Promise((resolve) => { releaseA = resolve })
  const reader = (file) => (file.name === 'a.csv' ? pendingA : Promise.resolve(STEP_HEAD))
  const scanning = createHeadScan({ onSettled: (seq, outcome) => {
    if (scanning.isCurrent(seq)) settled.push([seq, outcome])
  } }, reader)
  const seqA = scanning.scan({ name: 'a.csv' })
  const seqB = scanning.scan({ name: 'b.csv' })
  check('世代号递增', seqB === seqA + 1)
  releaseA(GENERIC_HEAD)
  await pendingA
  await Promise.resolve()
  await new Promise((resolve) => { setTimeout(resolve, 0) })
  check('旧文件迟到结果被丢弃', settled.length === 1 && settled[0][0] === seqB)
  check('新文件 step 判定生效', settled[0][1].ok === true && settled[0][1].decision.mode === 'device-data')
}

// 2. 读取失败上报 ok:false。
{
  const settled = []
  const scanning = createHeadScan({ onSettled: (seq, outcome) => {
    if (scanning.isCurrent(seq)) settled.push(outcome)
  } }, () => Promise.reject(new Error('locked')))
  scanning.scan({ name: 'broken.csv' })
  await new Promise((resolve) => { setTimeout(resolve, 0) })
  check('读取失败显式上报', settled.length === 1 && settled[0].ok === false)
}

// 4. 清除选择直接 generic 结算。
{
  const settled = []
  const scanning = createHeadScan({ onSettled: (seq, outcome) => settled.push(outcome) })
  scanning.scan(null)
  check('清除选择即时 generic', settled.length === 1 && settled[0].ok === true && settled[0].decision.mode === 'generic')
}

// 对话框重置作废进行中判定。
{
  const settled = []
  const scanning = createHeadScan({ onSettled: (seq, outcome) => {
    if (scanning.isCurrent(seq)) settled.push(outcome)
  } }, () => Promise.resolve(STEP_HEAD))
  scanning.scan({ name: 'x.csv' })
  scanning.invalidate()
  await new Promise((resolve) => { setTimeout(resolve, 0) })
  check('重置后迟到 settled 被丢弃', settled.length === 0)
}

if (failures > 0) {
  console.error(`${failures} 个断言失败`)
  process.exit(1)
}
console.log('全部通过')
