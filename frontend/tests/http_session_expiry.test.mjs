import assert from 'node:assert/strict'
import { ApiError } from '../src/types.ts'
import { hasSession, request, requestBlob, setSession, setUnauthorizedHandler } from '../src/shared/http.ts'

const storage = new Map()
globalThis.window = { localStorage: {
  getItem: key => storage.get(key) ?? null,
  setItem: (key, value) => storage.set(key, value),
  removeItem: key => storage.delete(key),
} }
let redirects = 0
setUnauthorizedHandler(() => { redirects += 1 })

for (const send of [request, requestBlob]) {
  for (const message_key of ['ies.diag.auth.required', 'ies.diag.auth.session_invalid']) {
    setSession()
    const before = redirects
    globalThis.fetch = async () => new Response(JSON.stringify({ error: {
      code: message_key.endsWith('required') ? 'AUTH-REQ-001' : 'AUTH-SESS-001',
      message_key,
    } }), { status: 401 })
    await assert.rejects(send('/projects/1/tasks/1/result'), err => err instanceof ApiError && err.status === 401)
    assert.equal(hasSession(), false)
    assert.equal(redirects, before + 1)
  }
}

for (const [path, status, message_key, params] of [
  ['/auth/login', 401, 'ies.diag.auth.invalid_credentials', {}],
  ['/projects', 401, 'ies.diag.auth.session_invalid', { reason: 'takeover_pending' }],
  ['/projects/1/export/download', 401, 'ies.diag.package.download_token_invalid', {}],
  ['/projects/1', 403, 'ies.diag.auth.forbidden', {}],
  ['/projects/1', 502, 'ies.error.internal', {}],
]) {
  setSession()
  const before = redirects
  globalThis.fetch = async () => new Response(JSON.stringify({ error: {
    code: 'TEST', message_key, params,
  } }), { status })
  await assert.rejects(request(path), err => err instanceof ApiError && err.status === status)
  assert.equal(hasSession(), true, path)
  assert.equal(redirects, before, path)
}
setUnauthorizedHandler(null)
for (const send of [request, requestBlob]) {
  setSession()
  globalThis.fetch = async () => new Response('<html>502 Bad Gateway</html>', { status: 502 })
  await assert.rejects(send('/projects/1/result'), err =>
    err instanceof ApiError && err.status === 502 && err.message_key === 'ies.error.internal')
  assert.equal(hasSession(), true)
}
globalThis.fetch = async () => new Response('<html>not JSON</html>', { status: 200 })
await assert.rejects(request('/projects/1/result'), err =>
  err instanceof ApiError && err.message_key === 'ies.error.bad_json')
console.log('Session expiry: JSON/blob requests redirect for expired/missing sessions, not other failures.')
