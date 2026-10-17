/**
 * 数据集读取映射定向测试：按后端 _version_dict 权威形态消费，不补猜测值。
 *
 * 覆盖 `src/api/client.ts` 经公开方法的读取映射：
 * 1. versions() 对真实形态版本保留全部业务值（id/版本/时域/分辨率/偏移/
 *    字段类型与单位与中文说明/units/质量率/许可/创建人）；
 * 2. 空 versions 列表合法为空，不抛错；
 * 3. 失败请求可见抛错，不回退空数据；
 * 4. list() 容忍 latest_version 缺席（无版本数据集的合法 null）；
 * 5. list() 的真空 {datasets: []} 合法为空；
 * 6. 缺 versions 具名键的畸形信封可见抛错，不回退空列表。
 * 另覆盖 integer/decimal 字段类型透传（features/datasets 纯映射）。
 * fetch 用桩代替（node22 原生 Response），业务值全为桩内真实形态内容。
 *
 * 运行（Docker，通过 esbuild 打包执行）：
 *   docker run --rm -v "$PWD/frontend:/workspace:ro" pies014b3-fe-build sh -c '
 *     /app/node_modules/.bin/esbuild /workspace/tests/dataset-version-mapping.test.mjs --bundle --platform=node \
 *       --format=esm --outfile=/tmp/dataset-version-mapping.mjs &&
 *     node /tmp/dataset-version-mapping.mjs'
 */

import { listDatasets, listVersions } from '../src/features/datasets/index.ts'

let failures = 0
function check(name, cond, detail = '') {
  if (!cond) failures++
  console.log(`[${cond ? 'PASS' : 'FAIL'}] ${name}${detail ? ` — ${detail}` : ''}`)
}

// 与后端 api/datasets._version_dict 同形的真实形态版本（含 v1 质量报告）。
function fullVersion() {
  return {
    id: 11,
    dataset_id: 3,
    version_no: 2,
    timeline: 'hourly',
    resolution: '1h',
    fixed_utc_offset_minutes: 480,
    fields: {
      e_load: { type: 'float', unit: 'kWh', description_zh: '电负荷', description_en: 'Electric load' },
      // 后端发布的 step 类型字面量即 integer：直接透传，不回退 float。
      step: { type: 'integer', unit: 'step', description_zh: '行序号', description_en: 'Row step index' },
      // 价格序列 price 列类型字面量即 decimal：直接透传，不归一化。
      price: { type: 'decimal', unit: 'CNY/kWh', description_zh: '能源价格', description_en: 'Energy price' },
    },
    units: { e_load: 'kWh', step: 'step', price: 'CNY/kWh' },
    quality_report: {
      tool: 'iesplan.dataset',
      generated_at: '2026-10-15T00:00:00+00:00',
      resolution: '1h',
      timeline: 'hourly',
      row_count: 8760,
      fixed_utc_offset_minutes: 480,
      checks: {
        row_count: { expected: 8760, actual: 8760, ok: true },
        timestamps: { ok: true, codes: [] },
        missing_values: { ok: false, total: 12, by_field: { e_load: 12 } },
        ranges: { ok: true, total: 0, by_field: {} },
        units: { ok: true },
      },
      diagnostics: [],
      has_blocking_errors: false,
    },
    provenance: { source_category: 'user_upload' },
    license: null,
    created_by: 5,
    created_at: '2026-10-15T00:00:00+00:00',
    created_reason: 'upload',
  }
}

function datasetRow(id, latestVersion) {
  return {
    dataset: {
      id,
      project_id: 7,
      name: `ds-${id}`,
      description: null,
      status: 'active',
      default_license: null,
      created_by: 5,
      created_at: '2026-10-15T00:00:00+00:00',
      updated_at: '2026-10-15T00:00:00+00:00',
    },
    latest_version: latestVersion,
  }
}

