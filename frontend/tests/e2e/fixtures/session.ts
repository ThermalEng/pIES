// QA-E2E-01 测试基础设施: 环境检查 / 会话管理 / 控制台与网络监控。
//
// 13.2 真实用户原则:
// - 业务动作全部通过 UI(点击/输入/上传/确认)完成;
// - API 仅用于本文件的环境自检(会话服务可用性), 以及 setup/cleanup
//   阶段的造数与清理, 与被测动作严格隔离(见 spec 文件顶部注释)。

import { test as base, expect, type ConsoleMessage, type Page } from '@playwright/test'

export { expect }

export interface SessionInfo {
  username: string
  password: string
  token: string
}

/** 登录会话: 通过 UI 登录(13.2 从应用公开入口打开页面并通过 UI 登录)。 */
export async function loginViaUI(page: Page, username: string, password: string): Promise<void> {
  await page.goto('/login')
  await expect(page.getByRole('heading', { name: /登录|Sign in/i })).toBeVisible()
  await page.getByLabel(/用户名|Username/i).fill(username)
  await page.getByLabel(/密码|Password/i).fill(password)
  await page.getByRole('button', { name: /登\s*录|Sign in/i }).click()
  // 登录成功后进入项目列表(路由 /)
  await expect(page).toHaveURL(/\/$/)
}

/**
 * 会话抽象: 默认页(项目列表)作为 tab 主入口; 供跨 tab 会话失效场景使用。
 * 所有 tab 共享同一浏览器 context(cookie/localStorage 一致)。
 */
export interface SessionContext {
  main: Page
  newTab: Page
  info: SessionInfo
  /** 断言页面上不存在未处理控制台错误与失败网络请求。
   *
   * 传入 expected 可声明“预期内 HTTP 噪声”(如空白项目读配置的 404):
   * 须按 method + path + status 精确限定。若提供 times, harness 会验证
   * 次数完全一致(供真正“一次性提交”等业务断言使用)；若省略 times,
   * 允许该确切响应出现零次或多次, 只核销观察到的同 URL 失败回显。
   * 额外路径/状态的请求不会被核销, 其余
   * 任何 console error / pageerror / 失败请求仍判失败。默认空数组 =
   * 历史零容忍行为。
   */
  assertNoErrors(expected?: ExpectedHttpNoise[]): void
  /**
   * 登记一次测试主动中止：须在 route 回调内部、route.abort() 紧前调用，
   * 按该请求的实际 method + URL 路径精确登记，一次登记只核销一条匹配
   * 的失败请求及其一条控制台回显。未登记的失败（含登记期内的其他
   * URL/方法）一律判失败；无跨 URL/跨方法的预算共用。
   */
  registerExpectedAbort(method: string, url: string): void
}

/** 预期内 HTTP 噪声声明；times 仅在次数有业务含义时填写。 */
export interface ExpectedHttpNoise {
  method: string
  /** URL 路径部分全等匹配(不含 origin/query, 如 `/api/projects/7/config`)。 */
  path: string
  status: number
  times?: number
}

/**
 * 建立会话(UI 登录)并安装控制台/网络监控。
 *
 * 适用于已完成后置改密(force_password_change=false)的用户
 * (场景 1 用初始密码走完改密流程后, 新密码即可直接登录)。
 *
 * @param username 用户名
 * @param password 当前有效密码
 */
export async function createSession(page: Page, username: string, password: string): Promise<SessionContext> {
  await loginViaUI(page, username, password)
  const info: SessionInfo = { username, password, token: '' }
  const session: SessionContext = {
    main: page,
    newTab: page,
    info,
    assertNoErrors: () => {},
    registerExpectedAbort: () => {},
  }
  // 后续标签页由调用方 tab-new 创建后通过 attachTab 接入监控
  attachMonitoring(session, page)
  return session
}

/** 为新打开的 tab 安装监控并登记到会话。 */
export function attachTab(session: SessionContext, page: Page): void {
  attachMonitoring(session, page)
}

/** 从 URL 取路径部分(解析失败则回退整体后缀比对)。 */
function pathOf(url: string): string {
  try {
    return new URL(url).pathname
  } catch {
    return url
  }
}

/** Chromium 资源失败回显文本是否对应指定 HTTP 状态。 */
function isResourceEchoFor(text: string, status: number): boolean {
  return text.includes(`the server responded with a status of ${status}`)
}

