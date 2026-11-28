// E5a 开放生态前端产品集成回归验收：
// 1. 管理员治理页(/admin/ecosystem): 正交模式切换(local × requires_approval)与待审批队列通过；
// 2. 统一页(/custom): 默认「我的创作/草稿」兼容、本地 .device.yaml 导入、切换「市场与已安装」完成成员安装；
// 3. 计算配置页(/projects/:id/config): 消费 /api/ecosystem/stage-catalog 与多评价器组装；
// 4. 在线市场三态与免登录 OTP 投稿向导 + ResultsPage 自描述评价报告与失败评价器重试。

import { test, expect } from '@playwright/test'
import { createSession, uniqueName, strongPassword } from './fixtures'
import { adminToken, createEngineer, createProject, loginToken, requestJson } from './setup/api'

const ADMIN_PASS = process.env.E2E_ADMIN_PASSWORD ?? 'Iesplan-Admin-2026!'

function crc32(buf: Buffer): number {
  let crc = 0xffffffff
  for (let i = 0; i < buf.length; i += 1) {
    crc ^= buf[i]
    for (let j = 0; j < 8; j += 1) {
      crc = (crc >>> 1) ^ (crc & 1 ? 0xedb88320 : 0)
    }
  }
  return (crc ^ 0xffffffff) >>> 0
}

function buildStoredZip(filename: string, content: string): Buffer {
  const nameBuf = Buffer.from(filename, 'utf8')
  const dataBuf = Buffer.from(content, 'utf8')
  const crc = crc32(dataBuf)

  const localHeader = Buffer.alloc(30 + nameBuf.length)
  localHeader.writeUInt32LE(0x04034b50, 0)
  localHeader.writeUInt16LE(20, 4)
  localHeader.writeUInt16LE(0, 6)
  localHeader.writeUInt16LE(0, 8)
  localHeader.writeUInt16LE(0, 10)
  localHeader.writeUInt16LE(0, 12)
  localHeader.writeUInt32LE(crc, 14)
  localHeader.writeUInt32LE(dataBuf.length, 18)
  localHeader.writeUInt32LE(dataBuf.length, 22)
  localHeader.writeUInt16LE(nameBuf.length, 26)
  localHeader.writeUInt16LE(0, 28)
  nameBuf.copy(localHeader, 30)

  const cdOffset = localHeader.length + dataBuf.length
  const centralDir = Buffer.alloc(46 + nameBuf.length)
  centralDir.writeUInt32LE(0x02014b50, 0)
  centralDir.writeUInt16LE(20, 4)
  centralDir.writeUInt16LE(20, 6)
  centralDir.writeUInt16LE(0, 8)
  centralDir.writeUInt16LE(0, 10)
  centralDir.writeUInt16LE(0, 12)
  centralDir.writeUInt16LE(0, 14)
  centralDir.writeUInt32LE(crc, 16)
  centralDir.writeUInt32LE(dataBuf.length, 20)
  centralDir.writeUInt32LE(dataBuf.length, 24)
  centralDir.writeUInt16LE(nameBuf.length, 28)
  centralDir.writeUInt16LE(0, 30)
  centralDir.writeUInt16LE(0, 32)
  centralDir.writeUInt16LE(0, 34)
  centralDir.writeUInt16LE(0, 36)
  centralDir.writeUInt32LE(0, 38)
  centralDir.writeUInt32LE(0, 42)
  nameBuf.copy(centralDir, 46)

  const eocd = Buffer.alloc(22)
  eocd.writeUInt32LE(0x06054b50, 0)
  eocd.writeUInt16LE(0, 4)
  eocd.writeUInt16LE(0, 6)
  eocd.writeUInt16LE(1, 8)
  eocd.writeUInt16LE(1, 10)
  eocd.writeUInt32LE(centralDir.length, 12)
  eocd.writeUInt32LE(cdOffset, 16)
  eocd.writeUInt16LE(0, 20)

  return Buffer.concat([localHeader, dataBuf, centralDir, eocd])
}

