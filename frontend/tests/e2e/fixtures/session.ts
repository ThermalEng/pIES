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
  /** 断言页面上不存在未处理控制台错误与失败网络请求。 */
  assertNoErrors(): void
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
  const session: SessionContext = { main: page, newTab: page, info, assertNoErrors: () => {} }
  // 后续标签页由调用方 tab-new 创建后通过 attachTab 接入监控
  attachMonitoring(session, page)
  return session
}

/** 为新打开的 tab 安装监控并登记到会话。 */
export function attachTab(session: SessionContext, page: Page): void {
  attachMonitoring(session, page)
}

function attachMonitoring(session: SessionContext, page: Page): void {
  const errors: string[] = []
  const failedRequests: string[] = []
  page.on('console', (msg: ConsoleMessage) => {
    if (msg.type() === 'error') errors.push(msg.text())
  })
  page.on('requestfailed', (req) => {
    // ERR_ABORTED 是导航/页面关闭时浏览器主动取消的 chunk 请求(SPA 懒加载
    // 取消属正常行为, 不是服务器失败); 其余失败(连接拒绝/超时/非 2xx)记录。
    const errText = req.failure()?.errorText ?? 'unknown'
    if (errText === 'net::ERR_ABORTED') return
    failedRequests.push(`${req.method()} ${req.url()} :: ${errText}`)
  })
  page.on('pageerror', (err) => errors.push(`pageerror: ${err.message}`))
  session.assertNoErrors = () => {
    expect(
      errors,
      `console error / pageerror 应不存在, 实际:\n${errors.join('\n')}`,
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
