/**
 * E5a 开放生态纯映射、YAML 表单双向同步、Schema 参数抽取与 i18n 契约单测。
 */

import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
globalThis.require = createRequire(import.meta.url)

const {
  applyDeviceFormToText,
  formatArtifactDiagnostics,
  mergeCatalogEntries,
  parseDeviceFormFromText,
  resolveArtifactLifecycleStatus,
} = await import('../src/features/customization/index.ts')
const { addPredictionEntry, coerceSchemaFieldValue, extractSchemaFields } = await import('../src/features/config/index.ts')
const { PAGE_MESSAGES_EN, PAGE_MESSAGES_ZH, pt } = await import('../src/i18n/pageMessages.ts')
const { messagesEn } = await import('../src/i18n/messages_en.ts')
const { messagesZh } = await import('../src/i18n/messages_zh.ts')

// 1. i18n keys parity for E5a customization, admin ecosystem, and config catalog
for (const key of ['ies.nav.admin_ecosystem', 'ies.result.evaluator_retry_btn']) {
  assert.ok(messagesZh[key], `missing zh key ${key}`)
  assert.ok(messagesEn[key], `missing en key ${key}`)
}
for (const key of [
  'ies.eco.page_title',
  'ies.eco.tab_mine',
  'ies.eco.tab_market',
  'ies.eco.state_remote',
  'ies.eco.state_local_uninstalled',
  'ies.eco.state_local_installed',
  'ies.eco.market_load_error',
  'ies.eco.submit_market_btn',
  'ies.admin_eco.title',
  'ies.admin_eco.market_mode',
  'ies.admin_eco.approval_switch',
  'ies.admin_eco.approve',
  'ies.admin_eco.reject',
  'ies.admin_eco.uninstall_cleanup',
  'ies.admin_eco.uninstall_in_use',
  'ies.config_eco.title',
  'ies.config_eco.add_evaluator',
  'ies.config_eco.add_predictor',
]) {
  assert.ok(PAGE_MESSAGES_ZH[key], `missing zh pageMessage ${key}`)
  assert.ok(PAGE_MESSAGES_EN[key], `missing en pageMessage ${key}`)
  assert.notEqual(pt(key), key)
}

// 2. resolveArtifactLifecycleStatus covers approval_status x install_status
assert.equal(resolveArtifactLifecycleStatus({ approval_status: 'pending', install_status: 'building' }), 'pending_approval')
assert.equal(resolveArtifactLifecycleStatus({ approval_status: 'rejected', install_status: 'ready' }), 'rejected')
assert.equal(resolveArtifactLifecycleStatus({ approval_status: 'approved', install_status: 'building' }), 'building')
assert.equal(resolveArtifactLifecycleStatus({ approval_status: 'approved', install_status: 'testing' }), 'building')
assert.equal(resolveArtifactLifecycleStatus({ approval_status: 'approved', install_status: 'build_failed' }), 'build_failed')
assert.equal(resolveArtifactLifecycleStatus({ approval_status: 'approved', install_status: 'disabled' }), 'disabled')
assert.equal(resolveArtifactLifecycleStatus({ approval_status: 'approved', install_status: 'ready' }), 'ready')

