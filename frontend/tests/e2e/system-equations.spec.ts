// N3: author document -> draft/conflict -> real worker -> distinct system
// series -> paging/Excel/package. Only prerequisites use the public setup API.
import { test, expect } from '@playwright/test'
import { execFileSync } from 'node:child_process'
import { readFileSync } from 'node:fs'
import { createSession, uniqueName, strongPassword } from './fixtures'
import { createEngineer, loginToken, requestJson } from './setup/api'
import { APP, scaffoldProject } from './financial-scaffold'

test('N3 system document survives editing and feeds same-solve curves and exports', async ({ browser }) => {
  test.setTimeout(900_000)
  const username = uniqueName('n3-author')
  const password = strongPassword('Init')
  const setup = await browser.newContext({ baseURL: APP })
  await createEngineer(setup.request, username, password)
  const token = await loginToken(setup.request, username, password)
  const pid = await scaffoldProject(requestJson, setup.request, token, uniqueName('N3'), uniqueName('n3-finance'))
  const context = await browser.newContext()
  const page = await context.newPage()
  const session = await createSession(page, username, password)
  await page.goto(`/projects/${pid}/planning`)
  const editor = page.getByLabel('系统方程 YAML', { exact: true })
  await expect(editor).toBeEnabled()
  await page.screenshot({ path: `${process.env.E2E_OUTPUT_DIR}/n3-document-entry.png`, fullPage: true })
  const document = {
    system_equations: {
      hp1: {
        constants: {},
        references: { actual: { kind: 'interface', instance: 'hp1', interface: 'electricity_in' } },
        variables: { electricity_in: { unit: 'kW', valid_range: { minimum: 0, maximum: null } } },
        relations: [{ id: 'account', expression: 'actual[t] = electricity_in[t]' }],
      },
    },
    outputs: { series: [], metrics: [], system_series: [{ block: 'hp1', variable: 'electricity_in' }] },
  }
  const source = JSON.stringify(document, null, 2)
  await editor.fill(source)
  await page.getByRole('button', { name: '保存系统方程', exact: true }).click()
  await expect(page.getByText('系统方程草稿已保存', { exact: true })).toBeVisible()
  const saved = await requestJson(page.request, 'GET', `/api/projects/${pid}`)
  expect(saved.draft.content.system_equations).toEqual(document.system_equations)
  expect(saved.draft.content.outputs).toEqual(document.outputs)
  await page.reload()
  await expect(editor).toHaveValue(/actual\[t\] = electricity_in\[t\]/)

  // Invalid YAML stays in the editor; the parser does not silently repair it.
  await editor.fill('system_equations: [')
  await page.getByRole('button', { name: '保存系统方程', exact: true }).click()
  await expect(editor).toHaveValue('system_equations: [')
  await expect(page.getByRole('alert').first()).toBeVisible()
  await page.getByRole('button', { name: '重载系统方程', exact: true }).click()
  await expect(editor).toHaveValue(/actual\[t\] = electricity_in\[t\]/)

  // Another real browser window saves first; stale writes must preserve input.
  const other = await context.newPage()
  await other.goto(`/projects/${pid}/planning`)
  const otherEditor = other.getByLabel('系统方程 YAML', { exact: true })
  await expect(otherEditor).toBeEnabled()
  await otherEditor.fill(source)
  await other.getByRole('button', { name: '保存系统方程', exact: true }).click()
  await expect(other.getByText('系统方程草稿已保存', { exact: true })).toBeVisible()
  await other.close()
  await editor.fill(source)
  const conflict = page.waitForResponse((res) => res.url().endsWith(`/api/projects/${pid}/draft`) && res.request().method() === 'PUT')
  await page.getByRole('button', { name: '保存系统方程', exact: true }).click()
  expect((await conflict).status()).toBe(409)
  await expect(editor).toHaveValue(source)
  await page.getByRole('button', { name: '重载系统方程', exact: true }).click()
  await expect(editor).toHaveValue(/actual\[t\] = electricity_in\[t\]/)

  // Real task submission UI, no solver/result mocking.
  await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('任务中心').click()
  await page.getByRole('button', { name: /提交任务|Submit task/i }).first().click()
  await page.locator('#task-type').selectOption('optimization')
  await page.locator('#task-construction-period').fill('0')
  await page.locator('#task-operation-years').fill('1')
  await page.locator('#task-discount-rate').fill('0.08')
  await page.locator('#task-construction-payments').fill('[{"t":0,"ratio":1}]')
  await page.locator('#task-maintenance-expenses').fill('[]')
  const submitted = page.waitForResponse((res) => res.url().endsWith(`/api/projects/${pid}/tasks`) && res.request().method() === 'POST')
  await page.getByRole('button', { name: /提交任务|Submit task/i }).last().click()
  const submission = await submitted
  expect(submission.status()).toBe(201)
  const taskId = (await submission.json()).task.id
  await expect(async () => {
    const detail = await requestJson(page.request, 'GET', `/api/projects/${pid}/tasks/${taskId}`)
    expect(detail.task.status).toBe('completed')
  }).toPass({ timeout: 420_000, intervals: [5000] })
  await page.goto(`/projects/${pid}/results`)
  await expect(page.getByText('系统 hp1.electricity_in', { exact: true })).toBeVisible({ timeout: 60_000 })
  await expect(page.getByText('hp1.electricity_in', { exact: true })).toBeVisible()
  await page.screenshot({ path: `${process.env.E2E_OUTPUT_DIR}/n3-distinct-series.png`, fullPage: true })
  const view = await requestJson(page.request, 'GET', `/api/projects/${pid}/tasks/${taskId}/result`)
  const directory = view.result.hourly_refs[0]
  const system = directory.series.find((entry: any) => entry.kind === 'system')
  const device = directory.series.find((entry: any) => entry.kind === 'device' && entry.instance_id === 'hp1' && entry.variable_id === 'electricity_in')
  expect(system).toEqual({ kind: 'system', series_id: 'system:hp1.electricity_in', block: 'hp1', variable: 'electricity_in', unit: 'kW' })
  expect(device.series_id).not.toBe(system.series_id)
  const readAll = async (field: string) => {
    const values: number[] = []
    let start = 0
    for (;;) {
      const slice = await requestJson(page.request, 'GET', `/api/projects/${pid}/tasks/${taskId}/result/hourly?field=${encodeURIComponent(field)}&start=${start}&limit=701`)
      expect(slice.kind).toBe(field === system.series_id ? 'system' : 'device')
      expect(slice.total_rows).toBe(directory.rows)
      values.push(...slice.values)
      if (slice.next_start === null) return values
      start = slice.next_start
    }
  }
  const systemValues = await readAll(system.series_id)
  expect(systemValues.length).toBe(8760)
  expect(systemValues).toEqual(await readAll(device.series_id))

  const [excel] = await Promise.all([page.waitForEvent('download'),
    page.getByRole('button', { name: /导出 Excel/i }).click()])
  expect(await excel.failure()).toBeNull()
  const excelPath = `${process.env.E2E_OUTPUT_DIR}/n3-series.xlsx`
  await excel.saveAs(excelPath)

  // Change only the live draft. The old task and its frozen version must stay
  // tied to the submitted block, while the package carries both revisions.
  await page.goto(`/projects/${pid}/planning`)
  await expect(editor).toBeEnabled()
  const changed = JSON.parse(source)
  changed.system_equations.hp1.relations[0].expression += ' + 0'
  await editor.fill(JSON.stringify(changed, null, 2))
  await page.getByRole('button', { name: '保存系统方程', exact: true }).click()
  await expect(page.getByText('系统方程草稿已保存', { exact: true })).toBeVisible()
  await page.goto(`/projects/${pid}/results`)
  const [pkg] = await Promise.all([page.waitForEvent('download'),
    page.getByRole('button', { name: /导出完整项目包/ }).click()])
  expect(await pkg.failure()).toBeNull()
  const pkgPath = `${process.env.E2E_OUTPUT_DIR}/n3-project.zip`
  await pkg.saveAs(pkgPath)
  const readPackage = (path: string) => JSON.parse(execFileSync('python3', ['-c', [
    'import json,sys,zipfile',
    'z=zipfile.ZipFile(sys.argv[1])',
    'm=json.loads(z.read("manifest.json"))',
    'print(json.dumps({"draft":json.loads(z.read("draft.json"))["content"],',
    '"versions":[json.loads(z.read(p))["content"] for p in m["files"]["versions"]]}))',
  ].join('\n'), path], { encoding: 'utf8' }))
  const exported = readPackage(pkgPath)
  expect(exported.draft.system_equations).toEqual(changed.system_equations)
  expect(exported.draft.outputs).toEqual(document.outputs)
  expect(exported.versions).toEqual(expect.arrayContaining([
    expect.objectContaining({ system_equations: document.system_equations, outputs: document.outputs }),
  ]))
  expect(await readAll(system.series_id)).toEqual(systemValues)

  // Existing package import, through its public boundary, preserves both
  // current author input and historical system declarations without remapping
  // block names into device identities.
  const importer = uniqueName('n3-import')
  await createEngineer(setup.request, importer, password)
  const importerToken = await loginToken(setup.request, importer, password)
  const proposalResponse = await setup.request.post(`${APP}/api/projects/import`, {
    headers: { Authorization: `Bearer ${importerToken}` },
    multipart: { file: { name: 'n3-project.zip', mimeType: 'application/zip', buffer: readFileSync(pkgPath) } },
  })
  expect(proposalResponse.ok()).toBe(true)
  const proposal = (await proposalResponse.json()).proposal
  const imported = await requestJson(setup.request, 'POST', `/api/projects/import/${proposal.id}/confirm`, {}, importerToken)
  const restored = await requestJson(setup.request, 'GET', `/api/projects/${imported.project.id}`, undefined, importerToken)
  expect(restored.draft.content.system_equations).toEqual(changed.system_equations)
  expect(restored.draft.content.outputs).toEqual(document.outputs)
  const restoredPackage = await requestJson(setup.request, 'POST', `/api/projects/${imported.project.id}/exports/package`, undefined, importerToken)
  const downloaded = await setup.request.get(`${APP}/api/projects/${imported.project.id}/exports/package/download?token=${restoredPackage.token}`, {
    headers: { Authorization: `Bearer ${importerToken}` },
  })
  expect(downloaded.ok()).toBe(true)
  const roundtripPath = `${process.env.E2E_OUTPUT_DIR}/n3-roundtrip.zip`
  // Reuse Playwright's downloaded artifact through the HTTP save operation.
  const { writeFileSync } = await import('node:fs')
  writeFileSync(roundtripPath, await downloaded.body())
  expect(readPackage(roundtripPath).versions).toEqual(expect.arrayContaining([
    expect.objectContaining({ system_equations: document.system_equations, outputs: document.outputs }),
  ]))
  session.assertNoErrors([{ method: 'PUT', path: `/api/projects/${pid}/draft`, status: 409, times: 1 }])
  await context.close()
  await setup.close()
})
