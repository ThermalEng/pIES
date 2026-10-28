/**
 * 错误信封定向测试：标准 {error} 透传；裸 {diagnostics} 走通用可见错误。
 *
 * 覆盖共享 toApiError 经公开方法（任务重试走 `src/features/tasks`
 * 公开入口，校验仍走聚合器模型家族）的行为：
 * 1. 真实形态的标准错误信封保留 status/code/message_key 与嵌套 diagnostics；
 * 2. 不再识别的裸 {diagnostics} 错误体不再伪造 VALIDATION-FAILED，走可见通用错误；
 * 3. 成功响应的 diagnostics 兄弟字段（validation/run 200）仍可用。
 * fetch 用桩代替；同时覆盖共享错误文案与 HTTP 状态参数插值。
 *
 * 运行（Docker，从仓库根执行；结束后删除自建镜像）：
 *   docker build --target build -f frontend/Dockerfile -t pies-error-envelope-build .
 *   docker run --rm pies-error-envelope-build sh -c '
 *     ./node_modules/.bin/esbuild tests/error-envelope.test.mjs --bundle --platform=node \
 *       --format=esm --define:import.meta.env.DEV=false --outfile=/tmp/error-envelope.mjs &&
 *     node /tmp/error-envelope.mjs'
 */

import { validateModel } from '../src/features/modeling/index.ts'
import { retryTask } from '../src/features/tasks/index.ts'
import { errorMessage, translate } from '../src/i18n/index.ts'
import { ApiError } from '../src/types.ts'

let failures = 0
function check(name, cond, detail = '') {
  if (!cond) failures++
  console.log(`[${cond ? 'PASS' : 'FAIL'}] ${name}${detail ? ` — ${detail}` : ''}`)
}

function envelope() {
  return {
    error: {
      code: 'TASK-STATE-001',
      message_key: 'ies.diag.task.state_conflict',
      severity: 'error',
      blocking: false,
      params: {
        task_id: 9,
        diagnostics: [{ code: 'D-1', message_key: 'ies.diag.inner', params: { n: 2 } }],
      },
      location: { object_type: 'task', object_id: 9 },
      fix_hint_key: '',
      ref_ids: ['r1'],
    },
  }
}

let mode = 'envelope'
globalThis.fetch = async (url, init = {}) => {
  const text = String(url)
  const method = String(init.method ?? 'GET').toUpperCase()
  if (method === 'POST' && text.includes('/tasks/9/retry')) {
    if (mode === 'bare') {
      return new Response(JSON.stringify({ diagnostics: [{ code: 'D-9' }] }), { status: 400 })
    }
    return new Response(JSON.stringify(envelope()), { status: 409 })
  }
  if (method === 'POST' && text.includes('/validation/run')) {
    return new Response(
      JSON.stringify({
        report: {
          blocks_submit: false,
          diagnostics: [{ code: 'V-1', message_key: 'ies.diag.v', params: {} }],
        },
      }),
      { status: 200 },
    )
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

// 1. 标准信封：status/code/message_key 与嵌套 diagnostics 全部保留。
{
  const err = await expectApiError(retryTask(7, 9))
  check('标准信封抛错', err !== null)
  check('状态码保留', err?.status === 409, `status=${err?.status}`)
  check('错误码保留', err?.code === 'TASK-STATE-001', `code=${err?.code}`)
  check('文案键保留', err?.message_key === 'ies.diag.task.state_conflict')
  check('嵌套诊断透传', err?.params?.diagnostics?.[0]?.code === 'D-1')
  check('定位透传', err?.location?.object_type === 'task')
}

// 2. 裸 {diagnostics}：不再伪造 VALIDATION-FAILED，走可见通用错误。
{
  mode = 'bare'
  const err = await expectApiError(retryTask(7, 9))
  check('裸错误体抛错', err !== null)
  check('不伪造 VALIDATION-FAILED', err?.code !== 'VALIDATION-FAILED', `code=${err?.code}`)
  check('通用错误码', err?.code === 'API-UNKNOWN', `code=${err?.code}`)
  check('通用文案键', err?.message_key === 'ies.error.unknown', `key=${err?.message_key}`)
  mode = 'envelope'
}

// 3. 成功响应的 diagnostics 仍可用。
{
  const result = await validateModel(7)
  check('校验结论可用', result.valid === true)
  check('成功诊断可用', result.diagnostics?.[0]?.code === 'V-1')
}

// 4. 错误展示与诊断渲染定向测试（有详情、聚合诊断、未知消息键且无详情）。
{
  // 4.1 有公开详情 (params.detail)
  const errDetail = new ApiError(400, {
    code: 'PROJ-FIN-001',
    message_key: 'ies.diag.param.invalid',
    severity: 'error',
    blocking: false,
    params: { detail: 'finance_types.pv.upfront_capex 必填且大于 0' },
  })
  check('有详情时展示公开具体详情', errorMessage(errDetail) === 'finance_types.pv.upfront_capex 必填且大于 0')

  // 4.2 聚合诊断 (params.diagnostics 多项，必须完整保留所有项，不丢失后续项)
  const errMultiDiag = new ApiError(422, {
    code: 'ASM-CHECK-FAILED',
    message_key: 'ies.diag.asm.check_failed',
    severity: 'error',
    blocking: true,
    params: {
      diagnostics: [
        { code: 'ASM-001', detail: '端口电压不匹配' },
        { code: 'ASM-002', detail: '母线频率越界' },
      ],
    },
  })
  const multiText = errorMessage(errMultiDiag)
  check('聚合诊断完整保留所有项', multiText.includes('端口电压不匹配') && multiText.includes('母线频率越界'), multiText)

  // 4.3 未知消息键且无详情：显示本地化通用提示及错误码，不显示原始键
  const errUnknown = new ApiError(400, {
    code: 'PLAN-VAR-999',
    message_key: 'custom.unregistered.error_key',
    severity: 'error',
    blocking: false,
    params: {},
  })
  const unknownText = errorMessage(errUnknown)
  check('未知消息键显示通用提示与错误码', unknownText === '操作失败 (PLAN-VAR-999)')
  check('不暴露原始消息键', !unknownText.includes('custom.unregistered.error_key'))

  // 4.4 模板含未替换占位符且无详情：不返回未替换占位符
  const errPlaceholder = new ApiError(400, {
    code: 'PARAM-001',
    message_key: 'ies.diag.param.invalid',
    severity: 'error',
    blocking: false,
    params: {},
  })
  const placeholderText = errorMessage(errPlaceholder)
  check('未替换占位符兜底通用提示', placeholderText === '操作失败 (PARAM-001)')
  check('不暴露占位符花括号', !placeholderText.includes('{detail}'))

  // 4.5 普通非 ApiError 异常可见且不伪造文案键
  check('普通异常可见且不伪造文案键', errorMessage(new Error('local failure')) === '未知错误:Error: local failure')
  for (const locale of ['zh', 'en']) {
    const message = translate('ies.error.http_exception', { status_code: 405 }, locale)
    check(`${locale} HTTP 状态码插值`, message.includes('405') && !message.includes('{status'))
  }
}

if (failures > 0) {
  console.log(`${failures} 项失败`)
  process.exitCode = 1
} else {
  console.log('全部通过')
}