function attachMonitoring(session: SessionContext, page: Page): void {
  const errors: string[] = []
  // error 控制台各自对应的资源 URL(Chromium location, 无则空串; 与 errors 同下标)。
  const errorResourceUrls: string[] = []
  const failedRequests: string[] = []
  // 已登记的测试主动中止（method + 路径精确匹配，一条登记只消费一次；
  // 未登记的 ERR_FAILED（含登记期内的其他 URL/方法）照常记为失败）。
  const registeredAborts: Array<{ method: string; path: string }> = []
  // 已核销失败请求对应的控制台回显配额（路径精确匹配，一条配额只核销
  // 一条同路径的确切 ERR_FAILED 回显）。
  const abortEchoAllowance: string[] = []
  // 全部已完成响应的 (method, path, status), 供预期噪声核对次数。
  const responses: Array<{ method: string; path: string; status: number }> = []
  page.on('console', (msg: ConsoleMessage) => {
    if (msg.type() === 'error') {
      errors.push(msg.text())
      errorResourceUrls.push(msg.location()?.url ?? '')
    }
  })
  page.on('response', (res) => {
    responses.push({ method: res.request().method(), path: pathOf(res.url()), status: res.status() })
  })
  page.on('requestfailed', (req) => {
    // ERR_ABORTED 是导航/页面关闭时浏览器主动取消的 chunk 请求(SPA 懒加载
    // 取消属正常行为, 不是服务器失败); 其余失败(连接拒绝/超时/非 2xx)记录。
    // 测试主动 route.abort 的 ERR_FAILED 仅在有完全相同 method + 路径的
    // 登记时核销（登记一次消费一次，并给同路径回显一条配额）；无登记、
    // 方法或路径任一不符的失败一律记录（含登记期内的其他请求）。
    const errText = req.failure()?.errorText ?? 'unknown'
    if (errText === 'net::ERR_ABORTED') return
    if (errText === 'net::ERR_FAILED') {
      const idx = registeredAborts.findIndex(
        (r) => r.method === req.method() && r.path === pathOf(req.url()),
      )
      if (idx >= 0) {
        registeredAborts.splice(idx, 1)
        abortEchoAllowance.push(pathOf(req.url()))
        return
      }
    }
    failedRequests.push(`${req.method()} ${req.url()} :: ${errText}`)
  })
  page.on('pageerror', (err) => {
    errors.push(`pageerror: ${err.message}`)
    errorResourceUrls.push('')
  })
  session.registerExpectedAbort = (method: string, url: string) => {
    registeredAborts.push({ method, path: pathOf(url) })
  }
  session.assertNoErrors = (expected: ExpectedHttpNoise[] = []) => {
    const remaining = errors.slice()
    const remainingUrls = errorResourceUrls.slice()
    // 已登记中止的控制台回显：文本须为确切的 ERR_FAILED 回显，且其资源
    // 路径须有同路径配额（一次登记只给一条同路径配额；路径不符的同文本
    // 回显仍判失败，无跨 URL 预算共用）。
    const allowance = abortEchoAllowance.slice()
    for (let i = remaining.length - 1; i >= 0; i--) {
      if (remaining[i] !== 'Failed to load resource: net::ERR_FAILED') continue
      const at = allowance.indexOf(pathOf(remainingUrls[i]))
      if (at < 0) continue
      allowance.splice(at, 1)
      remaining.splice(i, 1)
      remainingUrls.splice(i, 1)
    }
    for (const spec of expected) {
      const matched = responses.filter(
        (r) => r.method === spec.method && r.path === spec.path && r.status === spec.status,
      )
      // Only enforce request count when the caller explicitly needs exact-once semantics.
      if (spec.times !== undefined) {
        expect(
          matched.length,
          `预期响应 ${spec.method} ${spec.path} -> ${spec.status} 应恰好 ${spec.times} 次, 实际 ${matched.length} 次; 全部响应:\n${responses.map((r) => `${r.method} ${r.path} -> ${r.status}`).join('\n')}`,
        ).toEqual(spec.times)
      }
      // 只消费核对过的回显: 同状态文本 + 同资源路径, 上限为核对次数。
      // 不在此处直接清空: 额外错误必然剩余并判失败。
      let budget = matched.length
      for (let i = remaining.length - 1; i >= 0 && budget > 0; i--) {
        if (
          isResourceEchoFor(remaining[i], spec.status) &&
          pathOf(remainingUrls[i]) === spec.path
        ) {
          remaining.splice(i, 1)
          remainingUrls.splice(i, 1)
          budget--
        }
      }
    }
    expect(
      remaining,
      `console error / pageerror 应不存在, 实际:\n${remaining.join('\n')}`,
    ).toEqual([])
    expect(
      failedRequests,
      `失败网络请求应不存在, 实际:\n${failedRequests.join('\n')}`,
    ).toEqual([])
  }
}

/** 切换语言(帮助中心语言切换场景)。 */
export async function switchLocale(page: Page, locale: 'zh' | 'en'): Promise<void> {
  await page.getByLabel(/语言|Language/i).selectOption(locale)
}

export { base }
