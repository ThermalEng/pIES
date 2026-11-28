// pIES-Market 纯静态目录页 Playwright E2E 验收:
// - 消费 build_index 生成产物（./index.json, ies.market-index@1.0.0）
// - 双种子制品（ies.device.solar_pv_sample + ies.evaluator.sample_carbon）渲染与双语切换
// - 分类筛选与关键词搜索
// - 详情弹窗展示 consumes/produces 与安全 textContent README 渲染
// - 真实离线包下载字节 SHA-256 与页面展示 sha256 一致
// - ./index.json 返回 500 时显式展示错误告警（不伪装为空列表）

import { createHash } from 'node:crypto'
import { createReadStream, promises as fs } from 'node:fs'
import http from 'node:http'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { expect, test } from '@playwright/test'

const __dirname = path.dirname(fileURLToPath(import.meta.url))

const CONTENT_TYPES = {
  '.html': 'text/html; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.md': 'text/plain; charset=utf-8',
  '.yaml': 'text/yaml; charset=utf-8',
  '.yml': 'text/yaml; charset=utf-8',
  '.zip': 'application/zip',
}

async function startStaticServer(rootDir) {
  const resolvedRoot = path.resolve(rootDir)
  const server = http.createServer(async (req, res) => {
    try {
      const reqUrl = new URL(req.url ?? '/', 'http://127.0.0.1')
      let relPath = decodeURIComponent(reqUrl.pathname).replace(/^\/+/, '')
      if (!relPath) relPath = 'index.html'
      const candidate = path.resolve(resolvedRoot, relPath)
      if (candidate !== resolvedRoot && !candidate.startsWith(resolvedRoot + path.sep)) {
        res.statusCode = 403
        res.end('Forbidden')
        return
      }
      const stat = await fs.stat(candidate)
      if (!stat.isFile()) {
        res.statusCode = 404
        res.end('Not found')
        return
      }
      const ext = path.extname(candidate).toLowerCase()
      res.setHeader('Content-Type', CONTENT_TYPES[ext] ?? 'application/octet-stream')
      res.setHeader('Content-Length', String(stat.size))
      createReadStream(candidate).pipe(res)
    } catch {
      res.statusCode = 404
      res.end('Not found')
    }
  })
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
  const addr = server.address()
  return {
    baseUrl: `http://127.0.0.1:${addr.port}`,
    close: () => new Promise((resolve, reject) => server.close((err) => (err ? reject(err) : resolve()))),
  }
}

async function sha256OfStream(stream) {
  const hash = createHash('sha256')
  for await (const chunk of stream) {
    hash.update(chunk)
  }
  return hash.digest('hex')
}

