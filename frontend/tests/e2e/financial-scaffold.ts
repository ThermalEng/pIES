// T3/缺席验收共享脚手架: 项目/画像/模型/装配/冻结/配置走公开 API,
// 评价确认/提交/结果查看走 UI。不注入前端状态, 不 mock 求解。
//
// 可选扩展(extraEnergyPrices/extraTariffBindings/includeElectricLoad/
// scenario)只做追加与缺省保持: 正收益售热场景追加售热价格与计费绑定、
// 可不建电负荷; 'pv_export' 场景换为"光伏上网 + 外购热"拓扑(售电计量口
// 落在 grid.electricity_export, 售热计量口 heatload.heat_in 无有限上界,
// 不满足精确 VAT 大 M 所需边界, 不用作计费端点); 缺省场景的命令顺序
// 与内容逐字不变。
//
// 改动本文件时须同步跑 financial-evaluation、financial-absence 与
// result-irr-status-r1r2 三个 spec(后两者不重跑现金流阳性, 但须保证
// 导入解析与缺省行为不变)。

import type { APIRequestContext } from '@playwright/test'

export const APP = process.env.E2E_APP_URL ?? 'http://web:80'

/** 场景: 缺省热泵+热负荷(售热计费); 'pv_export' 光伏上网+外购热。 */
export type ScaffoldScenario = 'heat_pump' | 'pv_export'

export interface ScaffoldOptions {
  /** 计算配置 outputs(默认 ['solution'])。 */
  outputs?: string[]
  /** planning.constraints 附加项(默认 {})。 */
  planningConstraints?: Record<string, { type: string; expression: string; enabled?: boolean }>
  /** 追加能源价格条目(公开 finance-profile energy_prices 同构), 并入画像(默认 {})。 */
  extraEnergyPrices?: Record<string, unknown>
  /** 追加计费绑定(公开 assembly tariff_bindings 同构), 并入装配(默认 {})。 */
  extraTariffBindings?: Record<string, unknown>
  /** 是否创建电负荷 load1(默认 true); 售热正收益场景可关闭。 */
  includeElectricLoad?: boolean
  /** 场景(默认 'heat_pump'); 'pv_export' 用光伏上网+外购热拓扑。 */
  scenario?: ScaffoldScenario
  /** 画像 finance_types(默认 heat_pump_system); 'pv_export' 场景用 pv_system。 */
  financeTypes?: Record<string, unknown>
}

/** 'pv_export' 场景的财务类别(光伏按 kWp 计建设费与年固定 O&M)。 */
const PV_FINANCE_TYPES = {
  pv_system: {
    upfront_capex: {
      linear: { capacity_kwp: { unit_cost: { value: '3000', unit: 'CNY/kWp' } } },
    },
    annual_fixed_om: {
      linear: { capacity_kwp: { unit_cost: { value: '30', unit: 'CNY/kWp·a' } } },
    },
  },
}

/** 'pv_export' 场景的能源价格(售电上网 + 购热; 基础画像无税则)。 */
const PV_ENERGY_PRICES = {
  elec_export: {
    carrier: 'electricity',
    direction: 'sale',
    kind: 'constant',
    value: { value: '0.6', unit: 'CNY/kWh' },
  },
  heat_import: {
    carrier: 'heat',
    direction: 'purchase',
    kind: 'constant',
    value: { value: '0.15', unit: 'CNY/kWh' },
  },
}

export function financeProfile(
  profileId: string,
  extraEnergyPrices: Record<string, unknown> = {},
  financeTypes: Record<string, unknown> = {
    heat_pump_system: {
      upfront_capex: {
        linear: { heat_kw: { unit_cost: { value: '2800', unit: 'CNY/kW' } } },
      },
      annual_fixed_om: {
        linear: { heat_kw: { unit_cost: { value: '28', unit: 'CNY/kW·a' } } },
      },
      period_variable_om: {
        linear: { consumed_kwh: { unit_cost: { value: '0.008', unit: 'CNY/kWh' } } },
      },
    },
  },
): Record<string, unknown> {
  return {
    schema: 'ies.finance-profile',
    schema_version: '3.0.0',
    profile: {
      id: profileId,
      region: 'CN-North',
      currency: 'CNY',
      base_year: 2025,
      price_basis: 'tax_inclusive',
      cost_method: 'fixed_plus_linear',
    },
    finance_types: financeTypes,
    energy_prices: {
      grid_import: {
        carrier: 'electricity',
        direction: 'purchase',
        kind: 'constant',
        value: { value: '0.7', unit: 'CNY/kWh' },
      },
      ...extraEnergyPrices,
    },
    taxes: {},
    // 3.0.0 必需显式节(空 = 无税务计算, 既有非税路径不变)。
    credit_pools: {},
    tax_rules: {},
  }
}

