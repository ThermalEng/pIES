import assert from 'node:assert/strict'
import { mock } from 'node:test'
import { startSessionActivity } from '../src/features/auth/sessionActivity.ts'

const listeners = new Map()
globalThis.window = {
  addEventListener: (type, listener) => listeners.set(type, listener),
  removeEventListener: (type, listener) => {
    if (listeners.get(type) === listener) listeners.delete(type)
  },
}
globalThis.document = { visibilityState: 'visible' }
const emit = (type, isTrusted = true) => listeners.get(type)?.({ isTrusted })
const flush = () => new Promise(resolve => setImmediate(resolve))
mock.timers.enable({ apis: ['Date', 'setTimeout'], now: 1_000_000 })

try {
  const calls = []
  const errors = []
  const stop = startSessionActivity(async signal => { calls.push(signal) }, error => errors.push(error))
  mock.timers.tick(8 * 60 * 60_000)
  assert.equal(calls.length, 0, '挂载与后台计时本身不续期')
  emit('pointermove', false)
  emit('input', false)
  document.visibilityState = 'hidden'
  emit('keydown')
  assert.equal(calls.length, 0, '合成事件和隐藏页面不续期')
  document.visibilityState = 'visible'

  emit('pointerdown')
  await flush()
  assert.equal(calls.length, 1, '真实操作立即续期')
  mock.timers.tick(1_000)
  for (const type of ['pointermove', 'keydown', 'input', 'wheel']) emit(type)
  await flush()
  assert.equal(calls.length, 1, '密集画布、键盘、输入、滚动合并请求')
  mock.timers.tick(29_000)
  await flush()
  assert.equal(calls.length, 2, '尾次操作未被节流丢弃')
  mock.timers.tick(8 * 60 * 60_000)
  await flush()
  assert.equal(calls.length, 2, '操作停止后没有周期性保活')

  emit('keydown')
  await flush()
  assert.equal(calls.length, 3, '键盘编辑即使没有业务提交也续期')
  mock.timers.tick(1_000)
  emit('wheel')
  document.visibilityState = 'hidden'
  mock.timers.tick(30_000)
  await flush()
  assert.equal(calls.length, 3, '隐藏页面不补发旧活动')
  document.visibilityState = 'visible'
  emit('input')
  await flush()
  assert.equal(calls.length, 4)
  mock.timers.tick(1_000)
  emit('input')
  stop()
  mock.timers.tick(60_000)
  await flush()
  assert.equal(calls.length, 4)
  assert.equal(listeners.size, 0, '卸载移除全部操作监听')
  assert.ok(calls.every(signal => signal.aborted), '卸载取消续期请求')
  assert.ok(errors.every(error => error === null))

  const failure = new Error('offline')
  let attempts = 0
  const retryErrors = []
  const retryStop = startSessionActivity(async () => {
    if (++attempts === 1) throw failure
  }, error => retryErrors.push(error))
  emit('input')
  await flush()
  assert.deepEqual(retryErrors, [failure], '续期失败可见，不冒充成功')
  mock.timers.tick(60_000)
  assert.equal(attempts, 1, '失败不自动循环保活')
  emit('input')
  await flush()
  assert.equal(attempts, 2, '后续真实操作可重试')
  assert.deepEqual(retryErrors, [failure, null])
  retryStop()

  let resolveRefresh
  let inFlightCalls = 0
  const slowStop = startSessionActivity(() => {
    inFlightCalls++
    return new Promise(resolve => { resolveRefresh = resolve })
  }, () => {})
  emit('pointerdown')
  emit('keydown')
  mock.timers.tick(60_000)
  assert.equal(inFlightCalls, 1, '慢请求不并发堆积')
  resolveRefresh()
  await flush()
  assert.equal(inFlightCalls, 1, '休眠或慢请求后的陈旧操作不续期')
  slowStop()
} finally {
  mock.timers.reset()
}
console.log('session activity: passed')