globalThis.fetch = async (url, init = {}) => {
  const text = String(url)
  const method = String(init.method ?? 'GET').toUpperCase()
  if (method === 'GET' && text.endsWith('/projects/7/datasets')) {
    return new Response(
      JSON.stringify({ datasets: [datasetRow(3, null), datasetRow(4, fullVersion())] }),
      { status: 200 },
    )
  }
  if (method === 'GET' && text.includes('/projects/7/datasets/3')) {
    return new Response(
      JSON.stringify({ dataset: datasetRow(3, null).dataset, versions: [fullVersion()] }),
      { status: 200 },
    )
  }
  if (method === 'GET' && text.includes('/projects/7/datasets/9')) {
    return new Response(
      JSON.stringify({ dataset: datasetRow(9, null).dataset, versions: [] }),
      { status: 200 },
    )
  }
  if (method === 'GET' && text.includes('/projects/7/datasets/5')) {
    return new Response(JSON.stringify({ error: { code: 'API-500-001' } }), { status: 500 })
  }
  if (method === 'GET' && text.includes('/projects/7/datasets/6')) {
    // 畸形体：缺 versions 具名键，不得静默为空列表。
    return new Response(JSON.stringify({ dataset: datasetRow(6, null).dataset }), { status: 200 })
  }
  if (method === 'GET' && text.endsWith('/projects/8/datasets')) {
    return new Response(JSON.stringify({ datasets: [] }), { status: 200 })
  }
  return new Response('not found', { status: 404 })
}

async function expectApiError(promise) {
  try {
    await promise
  } catch (err) {
    return err
  }
  return null
}

// 1. 真实形态版本：全部业务值保留，无猜测填充。
{
  const versions = await listVersions(7, 3)
  check('版本数', versions.length === 1, `n=${versions.length}`)
  const v = versions[0]
  check('身份三元组', v.id === 11 && v.dataset_id === 3 && v.version_no === 2)
  check('时域/分辨率/偏移', v.timeline === 'hourly' && v.resolution === '1h' && v.fixed_utc_offset_minutes === 480)
  check('字段类型透传', v.fields.e_load.type === 'float' && v.fields.step.type === 'integer')
  check('decimal 类型保留', v.fields.price.type === 'decimal' && v.units.price === 'CNY/kWh')
  check('字段单位说明', v.fields.e_load.unit === 'kWh' && v.fields.e_load.description === '电负荷')
  check('单位表', v.units.e_load === 'kWh' && v.units.step === 'step')
  check('缺失率实算', v.quality_report?.missing_rate === 12 / 8760, `r=${v.quality_report?.missing_rate}`)
  check('越界率实算', v.quality_report?.outlier_rate === 0)
  check('许可缺席保留 null', v.license === null)
  check('创建人/事由', v.created_by === 5 && v.created_reason === 'upload')
}

// 2. 空版本列表合法为空。
{
  const versions = await listVersions(7, 9)
  check('空版本列表', Array.isArray(versions) && versions.length === 0)
}

// 3. 失败请求可见抛错，不回退空数据。
{
  const err = await expectApiError(listVersions(7, 5))
  check('失败抛错', err !== null)
  check('服务端状态保留', err?.status === 500, `status=${err?.status}`)
}

// 4. 列表容忍 latest_version 缺席；空列表合法。
{
  const page = await listDatasets({ project_id: 7 })
  check('列表行数', page.items.length === 2, `n=${page.items.length}`)
  check('行身份', page.items[0].id === 3 && page.items[1].id === 4)
  // 首行 latest_version 为 null（无版本数据集）仍是合法行，不阻断列表。
}

// 5. 真空数据集列表：{datasets: []} 合法为空。
{
  const page = await listDatasets({ project_id: 8 })
  check('空数据集列表', Array.isArray(page.items) && page.items.length === 0)
}

// 6. 畸形信封（缺 versions 具名键）必须可见抛错，不得回退空列表。
{
  const err = await expectApiError(listVersions(7, 6))
  check('畸形抛错', err !== null)
}

if (failures > 0) {
  console.log(`${failures} 项失败`)
  process.exitCode = 1
} else {
  console.log('全部通过')
}