test.describe('pIES-Market static site', () => {
  let serverHandle = null
  let baseUrl = process.env.MARKET_SITE_URL ?? ''

  test.beforeAll(async () => {
    if (!baseUrl) {
      const distDir = process.env.MARKET_DIST_DIR ?? path.resolve(__dirname, '../../dist')
      serverHandle = await startStaticServer(distDir)
      baseUrl = serverHandle.baseUrl
    }
  })

  test.afterAll(async () => {
    if (serverHandle) await serverHandle.close()
  })

  test('renders seed artifacts, filters/searches, verifies README and real download sha256', async ({ page }) => {
    const errors = []
    page.on('pageerror', (err) => errors.push(err.message))

    await page.goto(baseUrl)

    // 1. 双种子制品渲染
    const solarCard = page.locator('.market-card[data-artifact-id="ies.device.solar_pv_sample"]')
    const carbonCard = page.locator('.market-card[data-artifact-id="ies.evaluator.sample_carbon"]')
    await expect(solarCard).toBeVisible()
    await expect(carbonCard).toBeVisible()
    await expect(solarCard.getByText('r1', { exact: true })).toBeVisible()
    await expect(carbonCard.getByText('v1.0.0', { exact: true })).toBeVisible()

    // 2. 中英文切换
    await page.locator('#lang-en').click()
    await expect(page.locator('html')).toHaveAttribute('lang', 'en-US')
    await expect(solarCard.getByText('Device Model', { exact: true })).toBeVisible()
    await expect(carbonCard.getByText('Evaluator', { exact: true })).toBeVisible()
    await page.locator('#lang-zh').click()
    await expect(page.locator('html')).toHaveAttribute('lang', 'zh-CN')
    await expect(solarCard.getByText('设备模型', { exact: true })).toBeVisible()

    // 3. 分类筛选与关键词搜索
    await page.locator('.market-filter-btn[data-kind="device_model"]').click()
    await expect(solarCard).toBeVisible()
    await expect(carbonCard).toBeHidden()

    await page.locator('.market-filter-btn[data-kind="evaluator"]').click()
    await expect(carbonCard).toBeVisible()
    await expect(solarCard).toBeHidden()

    await page.locator('.market-filter-btn[data-kind="all"]').click()
    await page.locator('#search-input').fill('solar')
    await expect(solarCard).toBeVisible()
    await expect(carbonCard).toBeHidden()

    await page.locator('#search-input').fill('not-matching-anything')
    await expect(page.locator('.market-card')).toHaveCount(0)
    await expect(page.locator('#empty-box')).toBeVisible()
    await expect(page.locator('#status-alert')).toBeHidden()

    await page.locator('#search-input').fill('')
    await expect(solarCard).toBeVisible()
    await expect(carbonCard).toBeVisible()

    // 4. 详情弹窗与安全 README 展示
    const indexRes = await page.request.get(`${baseUrl}/index.json`)
    expect(indexRes.ok()).toBe(true)
    const indexJson = await indexRes.json()
    const carbonEntry = indexJson.artifacts.find((a) => a.id === 'ies.evaluator.sample_carbon')
    expect(carbonEntry).toBeTruthy()

    await carbonCard.locator('[data-action="details"]').click()
    const modal = page.locator('#artifact-modal')
    await expect(modal).toBeVisible()
    await expect(page.locator('#modal-id')).toHaveText('ies.evaluator.sample_carbon')
    if (Array.isArray(carbonEntry.consumes) && carbonEntry.consumes.length > 0) {
      await expect(page.locator('#modal-consumes-list')).toContainText(carbonEntry.consumes[0])
    }
    if (Array.isArray(carbonEntry.produces) && carbonEntry.produces.length > 0) {
      await expect(page.locator('#modal-produces-list')).toContainText(carbonEntry.produces[0])
    }
    if (carbonEntry.readme_url) {
      const readmeRes = await page.request.get(`${baseUrl}/${carbonEntry.readme_url}`)
      expect(readmeRes.ok()).toBe(true)
      const rawReadme = await readmeRes.text()
      await expect
        .poll(() => page.locator('#modal-readme-body').textContent())
        .toBe(rawReadme)
    }
    await page.locator('#modal-close').click()
    await expect(modal).toBeHidden()

    // 5. 真实下载文件字节 SHA-256 与页面展示 sha256 一致
    for (const [id, card] of [
      ['ies.device.solar_pv_sample', solarCard],
      ['ies.evaluator.sample_carbon', carbonCard],
    ]) {
      const displayedSha = (await card.locator('[data-testid="card-sha256"]').textContent())?.trim() ?? ''
      expect(displayedSha).toMatch(/^[0-9a-f]{64}$/)
      const [download] = await Promise.all([
        page.waitForEvent('download'),
        card.locator('[data-action="download"]').click(),
      ])
      const stream = await download.createReadStream()
      expect(stream).not.toBeNull()
      const actualSha = await sha256OfStream(stream)
      expect(actualSha, `downloaded sha256 mismatch for ${id}`).toBe(displayedSha)
    }

    expect(errors).toEqual([])
  })

  test('shows explicit error state when index.json returns 500', async ({ page }) => {
    await page.route('**/index.json', (route) =>
      route.fulfill({
        status: 500,
        contentType: 'text/plain',
        body: 'Internal Server Error',
      }),
    )

    await page.goto(baseUrl)

    const alert = page.locator('#status-alert')
    await expect(alert).toBeVisible()
    await expect(alert).toContainText('500')
    await expect(page.locator('#empty-box')).toBeHidden()
    await expect(page.locator('.market-card')).toHaveCount(0)
  })
})