/** 缺省场景命令(热泵 + 热负荷 + 电网): 与历史装配逐字一致。 */
function heatPumpDevices(includeElectricLoad: boolean): unknown[] {
  return [
    {
      id: 'e2e-dev-grid1', unit: 'model', type: 'model.upsert_device',
      payload: {
        name: 'grid1', device_type: 'ies.device.grid_connection', kind: 'existing',
        params: { max_import_power_kw: 800, max_export_power_kw: 200 },
        position: { x: 0.0, y: 0.0 },
      },
    },
    // 电负荷(售热正收益场景关闭; 缺省与历史装配逐字一致)。
    ...(includeElectricLoad
      ? [
          {
            id: 'e2e-dev-load1', unit: 'model', type: 'model.upsert_device',
            payload: {
              name: 'load1', device_type: 'ies.device.electric_load', kind: 'new',
              params: { peak_power_kw: 200, electricity_demand: 50 },
              position: { x: 2.0, y: 0.0 },
            },
          },
        ]
      : []),
    {
      id: 'e2e-dev-hp1', unit: 'model', type: 'model.upsert_device',
      payload: {
        name: 'hp1', device_type: 'ies.device.heat_pump', kind: 'new',
        params: { rated_heat_kw: 600, cop: 3.5 },
        position: { x: 1.0, y: 1.0 },
      },
    },
    ...(includeElectricLoad
      ? [
          {
            id: 'e2e-conn-grid-load1', unit: 'model', type: 'model.upsert_connection',
            payload: {
              name: 'c-grid-load1', from_device: 'grid1', from_port: 'electricity_import',
              to_device: 'load1', to_port: 'electricity_in',
            },
          },
        ]
      : []),
    {
      id: 'e2e-conn-grid-hp1', unit: 'model', type: 'model.upsert_connection',
      payload: {
        name: 'c-grid-hp1', from_device: 'grid1', from_port: 'electricity_import',
        to_device: 'hp1', to_port: 'electricity_in',
      },
    },
    {
      id: 'e2e-dev-heatload', unit: 'model', type: 'model.upsert_device',
      payload: {
        name: 'heatload', device_type: 'ies.device.heat_load', kind: 'new',
        params: { heat_demand: 30 },
        position: { x: 3.0, y: 1.0 },
      },
    },
    {
      id: 'e2e-conn-hp-heat', unit: 'model', type: 'model.upsert_connection',
      payload: {
        name: 'c-hp-heat', from_device: 'hp1', from_port: 'heat_out',
        to_device: 'heatload', to_port: 'heat_in',
      },
    },
    {
      id: 'e2e-calc', unit: 'config', type: 'config.patch',
      payload: {
        mode: 'fixed_operation',
        generator: 'ies.generator.highs_milp@3.4.0',
        solver: 'ies.solver.highs@1.7.2',
        time_axis: { resolution: '1h', start: '2025-01-01T00:00:00Z' },
      },
    },
    {
      id: 'e2e-finbind', unit: 'assembly', type: 'assembly.set_finance_binding',
      payload: {
        instances: {
          grid1: { type: 'none' },
          ...(includeElectricLoad ? { load1: { type: 'none' } } : {}),
          heatload: { type: 'none' },
          hp1: {
            type: 'costed',
            finance_type: 'heat_pump_system',
            drivers: {
              heat_kw: { property: 'rated_heat_kw' },
              consumed_kwh: { interface: 'electricity_in', aggregation: 'integrate_positive' },
            },
          },
        },
      },
    },
    {
      id: 'e2e-tariff', unit: 'assembly', type: 'assembly.set_tariff_bindings',
      payload: {
        bindings: {
          grid_import: {
            price: 'grid_import', instance: 'grid1', interface: 'electricity_import',
            aggregation: 'integrate_positive',
          },
        },
      },
    },
  ]
}

/**
 * 'pv_export' 场景命令(光伏上网 + 外购热 + 既有热负荷)。
 *
 * 售电计量口落在 grid1.electricity_export(in 接口, 界由
 * max_export_power_kw 给出); 购热计量口落在既有燃气锅炉的 heat_out
 * (out 接口, 界由 rated_heat_kw 给出)。热负荷的 heat_in 无有限上界,
 * 不作为计费端点(精确 VAT 大 M 需税基有界)。
 */
