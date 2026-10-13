// D3 数据页项目主体加载三态: 加载中只转圈、失败只显错+重试、
// 成功才消费真实项目信息；不猜 ±14h/480，不把 null 当正常项目。
//
// 成功内容真实不 mock: 项目名以 GET /projects/{id} 响应为准逐字对照；
// 项目不声明时区, 偏移只从 GET /projects/{id}/datasets/{id} 的真实版本
// 响应读取, 并核对数据集版本行展示同一值。failure 仅用定向拦截目标
// GET /projects/{id}(延迟门挂起真实响应，或 500)；dataset/versions 保持
// 真实响应。不注库、不注入页面状态。

import { test, expect } from '@playwright/test'
import { createSession, uniqueName, strongPassword } from './fixtures'
import { createEngineer, loginToken, requestJson } from './setup/api'
import { APP } from './financial-scaffold'

/** 与前端 formatUtcOffset 同语义的期望渲染(由 API 值算出, 不写死 480)。 */
function expectOffsetText(minutes: number): string {
  const sign = minutes < 0 ? '-' : '+'
  const abs = Math.abs(Math.round(minutes))
  const h = Math.floor(abs / 60)
  const m = abs % 60
  return `UTC${sign}${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}`
}

test.describe('D3: 项目主体加载三态', () => {
  test.setTimeout(300_000)

  test('延迟与失败均不呈现可写表单、恢复后与API一致', async ({ browser }) => {
    const userName = uniqueName('es-d3-doer')
    const password = strongPassword('Init')
    const projectName = uniqueName('D3项目主体')
    const apiCtx = await browser.newContext({ baseURL: APP })
    await createEngineer(apiCtx.request, userName, password)
    const userToken = await loginToken(apiCtx.request, userName, password)
    const created = await requestJson(apiCtx.request, 'POST', '/api/projects', {
      name: projectName,
      baseline_resolution: '1h',
      baseline_start_year: 2026,
      baseline_scenario_mode: 'single',
    }, userToken)
    const pid: number = created.project?.id ?? created.id
    expect(pid).toBeTruthy()
    // 公开 API 建一个真实采样数据集(恢复后版本数据对照用, 无求解)。
    const dsName = uniqueName('D3版本集')
    const ds = await requestJson(apiCtx.request, 'POST', `/api/projects/${pid}/datasets`, {
      name: dsName,
    }, userToken)
    const dsId: number = ds.dataset?.id ?? ds.id
    await requestJson(apiCtx.request, 'POST', `/api/projects/${pid}/datasets/${dsId}/sample?resolution=1h`, {}, userToken)
    const detail = await requestJson(apiCtx.request, 'GET', `/api/projects/${pid}/datasets/${dsId}`, undefined, userToken)
    const ver = (detail.versions ?? [])[0]
    expect(ver?.id).toBeTruthy()
    const verBadge = `v${ver.version_no}`
    // 项目名以项目响应为准(名称唯一防硬编码); 偏移只从版本响应读取,
    // 不得从 GET /projects/{id} 期待偏移(项目不声明时区)。
    const projView = await requestJson(apiCtx.request, 'GET', `/api/projects/${pid}`, undefined, userToken)
    const apiName: string = projView.project?.name ?? projView.name
    expect(apiName).toBe(projectName)
    const apiOffset: number = ver.fixed_utc_offset_minutes
    expect(Number.isFinite(apiOffset)).toBe(true)
    const offsetText = expectOffsetText(apiOffset)

    const projectPath = `/api/projects/${pid}`
    // 单一拦截器: 延迟门挂起真实项目响应, 或按标志 500, 其余放行。
    let releaseProject!: () => void
    const gate = new Promise<void>((resolve) => { releaseProject = resolve })
    let entered = false
    let failProject = false
    const context = await browser.newContext()
    const page = await context.newPage()
    await context.route(
      (url) => url.pathname === projectPath,
      async (route) => {
        entered = true
        const res = await route.fetch()
        if (failProject) {
          await route.fulfill({
            status: 500,
            contentType: 'application/json',
            body: '{"detail":"d3 injected project failure"}',
          })
          return
        }
        await gate
        await route.fulfill({ response: res })
      },
    )
    // 全程写请求: 加载/重试只许读, 不许上传/绑定/采样/任务 POST。
    const writePosts: string[] = []
    page.on('request', (req) => {
      if (req.method() === 'POST' && /\/(datasets|versions|draft|tasks|sample|bind|upload)/.test(req.url())) {
        writePosts.push(`${req.method()} ${req.url()}`)
      }
    })
    const session = await createSession(page, userName, password)

    // 阶段一: 主体挂起期间只转圈, 无可写表单、无伪默认信息。
    try {
      await page.getByText(projectName).click({ timeout: 30_000 })
      await expect(page).toHaveURL(/\/projects\/\d+$/)
      await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('数据管理').click({ timeout: 30_000 })
      await expect(page).toHaveURL(/\/data$/)
      await expect.poll(async () => entered, { timeout: 30_000 }).toBe(true)
      expect(await page.getByRole('button', { name: '下载模板' }).count()).toEqual(0)
      expect(await page.getByText('±14h').count()).toBe(0)
      expect(await page.getByText(projectName).count()).toBe(0)
      expect(writePosts).toEqual([])

      // 释放后真实恢复: 项目名与项目响应一致, 版本行展示版本响应的偏移。
      releaseProject()
      await expect(page.getByText(projectName).first()).toBeVisible({ timeout: 60_000 })
      expect(await page.getByText('±14h').count()).toBe(0)
      const dsRow = page.locator('tr', { hasText: dsName })
      await expect(dsRow).toBeVisible({ timeout: 60_000 })
      await expect(dsRow).toContainText(verBadge, { timeout: 30_000 })
      await expect(dsRow).toContainText(offsetText, { timeout: 30_000 })
      await expect(page.getByRole('button', { name: '下载模板' })).toBeVisible({ timeout: 30_000 })
      expect(writePosts).toEqual([])
    } finally {
      releaseProject()
    }

    // 阶段二: 主体 500(重载重进) → 只显错+重试, 无表单、无伪默认、无数据集表。
    failProject = true
    await page.reload()
    await expect(page.getByText('页面加载失败')).toBeVisible({ timeout: 60_000 })
    const retryBtn = page.getByRole('button', { name: '重试', exact: true })
    await expect(retryBtn).toBeVisible({ timeout: 30_000 })
    expect(await page.getByRole('button', { name: '下载模板' }).count()).toBe(0)
    expect(await page.getByText('±14h').count()).toBe(0)
    expect(await page.getByText(dsName).count()).toBe(0)
    expect(writePosts).toEqual([])

    // 解除后点页面重试(原 Provider 重读): 真实恢复, 且不补第二次列表读取。
    failProject = false
    const datasetGetCount = await page.evaluate(() => performance.getEntriesByType('resource').filter(
      (r) => r.name.includes('/datasets'),
    ).length)
    await retryBtn.click({ timeout: 30_000 })
    await expect(page.getByText(projectName).first()).toBeVisible({ timeout: 60_000 })
    const dsRowAfter = page.locator('tr', { hasText: dsName })
    await expect(dsRowAfter).toBeVisible({ timeout: 60_000 })
    await expect(dsRowAfter).toContainText(verBadge, { timeout: 30_000 })
    await expect(dsRowAfter).toContainText(offsetText, { timeout: 30_000 })
    const datasetGetCountAfter = await page.evaluate(() => performance.getEntriesByType('resource').filter(
      (r) => r.name.includes('/datasets'),
    ).length)
    expect(datasetGetCountAfter).toBe(datasetGetCount)
    expect(writePosts).toEqual([])

    // 预期内噪声: 主体 500×1; 进项目读尚无保存的计算配置 404(D3 外既有行为, 计数锁定防漂移)。
    session.assertNoErrors([
      { method: 'GET', path: projectPath, status: 500, times: 1 },
      { method: 'GET', path: `/api/projects/${pid}/config`, status: 404, times: 1 },
    ])
    await apiCtx.close()
    await context.close()
  })
})