// 3. mergeCatalogEntries handles local-only mode vs online three-state catalog without duplicates
{
  const localArtifacts = [
    {
      id: 1,
      kind: 'evaluator',
      stable_id: 'acme.carbon',
      version: '1.0.0',
      source_origin: 'market',
      approval_status: 'approved',
      install_status: 'ready',
      enabled: true,
      installed: true,
      sha256: 'a'.repeat(64),
      metadata: { names: { 'zh-CN': '碳排放评价', 'en-US': 'Carbon Eval' } },
      diagnostics: null,
      created_at: '2026-11-28T00:00:00Z',
    },
    {
      id: 2,
      kind: 'device_model',
      stable_id: 'acme.chiller',
      version: '2',
      source_origin: 'local_upload',
      approval_status: 'approved',
      install_status: 'ready',
      enabled: false,
      installed: false,
      sha256: 'b'.repeat(64),
      metadata: { names: { 'zh-CN': '冷水机', 'en-US': 'Chiller' } },
      diagnostics: null,
      created_at: '2026-11-28T00:00:00Z',
    },
    {
      id: 3,
      kind: 'solver',
      stable_id: 'acme.pending',
      version: '0.1.0',
      source_origin: 'local_upload',
      approval_status: 'pending',
      install_status: 'ready',
      enabled: false,
      installed: false,
      sha256: 'c'.repeat(64),
      metadata: {},
      diagnostics: null,
      created_at: '2026-11-28T00:00:00Z',
    },
  ]

  const marketItems = [
    {
      id: 'acme.carbon',
      kind: 'evaluator',
      version: '1.1.0',
      name: { zh_cn: '碳排放评价', en_us: 'Carbon Eval' },
      description: 'Remote carbon evaluator',
      publisher: 'acme',
      license: 'MIT',
      compatibility: { sdk: '>=1.0.0' },
      consumes: ['solution'],
      produces: ['report'],
      sha256: 'd'.repeat(64),
      size_bytes: 2048,
      member_state: 'remote',
      update_available: true,
    },
    {
      id: 'acme.wind',
      kind: 'predictor',
      version: '1.2.0',
      name: { zh_cn: '风电预测', en_us: 'Wind Predictor' },
      description: 'Wind forecasting old',
      publisher: 'acme',
      license: 'Apache-2.0',
      compatibility: {},
      consumes: [],
      produces: ['wind_cf'],
      sha256: 'f'.repeat(64),
      size_bytes: 900,
      member_state: 'remote',
      update_available: false,
    },
    {
      id: 'acme.wind',
      kind: 'predictor',
      version: '1.10.0',
      name: { zh_cn: '风电预测', en_us: 'Wind Predictor' },
      description: 'Wind forecasting',
      publisher: 'acme',
      license: 'Apache-2.0',
      compatibility: {},
      consumes: [],
      produces: ['wind_cf'],
      sha256: 'e'.repeat(64),
      size_bytes: 1024,
      member_state: 'remote',
      update_available: false,
    },
    {
      id: 'acme.device.boiler',
      kind: 'device_model',
      revision: 1,
      name: { zh_cn: '锅炉', en_us: 'Boiler' },
      description: 'Old boiler',
      publisher: 'acme',
      license: 'MIT',
      compatibility: {},
      consumes: [],
      produces: [],
      sha256: '1'.repeat(64),
      size_bytes: 512,
      member_state: 'remote',
      update_available: false,
    },
    {
      id: 'acme.device.boiler',
      kind: 'device_model',
      revision: 3,
      name: { zh_cn: '锅炉', en_us: 'Boiler' },
      description: 'Latest boiler',
      publisher: 'acme',
      license: 'MIT',
      compatibility: {},
      consumes: [],
      produces: [],
      sha256: '2'.repeat(64),
      size_bytes: 600,
      member_state: 'remote',
      update_available: false,
    },
  ]

  const localOnly = mergeCatalogEntries({ localArtifacts, marketItems, marketMode: 'local' })
  assert.equal(localOnly.length, 2, 'pending and remote-only entries excluded in local mode')
  assert.equal(localOnly[0].memberState, 'local_installed')
  assert.equal(localOnly[1].memberState, 'local_uninstalled')
  assert.equal(localOnly[1].versionOrRevision, 'r2')

  const onlineMerged = mergeCatalogEntries({ localArtifacts, marketItems, marketMode: 'online' })
  assert.equal(onlineMerged.length, 4, 'historical remote versions must collapse before merge')
  const byId = Object.fromEntries(onlineMerged.map((e) => [e.id, e]))
  assert.equal(byId['acme.carbon'].memberState, 'local_installed')
  assert.equal(byId['acme.carbon'].updateAvailable, true)
  assert.equal(byId['acme.wind'].memberState, 'remote')
  assert.equal(byId['acme.wind'].versionOrRevision, '1.10.0', 'semver collapse must pick 1.10.0 over 1.2.0')
  assert.equal(byId['acme.device.boiler'].versionOrRevision, 'r3', 'revision collapse must pick r3 over r1')
  assert.equal(byId['acme.chiller'].memberState, 'local_uninstalled')

  // Unready / disabled local rows must NOT override remote member_state to local_installed/local_uninstalled
  for (const [label, patch] of [
    ['pending', { approval_status: 'pending', install_status: 'ready', installed: false }],
    ['building', { approval_status: 'approved', install_status: 'building', installed: false }],
    ['build_failed', { approval_status: 'approved', install_status: 'build_failed', installed: false }],
    ['disabled', { approval_status: 'approved', install_status: 'disabled', installed: true }],
  ]) {
    const gated = mergeCatalogEntries({
      localArtifacts: [
        {
          id: 99,
          kind: 'predictor',
          stable_id: 'acme.wind',
          version: '1.10.0',
          source_origin: 'market',
          ...patch,
          enabled: false,
          sha256: 'e'.repeat(64),
          metadata: {},
          diagnostics: null,
          created_at: '2026-11-28T00:00:00Z',
        },
      ],
      marketItems,
      marketMode: 'online',
    })
    const wind = gated.find((e) => e.id === 'acme.wind')
    assert.ok(wind, `${label} entry should still appear via market index`)
    assert.equal(wind.memberState, 'remote', `${label} local state must not promote memberState`)
    assert.ok(wind.localArtifact, `${label} localArtifact metadata should stay attached for lifecycle badge`)
  }
}