function pvExportDevices(): unknown[] {
  return [
    {
      id: 'e2e-dev-grid1', unit: 'model', type: 'model.upsert_device',
      payload: {
        name: 'grid1', device_type: 'ies.device.grid_connection', kind: 'existing',
        params: { max_import_power_kw: 800, max_export_power_kw: 200 },
        position: { x: 0.0, y: 0.0 },
      },
    },
    {
      id: 'e2e-dev-pv1', unit: 'model', type: 'model.upsert_device',
      payload: {
        name: 'pv1', device_type: 'ies.device.pv', kind: 'new',
        params: { rated_capacity_kwp: 100, solar_irradiance: 500, ambient_temperature: 25 },
        position: { x: 1.0, y: 0.0 },
      },
    },
    {
      id: 'e2e-dev-boiler1', unit: 'model', type: 'model.upsert_device',
      payload: {
        name: 'boiler1', device_type: 'ies.device.gas_boiler', kind: 'existing',
        params: { rated_heat_kw: 50, thermal_efficiency: 0.9, lhv_kwh_per_m3: 9.97 },
        position: { x: 2.0, y: 1.0 },
      },
    },
    {
      id: 'e2e-dev-heatload', unit: 'model', type: 'model.upsert_device',
      payload: {
        name: 'heatload', device_type: 'ies.device.heat_load', kind: 'existing',
        params: { peak_heat_kw: 10, heat_demand: 10 },
        position: { x: 3.0, y: 1.0 },
      },
    },
    {
      id: 'e2e-conn-pv-grid', unit: 'model', type: 'model.upsert_connection',
      payload: {
        name: 'c-pv-grid', from_device: 'pv1', from_port: 'electric_out',
        to_device: 'grid1', to_port: 'electricity_export',
      },
    },
    {
      id: 'e2e-conn-boiler-heat', unit: 'model', type: 'model.upsert_connection',
      payload: {
        name: 'c-boiler-heat', from_device: 'boiler1', from_port: 'heat_out',
        to_device: 'heatload', to_port: 'heat_in',
      },
    },
    {
      id: 'e2e-calc', unit: 'config', type: 'config.patch',
      payload: {
        mode: 'fixed_operation',
        generator: 'ies.generator.highs_milp@3.4.0',
        solver: 'ies.solver.highs@1.7.2',
        time_axis: { resolution: '1h', start: '2025-01-01T00:00:00Z' },
      },
    },
    {
      id: 'e2e-finbind', unit: 'assembly', type: 'assembly.set_finance_binding',
      payload: {
        instances: {
          grid1: { type: 'none' },
          pv1: {
            type: 'costed',
            finance_type: 'pv_system',
            drivers: { capacity_kwp: { property: 'rated_capacity_kwp' } },
          },
          boiler1: { type: 'none' },
          heatload: { type: 'none' },
        },
      },
    },
    {
      id: 'e2e-tariff', unit: 'assembly', type: 'assembly.set_tariff_bindings',
      payload: {
        bindings: {
          elec_export: {
            price: 'elec_export', instance: 'grid1', interface: 'electricity_export',
            aggregation: 'integrate_positive',
          },
          heat_import: {
            price: 'heat_import', instance: 'boiler1', interface: 'heat_out',
            aggregation: 'integrate_positive',
          },
        },
      },
    },
  ]
}

