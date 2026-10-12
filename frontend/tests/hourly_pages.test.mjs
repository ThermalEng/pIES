/**
 * R5/R6 逐时分页拼接适配层定向测试。
 *
 * 覆盖 `src/api/hourlyPages.ts collectHourlyValues`:
 * 单页(next_start=null 一次返回)、两页拼接、超过后端 MAX_HOURLY_LIMIT=50000
 * 的多页(桩按 50000 截断模拟后端语义)、rows=0 空序列。
 * 断言每次请求的 start/limit 参数与拼接后的真实关键值(非字段存在性)。
 *
 * 运行(Docker, 复用已缓存的 playwright 镜像, 不新增镜像/依赖):
 *   docker run --rm -v "$PWD/frontend:/app:ro" --entrypoint node \
 *     mcr.microsoft.com/playwright:v1.54.2-noble /app/tests/hourly_pages.test.mjs
 * (node22 自带类型剥离, helper 零导入, 直接 import .ts 源码。)
 */

import { collectHourlyValues } from '../src/api/hourlyPages.ts'

let failures = 0
function check(name, cond, detail = '') {
  if (!cond) failures++
  console.log(`[${cond ? 'PASS' : 'FAIL'}] ${name}${detail ? ` — ${detail}` : ''}`)
}
function eq(a, b) {
  return a.length === b.length && a.every((v, i) => Object.is(v, b[i]))
}

/** 按后端 read_hourly 语义的桩: limit 截断到 PAGE_MAX, next_start 为空表示末页。 */
function makeBackend(all, PAGE_MAX = 50000) {
  const calls = []
  return {
    calls,
    async readPage(start, limit) {
      calls.push({ start, limit })
      const eff = Math.max(1, Math.min(limit, PAGE_MAX))
      const end = Math.min(start + eff, all.length)
      return { values: all.slice(start, end), next_start: end < all.length ? end : null }
    },
  }
}

// 1. 单页: 一次请求即 next_start=null
{
  const vals = [10.5, -3, 0, 700.25, 1e-4]
  const be = makeBackend(vals)
  const out = await collectHourlyValues((s, l) => be.readPage(s, l), vals.length)
  check('单页一次请求', be.calls.length === 1, JSON.stringify(be.calls))
  check('单页请求参数', be.calls[0].start === 0 && be.calls[0].limit === 5, JSON.stringify(be.calls[0]))
  check('单页拼接真实值', eq(out, vals))
}

// 2. 两页拼接(小截断模拟, 真实关键值含正/零/负)
{
  const vals = [1.5, -2.25, 0, 3.75, -4, 5.125, 6, -7.5]
  const be = makeBackend(vals, 5)
  const out = await collectHourlyValues((s, l) => be.readPage(s, l), vals.length)
  check('两页请求两次', be.calls.length === 2, JSON.stringify(be.calls))
  check('两页请求参数', be.calls[0].start === 0 && be.calls[0].limit === 8 && be.calls[1].start === 5 && be.calls[1].limit === 3, JSON.stringify(be.calls))
  check('两页拼接真实值', eq(out, vals))
}

// 3. 超 50000: 120000 点按后端截断分 50000/50000/20000 三页
{
  const vals = Array.from({ length: 120000 }, (_, i) => (i % 2 === 0 ? i * 0.5 : -i * 0.25))
  const be = makeBackend(vals)
  const out = await collectHourlyValues((s, l) => be.readPage(s, l), vals.length)
  check('超限请求三次', be.calls.length === 3, JSON.stringify(be.calls.map((c) => c.limit)))
  check('超限分页参数', be.calls[0].start === 0 && be.calls[1].start === 50000 && be.calls[2].start === 100000 && be.calls[2].limit === 20000, JSON.stringify(be.calls))
  check('超限请求原文limit不预截', be.calls[0].limit === 120000 && be.calls[1].limit === 70000, JSON.stringify(be.calls.map((c) => c.limit)))
  check('超限拼接全量真实值', out.length === 120000 && eq(out, vals))
}

// 4. rows=0 空序列: 单次请求后结束, 返回空数组
{
  const be = makeBackend([])
  const out = await collectHourlyValues((s, l) => be.readPage(s, l), 0)
  check('空序列单次请求', be.calls.length === 1 && be.calls[0].start === 0 && be.calls[0].limit === 1, JSON.stringify(be.calls))
  check('空序列返回空数组', Array.isArray(out) && out.length === 0)
}

if (failures > 0) {
  console.error(`${failures} check(s) FAILED`)
  process.exitCode = 1
} else {
  console.log('all hourly pagination checks passed')
}
