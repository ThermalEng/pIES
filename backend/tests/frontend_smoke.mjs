/**
 * 前端契约适配层端到端验证(Node 环境):
 * 1) esbuild 将 src/api/client.ts 打包为 CJS(真实适配代码);
 * 2) 注入 window/localStorage/fetch(Cookie 会话) 桩;
 * 3) 通过 nginx 代理(web:8080)连接真实后端, 逐个调用页面实际使用的方法,
 *    校验返回形状与页面消费方式(items/project/versions/config/model 等)一致。
 *
 * 运行(在 docker node 容器内, 挂载 frontend 目录):
 *   npx esbuild src/api/client.ts --bundle --format=cjs --outfile=/tmp/ies-client.cjs
 *   node /tmp/frontend_smoke.mjs
 */
import { createRequire } from 'node:module'
const require = createRequire(import.meta.url)

const BASE = process.env.SMOKE_BASE || 'http://localhost:8080/api'

// ---- window / localStorage 桩 ----
const store = {}
globalThis.window = {
  localStorage: {
    getItem: (k) => store[k] ?? null,
    setItem: (k, v) => { store[k] = String(v) },
    removeItem: (k) => { delete store[k] },
  },
  location: { pathname: '/' },
  addEventListener: () => {},
  removeEventListener: () => {},
}

// ---- Cookie 会话桩(模拟浏览器 credentials: 'include'; 相对路径按 BASE 解析) ----
const nativeFetch = globalThis.fetch // 先保存原生 fetch(覆盖前)
let cookieHeader = ''
globalThis.fetch = async (url, opts = {}) => {
  const full = new URL(String(url), BASE).toString()
  const headers = new Headers(opts.headers || {})
  if (cookieHeader) headers.set('Cookie', cookieHeader)
  const res = await nativeFetch(full, { ...opts, headers })
  const setCookie = res.headers.get('set-cookie')
  if (setCookie) {
    const m = /^([^=;]+)=([^;]*)/.exec(setCookie)
    if (m) cookieHeader = `${m[1]}=${m[2]}`
  }
  return res
}

const { api } = require('/tmp/ies-client.cjs')

let failures = 0
function check(name, cond, detail = '') {
  const ok = cond ? 'PASS' : 'FAIL'
  console.log(`${ok}  ${name}  ${detail}`)
  if (!cond) failures += 1
}