// 4. Two-way device YAML <-> basic form sync preserves equations & inputs
{
  const srcYaml = `schema: ies.device-model
schema_version: "1.0.0"
device:
  id: demo-boiler
  names:
    zh-CN: 示例锅炉
    en-US: Demo Boiler
inputs:
  properties:
    cap_kw:
      value:
        type: number
properties:
  efficiency:
    value: 0.92
    unit: "1"
    valid_range:
      minimum: 0.5
      maximum: 1.0
interfaces:
  heat_out:
    type: out
    carrier: heat
    unit: kW
  internal_flux:
    carrier: heat
    unit: kW
equations:
  variables:
    q_out:
      unit: kW
  relations:
    - "q_out == efficiency * fuel_in"
`
  const parsed = parseDeviceFormFromText(srcYaml)
  assert.equal(parsed.valid, true)
  assert.equal(parsed.deviceId, 'demo-boiler')
  assert.equal(parsed.nameZh, '示例锅炉')
  assert.equal(parsed.properties[0].value, '0.92')
  assert.equal(parsed.interfaces[0].carrier, 'heat')
  assert.equal(parsed.interfaces[1].type, 'blind', 'omitted interface type must normalize to blind')

  const updatedYaml = applyDeviceFormToText(srcYaml, {
    deviceId: 'demo-boiler-v2',
    nameZh: '燃气锅炉',
    nameEn: 'Gas Boiler',
    properties: [{ name: 'efficiency', value: '0.95', unit: '1', min: '0.5', max: '1' }],
    interfaces: [
      { name: 'heat_out', type: 'out', carrier: 'heat', unit: 'kW' },
      { name: 'gas_in', type: 'bidirectional', carrier: 'fuel_gas', unit: 'kW' },
      { name: 'hidden_bus', type: '', carrier: 'heat', unit: 'kW' },
    ],
  })
  assert.ok(updatedYaml.includes('q_out == efficiency * fuel_in'), 'equations must be preserved')
  assert.ok(updatedYaml.includes('cap_kw'), 'inputs must be preserved')
  const reparsed = parseDeviceFormFromText(updatedYaml)
  assert.equal(reparsed.deviceId, 'demo-boiler-v2')
  assert.equal(reparsed.properties[0].value, '0.95')
  assert.equal(reparsed.interfaces.length, 3)
  assert.equal(reparsed.interfaces[1].type, 'bidirectional')
  assert.equal(reparsed.interfaces[2].type, 'blind')
}

// 5. formatArtifactDiagnostics
assert.equal(formatArtifactDiagnostics(null), null)
assert.equal(formatArtifactDiagnostics({ rejected_reason: 'Unsafe AST import' }), 'Unsafe AST import')
assert.equal(formatArtifactDiagnostics({ error: 'pip failed' }), 'pip failed')
assert.equal(formatArtifactDiagnostics({ message: 'missing manifest' }), 'missing manifest')

// 6. extractSchemaFields + coerceSchemaFieldValue
{
  const schema = {
    type: 'object',
    required: ['alpha'],
    properties: {
      alpha: { type: 'number', title: 'Alpha', default: 0.5 },
      retries: { type: 'integer', default: 3 },
      strict: { type: 'boolean', default: false },
      mode: { type: 'string', enum: ['fast', 'exact'] },
    },
  }
  const fields = extractSchemaFields(schema)
  assert.equal(fields.length, 4)
  assert.equal(fields[0].required, true)
  assert.deepEqual(fields[3].enumValues, ['fast', 'exact'])
  assert.equal(coerceSchemaFieldValue(fields[0], '1.25'), 1.25)
  assert.equal(coerceSchemaFieldValue(fields[0], ''), undefined)
  assert.equal(coerceSchemaFieldValue(fields[1], '4'), 4)
  assert.equal(coerceSchemaFieldValue(fields[2], true), true)
  assert.equal(coerceSchemaFieldValue(fields[2], false), false)
  assert.equal(coerceSchemaFieldValue(fields[3], 'exact'), 'exact')
}

// 7. addPredictionEntry follows CalculationConfig 1.0.0 {target: {instance, interface}, algorithm, parameters}
{
  const list1 = addPredictionEntry([], {
    instance: 'pv1',
    interfaceName: 'power_out',
    algorithm: 'ies.predictor.naive@1.0.0',
  })
  assert.deepEqual(list1, [
    {
      target: { instance: 'pv1', interface: 'power_out' },
      algorithm: 'ies.predictor.naive@1.0.0',
      parameters: {},
    },
  ])
  const dup = addPredictionEntry(list1, {
    instance: 'pv1',
    interfaceName: 'power_out',
    algorithm: 'ies.predictor.other@1.0.0',
  })
  assert.equal(dup.length, 1, 'duplicate {instance, interface} target must be ignored')
}

console.log('ALL E5A ECOSYSTEM MAPPER CHECKS PASSED')