export async function scaffoldProject(
  requestJson: (
    api: APIRequestContext,
    method: string,
    url: string,
    body: unknown,
    token: string,
  ) => Promise<any>,
  api: APIRequestContext,
  token: string,
  projectName: string,
  profileId: string,
  opts: ScaffoldOptions = {},
): Promise<number> {
  const scenario: ScaffoldScenario = opts.scenario ?? 'heat_pump'
  const created = await requestJson(api, 'POST', '/api/projects', {
    name: projectName,
    baseline_resolution: '1h',
    baseline_start_year: 2025,
    baseline_scenario_mode: 'single',
  }, token)
  const pid: number = created.project?.id ?? created.id
  const financeTypes = opts.financeTypes ?? (
    scenario === 'pv_export' ? PV_FINANCE_TYPES : undefined
  )
  const energyPrices = scenario === 'pv_export'
    ? {
      ...PV_ENERGY_PRICES,
      ...(opts.extraEnergyPrices ?? {}),
    }
    : (opts.extraEnergyPrices ?? {})
  await requestJson(api, 'POST', '/api/finance-profiles', {
    finance_profile: financeProfile(profileId, energyPrices, financeTypes),
  }, token)
  await requestJson(api, 'PUT', `/api/projects/${pid}/finance-profile`, {
    profile_ref: { id: profileId },
  }, token)

  const view = await requestJson(api, 'GET', `/api/projects/${pid}`, undefined, token)
  const includeElectricLoad = opts.includeElectricLoad !== false
  const devices = scenario === 'pv_export'
    ? pvExportDevices()
    : heatPumpDevices(includeElectricLoad)
  if (opts.extraTariffBindings) {
    // 追加计费绑定只并入既有绑定集合, 不改既有条目。
    const tariff = devices.find(
      (cmd) => (cmd as { type?: string }).type === 'assembly.set_tariff_bindings',
    ) as { payload: { bindings: Record<string, unknown> } } | undefined
    if (!tariff) throw new Error('脚手架缺少计费绑定命令')
    tariff.payload.bindings = { ...tariff.payload.bindings, ...opts.extraTariffBindings }
  }
  let rev: number = view.draft.revision
  const put = await requestJson(api, 'PUT', `/api/projects/${pid}/draft`, {
    expected_revision: rev, commands: devices,
  }, token)
  rev = put.revision

  await requestJson(api, 'PUT', `/api/projects/${pid}/planning-config`, {
    planning_config: scenario === 'pv_export'
      ? {
        objective: { sense: 'maximize', expression: 'system.after_tax_npv' },
        variables: {
          pv_cap: {
            device_ref: 'pv1', parameter: 'rated_capacity_kwp',
            lower_bound: '50', upper_bound: '200', unit: 'kWp',
          },
        },
        constraints: opts.planningConstraints ?? {},
      }
      : {
        objective: {
          sense: 'minimize',
          expression: 'grid_import__period_energy_purchase + hp1__upfront_capex + 1 year * hp1__annual_fixed_om + hp1__period_variable_om',
        },
        variables: {
          hp_cap: {
            device_ref: 'hp1', parameter: 'rated_heat_kw',
            lower_bound: '50', upper_bound: '1000', unit: 'kW',
          },
        },
        constraints: opts.planningConstraints ?? {},
      },
    expected_revision: null,
  }, token)
  await requestJson(api, 'POST', `/api/projects/${pid}/versions`, {
    name: '计算基线', reason: 'snapshot_freeze', expected_revision: rev,
  }, token)
  // 预检门禁要求项目绑定数据集版本: 生成内置样例并绑定(恒定负荷不消费
  // 该数据, 绑定仅满足项目级数据可用门禁; 数据驱动路径由后端链测试覆盖)。
  const ds = await requestJson(api, 'POST', `/api/projects/${pid}/datasets`, {
    name: 'e2e-sample',
  }, token)
  const dsid: number = ds.dataset?.id ?? ds.id
  const sampled = await requestJson(
    api, 'POST', `/api/projects/${pid}/datasets/${dsid}/sample?resolution=1h`, {}, token,
  )
  const dvid: number = sampled.dataset_version?.id
  const bindPut = await requestJson(api, 'PUT', `/api/projects/${pid}/draft`, {
    expected_revision: rev,
    commands: [{
      id: 'e2e-bind-data', unit: 'dataset', type: 'dataset.bind',
      payload: { dataset_version_id: dvid, role: 'main' },
    }],
  }, token)
  rev = bindPut.revision
  // 提交预检门禁要求已确认财务基准(与校验页一致), 由 API 记录确认。
  await requestJson(api, 'POST', `/api/projects/${pid}/validation/baseline-confirm`, {
    assumptions: {},
  }, token)
  await requestJson(api, 'PUT', `/api/projects/${pid}/config`, {
    calculation_config: {
      schema: 'ies.calculation-config',
      schema_version: '2.0.0',
      mode: 'planning',
      predictions: [],
      generator: 'ies.generator.highs_milp@3.4.0',
      solver: 'ies.solver.highs@1.7.2',
      precision: 'medium',
      convergence: null,
      time_limit_seconds: 600,
      options: {},
      seed: null,
      outputs: opts.outputs ?? ['solution'],
    },
    expected_revision: null,
  }, token)
  return pid
}