// ---- 流程 ----
async function main() {
  // 0) 登录页公开设置(无需认证): 注册开关 / SSO 入口
  const pub = await api.auth.publicSettings()
  check('auth.publicSettings → {registration_enabled, sso_enabled}', 'registration_enabled' in pub && 'sso_enabled' in pub, JSON.stringify(pub))

  // 1) 登录(初始密码可能已改)
  let login
  try {
    login = await api.auth.login({ username: 'admin', password: 'iesplan-admin-initial' })
  } catch {
    try {
      login = await api.auth.login({ username: 'admin', password: 'Iesplan-Admin#2026e2e' })
    } catch {
      login = await api.auth.login({ username: 'admin', password: 'AdminTest123' })
    }
  }
  check('auth.login', !!login.token && !!login.user, JSON.stringify({ role: login.user.role, fpc: login.user.force_password_change }))
  if (login.needs_takeover_confirm) {
    const tk = await api.auth.confirmTakeover({ token: login.token })
    check('auth.confirmTakeover', !!tk.token)
  }

  // 1.5) 管理员安全设置(注册开关持久化)
  try {
    const sec = await api.admin.getSecuritySettings()
    check('admin.getSecuritySettings → registration_enabled', typeof sec.registration_enabled === 'boolean', JSON.stringify(sec))
  } catch {
    check('admin.getSecuritySettings', false, '跳过(非管理员或无权限)')
  }

  // 2) auth.me → User(页面只读 id/username)
  const me = await api.auth.me()
  check('auth.me → User', me.id > 0 && typeof me.username === 'string', `id=${me.id} name=${me.username}`)

  // 3) projects.list → PageResult{items}(修复点: {projects} → items)
  const page = await api.projects.list({ limit: 50 })
  check('projects.list → items[]', Array.isArray(page.items) && page.next_cursor === null, `n=${page.items.length}`)
  const pid = page.items[0]?.id
  check('projects.list item 字段(id/name/role)', pid !== undefined && 'role' in page.items[0], JSON.stringify({ role: page.items[0]?.role, name: page.items[0]?.name }))

  // 4) projects.get → Project(解包 {project, draft, versions, my_role})
  const proj = await api.projects.get(pid)
  check('projects.get → Project(role)', proj.id === pid && 'currency' in proj && proj.role !== undefined, `name=${proj.name} role=${proj.role}`)

  // 5) projects.versions → ProjectVersion[](解包 {versions})
  const versions = await api.projects.versions(pid)
  check('projects.versions → ProjectVersion[]', Array.isArray(versions), `n=${versions.length}`)

  // 6) config.get → CalcConfig(parameters/irr_floor/algorithm 映射)
  const cfg = await api.config.get(pid)
  check('config.get → CalcConfig', cfg.project_id === pid && 'params' in cfg && Array.isArray(cfg.variables) && Array.isArray(cfg.constraints) && Array.isArray(cfg.objectives), `algo=${cfg.algorithm} irr=${cfg.min_irr} status=${cfg.status} version=${cfg.version}`)

  // 7) config.algorithms → {items:[{name,label,description_key}]}
  const algos = await api.config.algorithms()
  check('config.algorithms → items', Array.isArray(algos.items) && algos.items.length > 0 && 'description_key' in algos.items[0], `n=${algos.items.length} first=${algos.items[0]?.name}`)

  // 8) config.default → CalcConfigInput
  const def = await api.config.default(pid)
  check('config.default → CalcConfigInput', 'params' in def && 'algorithm' in def, `algo=${def.algorithm}`)

  // 9) config.validate → ValidationResult
  const vres = await api.config.validate(pid)
  check('config.validate → {valid, diagnostics}', typeof vres.valid === 'boolean' && Array.isArray(vres.diagnostics))

  // 10) model.getGraph → GraphModel(graph/devices/ports/connections)
  const graph = await api.model.getGraph(pid)
  check('model.getGraph → GraphModel', Array.isArray(graph.devices) && Array.isArray(graph.ports) && Array.isArray(graph.connections) && 'graph' in graph, `devices=${graph.devices.length} ports=${graph.ports.length} conns=${graph.connections.length}`)

  // 11) model.deviceTypes → DeviceTypeSpec[](items 解包 + 参数规格映射)
  const specs = await api.model.deviceTypes()
  check('model.deviceTypes → DeviceTypeSpec[]', Array.isArray(specs) && specs.length > 0 && 'parameters' in specs[0], `n=${specs.length} first=${specs[0]?.type_id}`)

  // 12) model.validate → ValidationResult
  const mval = await api.model.validate(pid)
  check('model.validate → {valid, diagnostics}', typeof mval.valid === 'boolean' && Array.isArray(mval.diagnostics))

  // 13) tasks.list → PageResult<Task>
  const tpage = await api.tasks.list({ project_id: pid, limit: 5 })
  check('tasks.list → {items,next_cursor}', Array.isArray(tpage.items), `n=${tpage.items.length}`)

  // 14) datasets.list → PageResult<Dataset>
  const dpage = await api.datasets.list({ project_id: pid, limit: 10 })
  check('datasets.list → {items}', Array.isArray(dpage.items), `n=${dpage.items.length}`)

  // 15) validation.run → ValidationResult
  const vr = await api.validation.run(pid)
  check('validation.run → {valid, diagnostics}', typeof vr.valid === 'boolean' && Array.isArray(vr.diagnostics))

  // 16) admin.audit / health / users
  const audit = await api.admin.audit({ limit: 5 })
  check('admin.audit → {items,next_cursor}', Array.isArray(audit.items), `n=${audit.items.length}`)
  const health = await api.admin.health()
  check('admin.health → HealthStatus', health.status !== undefined && 'checks' in health, JSON.stringify(health.status))
  const users = await api.admin.users({ limit: 20 })
  check('admin.users → {items: AdminUserRow}', Array.isArray(users.items) && 'roles' in (users.items[0] ?? {}), `n=${users.items.length}`)

  // 17) updateDraft(前端命令 → 后端命令清单翻译, 用真实图内容)
  const sem = {
    devices: graph.devices.map((d) => ({
      id: String(d.id), device_type: d.device_type, kind: d.kind, name: d.name,
      params: d.params ?? {}, model_fidelity: d.model_fidelity, status: 'active',
    })),
    connections: graph.connections.map((c) => {
      const fp = graph.ports.find((p) => p.id === c.from_port_id)
      const tp = graph.ports.find((p) => p.id === c.to_port_id)
      const carrier = (pt) => ({ electric: 'electric', thermal: 'heat', cooling: 'cool', fuel: 'gas' })[pt] ?? pt
      return {
        id: String(c.id),
        from_device_id: String(fp?.device_id ?? 0),
        from_port: fp ? { carrier: carrier(fp.port_type), direction: fp.direction === 'in' ? 'in' : 'out' } : undefined,
        to_device_id: String(tp?.device_id ?? 0),
        to_port: tp ? { carrier: carrier(tp.port_type), direction: tp.direction === 'in' ? 'in' : 'out' } : undefined,
        conn_type: c.conn_type, loss_rate: c.loss_rate,
      }
    }),
  }
  const saved = await api.projects.updateDraft(pid, { command: 'model.set_graph', revision: null, graph: sem })
  check('updateDraft → {revision}', typeof saved.revision === 'number' && saved.revision > 0, `revision=${saved.revision}`)

  console.log()
  if (failures > 0) {
    console.log(`共 ${failures} 项失败`)
    process.exit(1)
  }
  console.log('前端适配层端到端验证全部通过')
}

main().catch((err) => {
  const status = err && typeof err === 'object' ? err.status : undefined
  console.error('SMOKE CRASH:', status !== undefined ? `ApiError ${status} ${err.message_key ?? err.message}` : err)
  process.exit(1)
})
