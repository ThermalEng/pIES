// D3 税务主链画像(ies.finance-profile@6.0.0 合成税则, 显式税率/池/退款)。
//
// 场景: 光伏余电上网(售电计量口 grid1.electricity_export, 界由
// max_export_power_kw 给出) + 外购热(购热计量口 boiler1.heat_out, 界由
// rated_heat_kw 给出)。热负荷 heat_in 无有限上界, 不作计费端点。
// 金额口径: 不含税(price_basis: tax_exclusive), 已声明税率部分的实际
// 收付款按价税合计(×(1+税率)); 维护清单金额恒为实际含税付款。
const taxProfileTemplate = {
  credit_pools: {
    main_pool: {
      opening_credit: { unit: 'CNY', value: '0' },
      receivable_policy: 'end_receivable',
      refunds: [
        // 确认期与到账期分离: m14 确认/m16 到账(期内现金), m20 确认/m40
        // 到账(评价期后应收, 不计期内现金)。
        { amount: { unit: 'CNY', value: '500' }, arrival_period: 16, period: 14 },
        { amount: { unit: 'CNY', value: '1500' }, arrival_period: 40, period: 20 },
      ],
    },
  },
  energy_prices: {
    elec_export: {
      carrier: 'electricity',
      direction: 'sale',
      kind: 'constant',
      value: { unit: 'CNY/kWh', value: '0.6' },
    },
    heat_import: {
      carrier: 'heat',
      direction: 'purchase',
      kind: 'constant',
      value: { unit: 'CNY/kWh', value: '0.15' },
    },
  },
  demand_charges: {},
  finance_types: {
    pv_system: {
      annual_fixed_om: {
        linear: {
          capacity_kwp: { unit_cost: { unit: 'CNY/kWp·a', value: '30' } },
        },
      },
      upfront_capex: {
        linear: {
          capacity_kwp: { unit_cost: { unit: 'CNY/kWp', value: '3000' } },
        },
      },
    },
  },
  profile: {
    base_year: 2025,
    cost_method: 'fixed_plus_linear',
    currency: 'CNY',
    id: 'cn-pv-tax-e2e',
    price_basis: 'tax_exclusive',
    region: '合成税务验收区',
  },
  schema: 'ies.finance-profile',
  schema_version: '1.0.0',
  net_cashflows: {},
  tax_rules: {
    building_vat: {
      confirmation: { method: 'payment_period' },
      credit_pool: 'main_pool',
      deductible_ratio: { unit: '1', value: '1' },
      for: 'pv_system.upfront_capex',
      kind: 'construction_input_vat',
      label_ref: 'vat_label',
      rate: { unit: '1', value: '0.13' },
    },
    income: {
      annual_om: ['pv_system.annual_fixed_om'],
      deductible_maintenance: {
        items: [
          { deductible: true, position: 1, year: 2 },
          { deductible: false, position: 1, year: 3 },
        ],
        mode: 'explicit_items',
      },
      depreciation: [
        {
          base_from: { kind: 'upfront_capex_net', finance_type: 'pv_system', scope: { kind: 'all_new_instances' } },
          first_year: 1,
          life_years: 20,
          method: 'straight_line',
          residual: { unit: 'CNY', value: '0' },
        },
      ],
      energy_cost: ['heat_import'],
      kind: 'income_tax',
      label_ref: 'income_label',
      loss_carryforward_years: 5,
      opening_losses: [],
      rate: { unit: '1', value: '0.25' },
      revenue: ['elec_export'],
      variable_om: [],
    },
    operating_vat: {
      confirmation: 'same_period',
      credit_pool: 'main_pool',
      in_tax: [
        {
          deductible_ratio: { unit: '1', value: '1' },
          price: 'heat_import',
          rate: { unit: '1', value: '0.13' },
        },
      ],
      kind: 'vat',
      label_ref: 'vat_label',
      maintenance_input_tax: [
        {
          deductible_ratio: { unit: '1', value: '1' },
          position: 1,
          rate: { unit: '1', value: '0.13' },
          year: 2,
        },
      ],
      om_input_tax: [
        {
          deductible_ratio: { unit: '1', value: '1' },
          for: 'pv_system.annual_fixed_om',
          rate: { unit: '1', value: '0.06' },
        },
      ],
      out_tax: [
        { price: 'elec_export', rate: { unit: '1', value: '0.13' } },
      ],
      settlement: 'monthly',
    },
  },
  taxes: {
    income_label: {
      applies_to: 'elec_export',
      display_name: '合成所得税',
      tax_type: 'income_tax',
    },
    vat_label: {
      applies_to: 'elec_export',
      display_name: '增值税',
      tax_type: 'value_added_tax',
    },
  },
}

export function taxProfile(profileId: string) {
  return {
    ...JSON.parse(JSON.stringify(taxProfileTemplate)) as typeof taxProfileTemplate,
    profile: { ...taxProfileTemplate.profile, id: profileId },
  }
}
