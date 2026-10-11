/**
 * 项目版本 feature mapper 纯函数单元测试(S1)。
 *
 * 覆盖: 名称去空白、空名阻止发送、描述空串归一 null、reason 恒为
 * manual_save(调用方无法传入伪装原因)。
 *
 * 纯 Node 环境(无浏览器/无 React): 用 esbuild 把 mappers.ts 打包为 CJS 后
 * 直接调用(与 tests/modeling_mappers.test.mjs 同一模式)。
 *
 * 运行(在 frontend 目录, Docker 内):
 *   npx esbuild src/features/project-versions/mappers.ts --bundle --format=cjs \
 *     --outfile=/tmp/project-versions-mappers.cjs
 *   node tests/project_versions_mapper.test.mjs
 */

import { createRequire } from 'node:module'

const require = createRequire(import.meta.url)

let failures = 0
function check(name, cond, detail = '') {
  const ok = cond ? 'PASS' : 'FAIL'
  if (!cond) failures++
  console.log(`[${ok}] ${name}${detail ? ` — ${detail}` : ''}`)
}

const md = require('/tmp/project-versions-mappers.cjs')

// 正常: 去空白 + 描述保留 + reason 恒定
const dto = md.buildCreateRequest({ name: '  v1 上线  ', description: ' 首版 ' })
check('name trimmed', dto.name === 'v1 上线', JSON.stringify(dto.name))
check('description trimmed', dto.description === '首版', JSON.stringify(dto.description))
check('reason pinned manual_save', dto.reason === 'manual_save', JSON.stringify(dto.reason))

// 描述空串归一 null(与后端 Optional 一致)
const dto2 = md.buildCreateRequest({ name: 'v2', description: '   ' })
check('blank description -> null', dto2.description === null, JSON.stringify(dto2.description))

// 空名阻止发送(抛 VersionFormError, 不发请求)
let threw = null
try {
  md.buildCreateRequest({ name: '   ', description: '' })
} catch (err) {
  threw = err
}
check('blank name throws', threw !== null && threw.name === 'VersionFormError')

if (failures > 0) {
  console.error(`FAILED: ${failures} check(s)`)
  process.exit(1)
}
console.log('All project-versions mapper checks passed.')