const makeCustomDeviceYaml = (deviceId: string): string => `schema: ies.device-model
schema_version: "1.0.0"
device:
  id: ${deviceId}
  names:
    zh-CN: E5a 测试负荷
    en-US: E5a Test Load
properties:
  rated_power_kw:
    value: 100
    unit: kW
    valid_range:
      minimum: 1
      maximum: 1000
interfaces:
  electric_in:
    type: in
    carrier: electricity
    unit: kW
    valid_range:
      minimum: 0
      maximum: null
equations:
  variables: {}
  relations: []
`

test.describe('E5a open ecosystem product integration', () => {
  test('enterprise local approval flow: upload -> admin approve -> member install -> config stage catalog', async ({
    browser,
    request,
  }) => {
    const slug = uniqueName('e5a-load').toLowerCase().replace(/[^a-z0-9_-]/g, '-').slice(0, 24)
    const deviceId = `acme.device.${slug}`
    const userName = uniqueName('e5a-eng')
    const userPass = strongPassword()
    const eng = await createEngineer(request, userName, userPass)
    const engToken = await loginToken(request, eng.username, eng.password)
    const project = await createProject(request, engToken, uniqueName('e5a-proj'))

    const adminCtx = await browser.newContext()
    const engCtx = await browser.newContext()
    const adminPage = await adminCtx.newPage()
    const engPage = await engCtx.newPage()
    try {
      // 1. 管理员开启 local + local_upload_requires_approval=true
      const adminSess = await createSession(adminPage, 'admin', ADMIN_PASS)
      await adminPage.getByRole('link', { name: /生态治理|Ecosystem/i }).click()
      await expect(adminPage).toHaveURL(/\/admin\/ecosystem$/)
      await adminPage.locator('#eco-market-mode').selectOption('local')
      const approvalBox = adminPage.locator('#eco-approval-switch')
      if (!(await approvalBox.isChecked())) {
        await approvalBox.check()
      }
      await adminPage.getByRole('button', { name: /保存生态设置|Save ecosystem settings/i }).click()
      await expect(adminPage.getByText(/生态设置已保存|Ecosystem settings saved/i)).toBeVisible()

      // 2. 工程师在 /custom 默认「我的创作/草稿」页签导入 .device.yaml -> 待审批
      const engSess = await createSession(engPage, eng.username, eng.password)
      await engPage.getByRole('link', { name: /^自定义$|^Custom$/i }).click()
      await expect(engPage).toHaveURL(/\/custom$/)
      await expect(engPage.getByRole('tab', { name: /我的创作\/草稿|My Creations/i })).toHaveAttribute('aria-selected', 'true')

      await engPage.locator('#ies-eco-import-yaml').fill(makeCustomDeviceYaml(deviceId))
      await engPage.getByRole('button', { name: /导入设备模型|Import device model/i }).click()
      await expect(engPage.getByRole('cell', { name: deviceId })).toBeVisible()

      // 3. 管理员审批通过
      await adminPage.getByRole('button', { name: /刷新|Refresh/i }).click()
      const pendingRow = adminPage.locator(`[data-pending-artifact="${deviceId}"]`)
      await expect(pendingRow).toBeVisible()
      await pendingRow.getByRole('button', { name: /^通过$|^Approve$/i }).click()
      await expect(pendingRow).toBeHidden()
      await expect(adminPage.locator(`[data-admin-artifact="${deviceId}"]`)).toBeVisible()
      adminSess.assertNoErrors()

      // 4. 工程师在「市场与已安装」安装该设备模型
      await engPage.getByRole('tab', { name: /市场与已安装|Market & Installed/i }).click()
      await engPage.getByRole('button', { name: /刷新|Refresh/i }).click()
      const card = engPage.locator(`[data-artifact-key="device_model:${deviceId}"]`)
      await expect(card).toBeVisible()
      await card.getByRole('button', { name: /^安装$|^Install$/i }).click()
      await expect(card.getByText(/本地（已安装）|Local \(Installed\)/i)).toBeVisible()

      // 5. ConfigPage 消费 /api/ecosystem/stage-catalog 与多评价器面板
      await engPage.goto(`/projects/${project.id}/config`)
      await expect(engPage.locator('#calc-catalog-generator')).toBeVisible()
      await expect(engPage.locator('#calc-catalog-solver')).toBeVisible()
      await expect(engPage.locator('#calc-catalog-algorithm')).toBeVisible()
      engSess.assertNoErrors([{ method: 'GET', path: `/api/projects/${project.id}/config`, status: 404 }])
    } finally {
      await adminCtx.close()
      await engCtx.close()
      // 清理：恢复管理员默认配置（保持环境整洁）
      const tok = await adminToken(request)
      await requestJson(
        request,
        'PUT',
        '/api/ecosystem/settings',
        {
          market_mode: 'local',
          local_upload_requires_approval: true,
          pip_index_url: null,
          market_index_url: null,
          submission_gateway_url: null,
        },
        tok,
      )
    }
  })

  test('online market three-state + OTP submission wizard + results self-describing report', async ({
    page,
    request,
  }) => {
    const userName = uniqueName('e5a-mkt')
    const userPass = strongPassword()
    const eng = await createEngineer(request, userName, userPass)
    const engToken = await loginToken(request, eng.username, eng.password)
    const project = await createProject(request, engToken, uniqueName('e5a-mkt-proj'))

    let windInstalled = false
    await page.route('**/api/ecosystem/settings', async (route) => {
      if (route.request().method() === 'GET') {
        await route.fulfill({
          json: {
            market_mode: 'online',
            local_upload_requires_approval: false,
            pip_index_url: 'https://pypi.example.com/simple',
            market_index_url: 'https://market.example.com/index.json',
            submission_gateway_url: 'https://submit.example.com',
          },
        })
        return
      }
      await route.continue()
    })
    await page.route('**/api/ecosystem/artifacts', async (route) => {
      await route.fulfill({
        json: {
          items: windInstalled
            ? [
                {
                  id: 901,
                  kind: 'evaluator',
                  stable_id: 'acme.evaluator.carbon',
                  version: '1.0.0',
                  source_origin: 'market',
                  approval_status: 'approved',
                  install_status: 'ready',
                  enabled: true,
                  installed: false,
                  sha256: 'a'.repeat(64),
                  metadata: { names: { 'zh-CN': '碳排放评价器', 'en-US': 'Carbon Evaluator' } },
                  diagnostics: null,
                  created_at: '2026-11-28T00:00:00Z',
                },
              ]
            : [],
        },
      })
    })
    await page.route('**/api/ecosystem/market/index', async (route) => {
      await route.fulfill({
        json: {
          items: [
            {
              id: 'acme.evaluator.carbon',
              kind: 'evaluator',
              version: '1.0.0',
              name: { zh_cn: '碳排放评价器', en_us: 'Carbon Evaluator' },
              description: 'Carbon evaluation plugin',
              publisher: 'acme',
              license: 'MIT',
              compatibility: { sdk: '>=1.0.0' },
              consumes: ['solution'],
              produces: ['report'],
              sha256: 'a'.repeat(64),
              size_bytes: 2048,
              member_state: windInstalled ? 'local_uninstalled' : 'remote',
              update_available: false,
            },
          ],
        },
      })
    })
    await page.route('**/api/ecosystem/market/install', async (route) => {
      windInstalled = true
      await route.fulfill({
        json: {
          artifact: {
            id: 901,
            kind: 'evaluator',
            stable_id: 'acme.evaluator.carbon',
            version: '1.0.0',
            source_origin: 'market',
            approval_status: 'approved',
            install_status: 'ready',
            enabled: true,
            installed: false,
            sha256: 'a'.repeat(64),
            metadata: {},
            diagnostics: null,
            created_at: '2026-11-28T00:00:00Z',
          },
          member_state: 'local_uninstalled',
        },
      })
    })
    await page.route('**/api/ecosystem/market/submit/otp-request', async (route) => {
      await route.fulfill({ json: { challenge_token: 'chal-123', expires_in_seconds: 600 } })
    })
    await page.route('**/api/ecosystem/market/submit/otp-verify', async (route) => {
      await route.fulfill({ json: { submission_token: 'sub-456', expires_in_seconds: 900 } })
    })
    let submittedMultipart = ''
    await page.route('**/api/ecosystem/market/submit', async (route) => {
      submittedMultipart = (route.request().postDataBuffer() ?? Buffer.alloc(0)).toString('latin1')
      await route.fulfill({
        json: { pr_number: 42, pr_url: 'https://github.com/acme/market/pull/42', branch: 'submit/carbon', duplicate: false },
      })
    })

    const sess = await createSession(page, eng.username, eng.password)
    await page.getByRole('link', { name: /^自定义$|^Custom$/i }).click()

    // OTP 投稿向导测试
    await page.getByRole('button', { name: /投稿到公共市场|Submit to Public Market/i }).click()
    await page.getByPlaceholder('author@example.com').fill('dev@example.com')
    await page.getByRole('button', { name: /发送验证码|Send OTP/i }).click()
    await page.getByPlaceholder('6-digit OTP').fill('123456')
    await page.getByRole('button', { name: /验证凭证|Verify OTP/i }).click()
    await page.locator('#sub-kind').selectOption('evaluator')
    await page.locator('#sub-id').fill('acme.evaluator.carbon')
    await page.locator('#sub-ver').fill('1.0.0')
    await page.locator('#sub-pub').fill('acme')
    await page.locator('#sub-lic').fill('MIT')
    await page.locator('#sub-file').setInputFiles({
      name: 'carbon.algorithm.zip',
      mimeType: 'application/zip',
      buffer: buildStoredZip(
        'plugin.yaml',
        [
          'schema: ies.algorithm-plugin',
          'schema_version: "1.0.0"',
          'kind: evaluator',
          'plugin_id: acme.evaluator.carbon',
          'version: "1.0.0"',
          'publisher: acme',
          'license: MIT',
        ].join('\n'),
      ),
    })
    await page.getByRole('button', { name: /提交投稿 PR|Submit PR/i }).click()
    await expect(page.getByText('https://github.com/acme/market/pull/42')).toBeVisible()
    expect(submittedMultipart).toMatch(/name="kind"[\r\n]+evaluator/)
    expect(submittedMultipart).toMatch(/name="artifact_id"[\r\n]+acme\.evaluator\.carbon/)
    expect(submittedMultipart).toMatch(/name="version_or_revision"[\r\n]+1\.0\.0/)
    expect(submittedMultipart).toContain('plugin.yaml')
    expect(submittedMultipart).toContain('plugin_id: acme.evaluator.carbon')
    await page.getByRole('dialog').getByRole('button', { name: /关闭|Close/i }).click()

    // 在线市场三态: remote -> download -> local_uninstalled
    await page.getByRole('tab', { name: /市场与已安装|Market & Installed/i }).click()
    const mktCard = page.locator('[data-artifact-key="evaluator:acme.evaluator.carbon"]')
    await expect(mktCard.getByText(/^远端$|^Remote$/i)).toBeVisible()
    await mktCard.getByRole('button', { name: /下载并安装|Download & Install/i }).click()
    await expect(mktCard.getByText(/本地（未安装）|Local \(Uninstalled\)/i)).toBeVisible()

    // ResultsPage 自描述评价报告与失败评价器重试按钮
    let retried = false
    const taskSummary = {
      id: 77,
      type: 'optimization',
      status: 'completed',
      business_outcome: 'normal_completion',
      priority: 0,
      calc_snapshot_id: 1,
      requested_by: eng.id,
      requested_at: '2026-11-28T00:00:00Z',
      attempt_count: 1,
      max_attempts: 3,
      idempotency_key: null,
      superseded_by_task_id: null,
      created_at: '2026-11-28T00:00:00Z',
      updated_at: '2026-11-28T00:01:00Z',
      result_available: true,
      summary: { attempt_no: 1, percent: 100, stage: 'done', queue_position: null },
      completed_at: '2026-11-28T00:01:00Z',
      latest_package_id: 770,
      latest_evidence_status: 'partial',
    }
    await page.route(`**/api/projects/${project.id}/tasks?*`, async (route) => {
      await route.fulfill({ json: { items: [taskSummary], next_cursor: null } })
    })
    await page.route(`**/api/projects/${project.id}/tasks`, async (route) => {
      if (route.request().method() === 'GET') {
        await route.fulfill({ json: { items: [taskSummary], next_cursor: null } })
        return
      }
      await route.continue()
    })
    await page.route(`**/api/projects/${project.id}/tasks/77`, async (route) => {
      await route.fulfill({
        json: {
          task: {
            ...taskSummary,
            calc_snapshot: null,
            attempts: [
              {
                id: 1,
                attempt_no: 1,
                status: 'succeeded',
                worker_id: 'w1',
                stop_reason: null,
                started_at: '2026-11-28T00:00:00Z',
                finished_at: '2026-11-28T00:01:00Z',
              },
            ],
            current_lease: null,
            progress: {
              attempt_no: 1,
              percent: 100,
              stage: 'done',
              detail: null,
              updated_at: '2026-11-28T00:01:00Z',
              source: 'worker',
            },
            diagnostics: [],
            batch: { parent_task_id: null, child_task_count: 0, children: [] },
          },
        },
      })
    })
    await page.route(`**/api/projects/${project.id}/tasks/77/result/assessments`, async (route) => {
      await route.fulfill({ json: { items: [] } })
    })
    await page.route(`**/api/projects/${project.id}/tasks/77/result`, async (route) => {
      await route.fulfill({
        json: {
          result: {
            task: { id: 77, type: 'optimization', status: 'completed', business_outcome: 'normal_completion', calc_snapshot_id: 1 },
            evidence_status: 'partial',
            evidence: { id: 770, status: 'partial', object_id: 1, attempt_id: 1, created_at: '2026-11-28T00:01:00Z' },
            assessment: null,
            metrics_summary: null,
            solver_summary: null,
            financial_evaluation: null,
            financial_absence: 'evaluator_failed',
            candidates: [],
            planning_solution: null,
            snapshot_refs: null,
            best: null,
            plan_summary: null,
            hourly_refs: null,
            selection: null,
            evaluator_results: [
              {
                evaluator: 'acme.evaluator.carbon@1.0.0',
                status: 'failed',
                contract: 'ies.evaluation-report@1.0.0',
                error: { code: 'EVAL-TIMEOUT', message: 'timeout error' },
                attempt_id: 1,
                finished_at: '2026-11-28T00:01:00Z',
              },
            ],
            evaluation_reports: [
              {
                evaluator: 'acme.evaluator.carbon@1.0.0',
                schema: 'ies.evaluation-report',
                schema_version: '1.0.0',
                title: 'Carbon Self-Describing Report',
                summary: 'Generated directly from schema output',
                metrics: [{ id: 'co2', label: 'CO2 Total', value: 18.4, unit: 'tCO2' }],
                charts: [
                  {
                    id: 'c1',
                    title: 'Monthly Carbon Chart',
                    chart_type: 'bar',
                    x_label: 'Month',
                    y_label: 'CO2',
                    y_unit: 'tCO2',
                    x_values: ['Jan', 'Feb'],
                    series: [{ id: 's1', label: 'Grid', values: [9.2, 9.2] }],
                  },
                ],
                tables: [
                  {
                    id: 't1',
                    title: 'Breakdown Table',
                    columns: [{ id: 'src', label: 'Source' }, { id: 'val', label: 'Value', unit: 'tCO2' }],
                    rows: [['Grid', 18.4]],
                  },
                ],
              },
            ],
          },
        },
      })
    })
    await page.route(`**/api/projects/${project.id}/tasks/77/result/evaluators/retry`, async (route) => {
      retried = true
      await route.fulfill({
        json: {
          task: { ...taskSummary, id: 78, status: 'pending' },
          source_task_id: 77,
          evaluator: 'acme.evaluator.carbon@1.0.0',
          replayed: false,
          hint: null,
        },
      })
    })

    await page.goto(`/projects/${project.id}/results`)
    await expect(page.getByText('Carbon Self-Describing Report')).toBeVisible()
    await expect(page.getByText('Generated directly from schema output')).toBeVisible()
    await expect(page.locator('[data-eval-report="acme.evaluator.carbon@1.0.0"] svg')).toBeVisible()
    await expect(page.getByRole('cell', { name: 'Grid' })).toBeVisible()

    await page.getByRole('button', { name: /重试评价器|Retry Evaluator/i }).click()
    await expect.poll(() => retried).toBe(true)
    await expect(page.getByText(/已提交评价器.*重试请求|Retry submitted for evaluator/i)).toBeVisible()

    // ConfigPage 动态 stage-catalog: 布尔参数往返勾选(true/false) + {target: {instance, interface}, algorithm} 预测保存载荷校验
    let capturedConfigPayload: Record<string, unknown> | null = null
    await page.route('**/api/ecosystem/stage-catalog', async (route) => {
      await route.fulfill({
        json: {
          generators: [
            {
              ref: 'ies.generator.highs_milp@1.0.0',
              kind: 'generator',
              source: 'builtin',
              modes: ['planning'],
              parameters_schema: {
                type: 'object',
                properties: {
                  use_cache: { type: 'boolean', title: 'Use Cache', default: false },
                },
              },
            },
          ],
          solvers: [
            { ref: 'ies.solver.highs@1.7.2', kind: 'solver', source: 'builtin', parameters_schema: {} },
          ],
          algorithms: [],
          predictors: [
            { ref: 'acme.predictor.wind@1.0.0', kind: 'predictor', source: 'local', parameters_schema: {} },
          ],
          evaluators: [
            { ref: 'ies.evaluator.finance@1.0.0', kind: 'evaluator', source: 'builtin', parameters_schema: {} },
          ],
        },
      })
    })
    await page.route(`**/api/projects/${project.id}/config`, async (route) => {
      if (route.request().method() === 'PUT') {
        capturedConfigPayload = JSON.parse(route.request().postData() ?? '{}') as Record<string, unknown>
        await route.fulfill({
          json: { calculation_config: capturedConfigPayload.calculation_config, revision: 1 },
        })
        return
      }
      await route.continue()
    })
    await page.goto(`/projects/${project.id}/config`)
    await page.locator('#calc-catalog-generator').selectOption('ies.generator.highs_milp@1.0.0')
    await page.locator('#calc-catalog-solver').selectOption('ies.solver.highs@1.7.2')
    const cacheBox = page.locator('#calc-slot-generator-use_cache')
    await cacheBox.check()
    await expect.poll(async () => JSON.parse(await page.locator('#calc-options').inputValue())).toMatchObject({
      generator: { use_cache: true },
    })
    await cacheBox.uncheck()
    await expect.poll(async () => JSON.parse(await page.locator('#calc-options').inputValue())).toMatchObject({
      generator: { use_cache: false },
    })
    await page.locator('#calc-catalog-predictor-instance').fill('pv1')
    await page.locator('#calc-catalog-predictor-interface').fill('power_out')
    await page.getByRole('button', { name: /加入 predictions\[\]|Add to predictions\[\]/i }).click()
    await page.locator('#calc-mode').fill('planning')
    await page.locator('#calc-precision').selectOption('medium')
    await page.locator('#calc-outputs').fill('["solution","metrics"]')
    await page.getByRole('button', { name: /保存计算配置|Save calculation config/i }).click()
    await expect.poll(() => capturedConfigPayload).not.toBeNull()
    const savedCfg = (capturedConfigPayload as unknown as { calculation_config: Record<string, unknown> }).calculation_config
    expect(savedCfg.predictions).toEqual([
      { target: { instance: 'pv1', interface: 'power_out' }, algorithm: 'acme.predictor.wind@1.0.0', parameters: {} },
    ])
    expect(savedCfg.options).toEqual({ generator: { use_cache: false } })
    sess.assertNoErrors([{ method: 'GET', path: `/api/projects/${project.id}/config`, status: 404 }])
  })
})
