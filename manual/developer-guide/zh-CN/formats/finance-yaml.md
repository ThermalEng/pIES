# 财务 YAML 契约

> 契约标识：`ies.finance-profile`、`ies.finance-overrides`、`ies.effective-finance-config`；目标 `schema_version`：`"1.0.0"`；项目包推荐文件名：`finance_profile.yaml`、`finance_overrides.yaml`、`effective_finance.yaml`

本页是财务三件套（地区财务基准 `FinanceProfile`、项目稀疏覆盖 `FinanceOverrides` 与确定性合并快照 `EffectiveFinanceConfig`）的唯一字段级正文。整体财务边界与规划/评价规则见[财务计算](../modules/finance.md)，配套版本规则见[单一当前契约](../file-formats.md#单一当前契约与项目包依赖)。

```text
FinanceProfile（地区财务基准，可人工编写）
FinanceOverrides（项目覆盖，可人工编写）
        │
        ▼ 确定性合并 + 领域引用校验
EffectiveFinanceConfig（不可变快照，禁止人工编写）
        │
        ▼ 精确引用
assembly / computation / 项目包
```

- **金额原子**：所有成本与费率均写作 `{value, unit}`，`value` 为十进制定点字符串（禁止 `float`、`NaN`/`Infinity`、`null`），`unit` 来自公共单位词汇。成本分量金额非负（允许 `"0"`）；能源价格允许正、零与负的有限十进制值。
- **独立引用原则**：财务配置可导出和随项目包迁移，导入时须携带来源 `FinanceProfile` 与 `FinanceOverrides` 并从来源重新合并校验。

## 通用金额引用目标语义

除基础成本分量、能源价格与基础税则外，扩展财务关系（命名明细、`amounts`、`cash_movements`、`assets` 与 `derived_tax`）的字段定义均以[命名金额、支付与资产](finance-relations.md)为唯一正文，遵守以下原则：

- **身份与发生分离**：费用项的稳定身份与所引用的规模/运行 `driver` 分离，多个费用项可共享同一 `driver` 而保持各自费率、含税口径与抵扣规则；纯数值金额配方（`amounts`）与实际收付发生（`cash_movements`）、资本化资产（`assets`）分离，不因数值引用而重复扣减现金或多次税前扣除。
- **显式口径与期间**：金额公式由带单位常数、已知系数乘显式来源及其有限和构成，保留含税、不含税、可抵进项及净基础标识，并严格区分标量、评价年与税务结算月。
- **派生税费与建设条件**：派生税费按指定税额输出乘已知费率计取，不按税目名称推断税基；依赖选建开关触发的费用须通过[费用发生条件](assembly-yaml.md#费用发生条件)显式绑定建设单元。

| 语义示意 | 处理规则 |
|---|---|
| 同容量 `q` 分别驱动材料费 `2 CNY/kW/a × q` 与服务费 `5 CNY/kW/a × q` | 保留各自明细身份与独立进项抵扣规则，不合并成单一无来源叶子 |
| 周转金 `9 CNY/kW × P` 于第 1 年支出、第 4 年回收 | 两次发生项引用同一金额配方，回收显式声明，不由折旧残值推断 |
| 附加税按 `0.04 × 某规则当期实缴税额` 计取 | 绑定 `paid` 实缴输出而非销项或留抵余额，滞后只影响实付时点 |
| 资产组合来自初建子项净额与指定期资本化支出 | 保留各自发生来源、分配份额及最早可折旧年度 |

## `ies.finance-profile@1.0.0`

### 完整示例

```yaml
schema: ies.finance-profile
schema_version: "1.0.0"
profile:
  id: cn-north-demo
  region: CN-North
  currency: CNY
  base_year: 2025
  price_basis: tax_inclusive   # 金额口径；是否计税及适用税率由 tax_rules 显式声明
  cost_method: fixed_plus_linear
finance_types:
  pv_system:
    upfront_capex:
      fixed: {value: "0", unit: CNY}
      linear:
        capacity_kw: {unit_cost: {value: "3500", unit: CNY/kW}}
    annual_fixed_om:
      fixed: {value: "1200", unit: CNY/a}
      linear:
        capacity_kw: {unit_cost: {value: "35", unit: CNY/kW/a}}
    period_variable_om:
      linear:
        generated_kwh: {unit_cost: {value: "0.01", unit: CNY/kWh}}
  battery_system:
    upfront_capex:
      linear:
        energy_kwh: {unit_cost: {value: "900", unit: CNY/kWh}}
    annual_fixed_om:
      linear:
        energy_kwh: {unit_cost: {value: "18", unit: CNY/kWh/a}}
    period_variable_om:
      linear:
        cycled_kwh: {unit_cost: {value: "0.002", unit: CNY/kWh}}
energy_prices:
  grid_import:                  # price_id：稳定自定义名，lower_snake_case、不含 __
    carrier: electricity        # carrier 取值来自公共载体词汇（与设备接口 carrier 同一词汇源，本页不复制清单）
    direction: purchase         # 显式方向：purchase | sale；不靠 price_id/键名推断
    kind: constant
    value: {value: "0.7", unit: CNY/kWh}
  pv_export:
    carrier: electricity
    direction: sale
    kind: time_series
    ref: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    series_meta:
      resolution: 1h
      point_count: 8760  # 单年示例（运营年限由提交时显式确认，此处取 1；非默认）；实际必须等于全周期总点数
      unit: CNY/kWh
  heat_supply:                  # 第二载体示例：购热价格条目（heat 为设备接口示例已用的载体取值）
    carrier: heat
    direction: purchase
    kind: constant
    value: {value: "0.35", unit: CNY/kWh}
demand_charges: {}
taxes:
  grid_import_vat:
    display_name: 购电增值税
    tax_type: value_added_tax
    applies_to: grid_import     # 引用 price_id（energy_prices 键）或 finance_types 分量路径
credit_pools: {}
tax_rules: {}                  # 本例只有展示登记，没有启用税务计算规则
net_cashflows: {}              # 无额外净现金项目，不从折旧残值自动生成回款
```

### 字段表

| 路径 | 必需 | 规则 |
|---|---|---|
| `schema` / `schema_version` | 是 | 固定 `ies.finance-profile` / `"1.0.0"` |
| `profile.id` | 是 | 稳定小写标识（如 `cn-north-demo`），供 Overrides 与 Effective 引用 |
| `profile.region` | 是 | 地区展示标识，不参与数值计算 |
| `profile.currency` | 是 | `CNY` / `USD` |
| `profile.base_year` | 是 | 整数财务基准年（1900–2999），与运行起始年 `start_year` 独立 |
| `profile.price_basis` | 是 | `tax_inclusive` / `tax_exclusive`：标明文件金额是否含税 |
| `profile.cost_method` | 是 | 固定为 `fixed_plus_linear` |
| `finance_types` | 是 | `finance_type` → 成本分量块（至少含 `upfront_capex`、`annual_fixed_om`、`period_variable_om` 之一） |
| `finance_types.*.upfront_capex` | 可选 | 建设期一次性成本（仅 `new` 计入）：`fixed` 量纲为币种，`linear.<driver>.unit_cost` 为 `currency / driver 单位` |
| `finance_types.*.annual_fixed_om` | 可选 | 年固定运维：`fixed` 量纲为 `currency/year`（如 `CNY/a`），`linear.<driver>.unit_cost` 为 `currency / driver 单位 / year` |
| `finance_types.*.period_variable_om` | 可选 | 运行期可变运维：`linear.<driver>.unit_cost` 为 `currency / 运行量单位`（如 `CNY/kWh`） |
| `energy_prices` | 是 | `price_id`（`lower_snake_case`，不含 `__`）→ `{carrier, direction, kind, ...}` |
| `energy_prices.*.carrier` / `direction` | 是 | `carrier` 来自设备公共载体词汇；`direction` 为 `purchase` 或 `sale` |
| `energy_prices.*` 定价联合 | 是 | 三选一：`{kind: constant, value: Money}`、`{kind: time_series, ref, series_meta}` 或 `{kind: monthly_daily, months}` |
| `energy_prices.*.series_meta` | 序列必填 | `{resolution, point_count, unit}`；全期序列须在提交边界匹配已确认运营总点数 |
| `demand_charges` | 是 | 需量费项映射，无费项填 `{}`；详见「自然月需量费」 |
| `taxes` | 可选 | 展示税目标签 `{display_name, tax_type, applies_to}`，不直接触发计算 |
| `credit_pools` / `tax_rules` | 是 | 抵扣池与计算规则映射，无规则时显式 `{}`；详见「税务计算输入」 |
| `net_cashflows` | 是 | 额外税后净现金项目映射，无项目时 `{}` |
| `finance_types.*.<comp>.items` / `amounts` / `cash_movements` / `assets` | 可选 | 命名财务关系扩展节，字段见[命名金额、支付与资产](finance-relations.md) |

### 月份日价格

`energy_prices.<id>` 选择 `kind: monthly_daily` 时，`months` 必须完整包含 `"1"` 至 `"12"` 共 12 个字符串键，每项为 `{ref, series_meta}`，指向通过上传校验的 `ies.price-series@1.0.0` 完整一日曲线（`1h` 为 24 点、`30min` 为 48 点、`15min` 为 96 点）。多个月份可复用同一对象 ID；执行阶段按公历运营年月选取对应日曲线（闰年 2 月 29 日沿用 2 月曲线）。

```yaml
carrier: electricity
direction: purchase
kind: monthly_daily
months:
  "1": {ref: "day-price-a", series_meta: {resolution: 1h, point_count: 24, unit: CNY/kWh}}
  "2": {ref: "day-price-a", series_meta: {resolution: 1h, point_count: 24, unit: CNY/kWh}}
  "3": {ref: "day-price-a", series_meta: {resolution: 1h, point_count: 24, unit: CNY/kWh}}
  "4": {ref: "day-price-a", series_meta: {resolution: 1h, point_count: 24, unit: CNY/kWh}}
  "5": {ref: "day-price-a", series_meta: {resolution: 1h, point_count: 24, unit: CNY/kWh}}
  "6": {ref: "day-price-b", series_meta: {resolution: 1h, point_count: 24, unit: CNY/kWh}}
  "7": {ref: "day-price-b", series_meta: {resolution: 1h, point_count: 24, unit: CNY/kWh}}
  "8": {ref: "day-price-b", series_meta: {resolution: 1h, point_count: 24, unit: CNY/kWh}}
  "9": {ref: "day-price-b", series_meta: {resolution: 1h, point_count: 24, unit: CNY/kWh}}
  "10": {ref: "day-price-a", series_meta: {resolution: 1h, point_count: 24, unit: CNY/kWh}}
  "11": {ref: "day-price-a", series_meta: {resolution: 1h, point_count: 24, unit: CNY/kWh}}
  "12": {ref: "day-price-a", series_meta: {resolution: 1h, point_count: 24, unit: CNY/kWh}}
```

### 税务计算输入

`credit_pools` 与 `tax_rules` 声明参与规划求解与同次评价的税务机制（规则 ID 与池 ID 均为 `lower_snake_case`）：

| 位置 | 结构与说明 |
|---|---|
| `credit_pools.<id>` | `{opening_credit: Money, receivable_policy: end_receivable, refunds: [{period, amount, arrival_period}]}`；`period` 与 `arrival_period` 为正整数税务月（`arrival_period >= period`），`end_receivable` 表示评价期后到账的期末应收不计入期内现金 |
| `kind: vat` | `{label_ref, settlement: monthly, in_tax, out_tax, om_input_tax, maintenance_input_tax, demand_input_tax?, credit_pool, confirmation: same_period}` |
| `in_tax[]` / `out_tax[]` | `{price, rate, deductible_ratio}`（进项）与 `{price, rate}`（销项），引用已有 `energy_prices` |
| `om_input_tax[]` | `{for, rate, deductible_ratio}`，`for` 指向 `<finance_type>.annual_fixed_om` / `period_variable_om` 或其 `.fixed` / `.linear.<driver>` 叶子 |
| `maintenance_input_tax[]` | `{year, position, rate, deductible_ratio}`，引用冻结维护清单的评价年与同年序号 |
| `kind: construction_input_vat` | `{label_ref, for, rate, deductible_ratio, credit_pool, confirmation}`；`for` 指向 `upfront_capex` 及其子项，`confirmation` 为 `{method: payment_period}` 或 `{method: explicit_schedule, schedule: [{period, ratio}]}`（`ratio` 合计 `"1"`） |
| `kind: income_tax` | `{label_ref, rate, revenue, energy_cost, annual_om, variable_om, deductible_maintenance, depreciation, loss_carryforward_years, opening_losses, demand_cost?}` |
| `deductible_maintenance` | `{mode: explicit_items, items: [{year, position, deductible}]}` |
| `depreciation[]` | `{base_from, method: straight_line, life_years, residual: Money, first_year}`；`first_year` 为运营年序号，`residual` 为本币确定金额或 `unit: "1"` 的 `0..1` 比例 |
| `opening_losses[]` | `{amount: Money, last_usable_year}`（正整数评价年，同到期年保留输入顺序）；`loss_carryforward_years` 为非负整数 |
| `kind: derived_tax` | 派生税费规则，字段见[命名金额、支付与资产](finance-relations.md#派生税费) |

**时间轴与重复认领检查**：税务月 `period` 从 `1` 起覆盖全评价期 `1..12E`（评价年 `y` 对应年尾月 `12y`，建设付款 `t` 对应月 `12t+1`）。同类进项或折旧不得对同一底层叶子或实例基础重复声明；整项与子项按实际覆盖叶子检测冲突。

```yaml
taxes:
  vat_label: {display_name: 合成增值税, tax_type: value_added_tax, applies_to: grid_import}
  income_label: {display_name: 合成所得税, tax_type: income_tax, applies_to: pv_export}
credit_pools:
  main_pool:
    opening_credit: {value: "0", unit: CNY}
    receivable_policy: end_receivable
    refunds: []
tax_rules:
  operating_vat:
    kind: vat
    label_ref: vat_label
    settlement: monthly
    in_tax:
      - {price: grid_import, rate: {value: "0.13", unit: "1"}, deductible_ratio: {value: "1", unit: "1"}}
    out_tax:
      - {price: pv_export, rate: {value: "0.13", unit: "1"}}
    om_input_tax:
      - {for: pv_system.annual_fixed_om, rate: {value: "0.06", unit: "1"}, deductible_ratio: {value: "1", unit: "1"}}
      - {for: pv_system.period_variable_om, rate: {value: "0.06", unit: "1"}, deductible_ratio: {value: "0.5", unit: "1"}}
    maintenance_input_tax: []
    credit_pool: main_pool
    confirmation: same_period
  building_vat:
    kind: construction_input_vat
    label_ref: vat_label
    for: pv_system.upfront_capex
    rate: {value: "0.09", unit: "1"}
    deductible_ratio: {value: "0.8", unit: "1"}
    credit_pool: main_pool
    confirmation: {method: payment_period}
  income:
    kind: income_tax
    label_ref: income_label
    rate: {value: "0.25", unit: "1"}
    revenue: [pv_export]
    energy_cost: [grid_import]
    annual_om: [pv_system.annual_fixed_om]
    variable_om: [pv_system.period_variable_om]
    deductible_maintenance: {mode: explicit_items, items: []}
    depreciation:
      - {base_from: {kind: upfront_capex_net, finance_type: pv_system, scope: {kind: all_new_instances}}, method: straight_line, life_years: 20, residual: {value: "0.04", unit: "1"}, first_year: 1}
    loss_carryforward_years: 5
    opening_losses: []
```

### 金额来源与折旧期间

`depreciation[].base_from` 与 `net_cashflows[].amount` 共享以下金额来源定义（扩展命名资产 `{kind: asset, id}` 与 `{kind: amount_ref, id}` 见[命名金额、支付与资产](finance-relations.md)）：

| `kind` | 结构与含义 |
|---|---|
| `upfront_capex_net` | `{kind: upfront_capex_net, finance_type, scope}`；`scope` 为 `{kind: all_new_instances}` 或 `{kind: instances, instances: [...]}`，取对应新建实例实际建设付款减去可抵建设进项 |
| `maintenance_expense_net` | `{kind: maintenance_expense_net, year, position}`；引用该评价年序号为 `position` 的实际含税维护付款减去其可抵进项 |

- 直线折旧年额为 `(B - R_effective) / life_years`：比例残值 `R_effective = residual * B`；确定货币残值仅在 `B > 0` 时取 `R`（须满足 `R <= B`），`B = 0` 时不计提折旧。
- 初建折旧可从运营第 1 年开始，维护换新折旧 `first_year` 须晚于支付所在评价年；已作折旧基础的维护项不得同时设为 `deductible: true` 当期全额扣除。

### 显式净现金项目

`net_cashflows.<id>` 定义额外税后净收付（不重复计入能源、O&M、建设或税款链，也不会再次计税；账面残值不自动转成净现金）：

| 字段 | 规则 |
|---|---|
| `at` | `{kind: evaluation_end}`（末期 `CF[E]`）或 `{kind: financial_year, year}`（整数时点 `0..E`） |
| `direction` | `inflow` / `outflow` |
| `amount` | `{kind: fixed, value: Money}` 或 `{kind: source_share, base_from, ratio: Money}`（另支持 `{kind: amount_ref, id}`） |

```yaml
net_cashflows:
  end_recovery:
    at: {kind: evaluation_end}
    direction: inflow
    amount:
      kind: source_share
      base_from: {kind: upfront_capex_net, finance_type: pv_system, scope: {kind: all_new_instances}}
      ratio: {value: "0.03", unit: "1"}
```

### 时间口径与分量

`FinanceProfile` 保持不同时间口径独立输出：

- `upfront_capex`（`currency`，一次性建设投入）、`annual_fixed_om`（`currency/year`，年固定运维）与 `period_variable_om`/`period_energy_*`（`currency`，运行窗口合计）。
- `annual_fixed_om` 须显式乘以时间跨度（如 `1 year`）才能与窗口合计分量相加；具体规则见[规划表达式中的财务分量标识符](assembly-yaml.md#规划表达式中的财务分量标识符)。

### 自然月需量费

`demand_charges.<charge_id>` 声明 `{rate: Money}`，单位为本币除以功率（如 `CNY/kW`，代表每自然月每单位峰值功率费率）。

```yaml
demand_charges:
  import_demand:
    rate: {value: "20", unit: CNY/kW}  # 合成示例；每自然月适用，不是默认费率
```

目录中未在装配 `demand_bindings` 绑定的费项不生效；Overrides 仅可覆盖已有 `charge_id` 的 `rate` 与 VAT 规则 `demand_input_tax` 费率，不可修改 `demand_cost` 范围。

## `ies.finance-overrides@1.0.0`

### 完整示例

```yaml
schema: ies.finance-overrides
schema_version: "1.0.0"
profile_ref:
  id: cn-north-demo
finance_types:
  pv_system:
    upfront_capex:
      fixed: {value: "1500", unit: CNY}                    # 按叶子路径整体替换 Money
      linear:
        capacity_kw: {unit_cost: {value: "3200", unit: CNY/kW}}
energy_prices:
  grid_import:                                             # 只替换定价定义（kind+值/序列）
    kind: constant
    value: {value: "0.75", unit: CNY/kWh}
  pv_export:
    kind: time_series
    ref: "9999999999999999999999999999999999999999999999999999999999999999"
    series_meta:
      resolution: 1h
      point_count: 8760  # 单年示例（运营年限由提交时显式确认，此处取 1；非默认）；实际必须等于全周期总点数
      unit: CNY/kWh
```

### 字段表与覆盖纪律

| 位置 | 允许覆盖范围与限制 |
|---|---|
| `schema` / `schema_version` / `profile_ref` | 固定字头与 `{id}`；无覆盖时只写这三个字段的空覆盖文档 |
| `finance_types.*` | 仅可按已有叶子路径（`<分量>.fixed` 或 `<分量>.linear.<driver>.unit_cost`）整体替换同单位非负 `Money`；禁止增删类别、分量或 `driver` |
| `energy_prices.*` | 仅可整项替换已有 `price_id` 的定价定义（`constant` / `time_series` / 完整 12 个月 `monthly_daily`）；单位不变，`carrier`/`direction` 继承自 Profile |
| `tax_rules.*` | 仅覆盖已有规则的税率/比例原子、亏损结转年限，或整表替换 `depreciation`、`maintenance_input_tax`、`deductible_maintenance.items`、`opening_losses` 与建设 `schedule`；不可新增/删除规则或更改池归属 |
| `net_cashflows.*` / `credit_pools.*` | 分别允许覆盖已有条目的时点/固定金额/比例及已有池的 `opening_credit`/`refunds`；不可改变收付方向与 `receivable_policy` |
| 禁改字段 | 顶层 `currency`、`base_year`、`price_basis`、`cost_method`、`taxes` 等 |

#### 维护税务处理与资本化覆盖示例

```yaml
schema: ies.finance-overrides
schema_version: "1.0.0"
profile_ref: {id: cn-north-demo}
tax_rules:
  operating_vat:
    maintenance_input_tax:
      - {year: 2, position: 1, rate: {value: "0.13", unit: "1"}, deductible_ratio: {value: "0", unit: "1"}}
      - {year: 2, position: 2, rate: {value: "0.13", unit: "1"}, deductible_ratio: {value: "1", unit: "1"}}
  income:
    deductible_maintenance:
      items:
        - {year: 2, position: 1, deductible: true}
        - {year: 2, position: 2, deductible: false}
```

多笔维护资本化折旧时的完整评价输入与 Overrides 示例：

```json
{
  "evaluation_years": 11,
  "construction_period": 2,
  "operation_years": 9,
  "discount_rate": 0.04,
  "construction_payments": [{"t": 2, "ratio": 1.0}],
  "maintenance_expenses": [
    {"year": 6, "currency": "CNY", "amount": "4500", "terms": []},
    {"year": 6, "currency": "CNY", "amount": "7000", "terms": []}
  ]
}
```

```yaml
schema: ies.finance-overrides
schema_version: "1.0.0"
profile_ref: {id: cn-north-demo}
tax_rules:
  operating_vat:
    maintenance_input_tax:
      - {year: 6, position: 1, rate: {value: "0.1", unit: "1"}, deductible_ratio: {value: "1", unit: "1"}}
      - {year: 6, position: 2, rate: {value: "0.1", unit: "1"}, deductible_ratio: {value: "0", unit: "1"}}
  income:
    deductible_maintenance:
      items:
        - {year: 6, position: 1, deductible: false}
        - {year: 6, position: 2, deductible: false}
    depreciation:
      - base_from: {kind: upfront_capex_net, finance_type: pv_system, scope: {kind: all_new_instances}}
        method: straight_line
        life_years: 20
        residual: {value: "0.04", unit: "1"}
        first_year: 1
      - base_from: {kind: maintenance_expense_net, year: 6, position: 1}
        method: straight_line
        life_years: 3
        residual: {value: "0", unit: CNY}
        first_year: 5
      - base_from: {kind: maintenance_expense_net, year: 6, position: 2}
        method: straight_line
        life_years: 4
        residual: {value: "0.05", unit: "1"}
        first_year: 5
```

## `ies.effective-finance-config@1.0.0`

由确定性合并器 `merge(FinanceProfile, FinanceOverrides | None)` 生成（未覆盖条目原样保留）：

```yaml
schema: ies.effective-finance-config
schema_version: "1.0.0"
profile_id: cn-north-demo
currency: CNY
base_year: 2025
price_basis: tax_inclusive
cost_method: fixed_plus_linear
finance_types:
  pv_system:
    upfront_capex:                                     # 被覆盖：fixed 1500、linear 3200
      fixed: {value: "1500", unit: CNY}
      linear:
        capacity_kw: {unit_cost: {value: "3200", unit: CNY/kW}}
    annual_fixed_om:                                   # 未覆盖，原样保留
      fixed: {value: "1200", unit: CNY/a}
      linear:
        capacity_kw: {unit_cost: {value: "35", unit: CNY/kW/a}}
    period_variable_om:                                # 未覆盖，原样保留
      linear:
        generated_kwh: {unit_cost: {value: "0.01", unit: CNY/kWh}}
  battery_system:                                      # 未覆盖，原样保留
    upfront_capex:
      linear:
        energy_kwh: {unit_cost: {value: "900", unit: CNY/kWh}}
    annual_fixed_om:
      linear:
        energy_kwh: {unit_cost: {value: "18", unit: CNY/kWh/a}}
    period_variable_om:
      linear:
        cycled_kwh: {unit_cost: {value: "0.002", unit: CNY/kWh}}
energy_prices:
  grid_import:                                         # 被覆盖定价：constant 0.75
    carrier: electricity
    direction: purchase
    kind: constant
    value: {value: "0.75", unit: CNY/kWh}
  pv_export:                                           # 被覆盖定价：换用另一覆盖全期的序列
    carrier: electricity
    direction: sale
    kind: time_series
    ref: "9999999999999999999999999999999999999999999999999999999999999999"
    series_meta:
      resolution: 1h
      point_count: 8760  # 单年示例（运营年限由提交时显式确认，此处取 1；非默认）；实际必须等于全周期总点数
      unit: CNY/kWh
  heat_supply:                                         # 未覆盖，原样保留
    carrier: heat
    direction: purchase
    kind: constant
    value: {value: "0.35", unit: CNY/kWh}
demand_charges: {}                                      # 继承 Profile，原样保留
taxes:                                                 # 继承 Profile，原样保留
  grid_import_vat:
    display_name: 购电增值税
    tax_type: value_added_tax
    applies_to: grid_import
credit_pools: {}
tax_rules: {}
net_cashflows: {}
```

## 规范化、聚合与计价规则

无覆盖时提交如下空覆盖文档：

```yaml
schema: ies.finance-overrides
schema_version: "1.0.0"
profile_ref:
  id: <profile-id>
```

### 聚合与计价衔接

| 聚合枚举 | 适用目标 | 语义 |
|---|---|---|
| `scalar` | 非时变设备 `property` | 直接取标量属性值；仅用于 `upfront_capex` 与 `annual_fixed_om`（不可用于 `period_variable_om` 或能源计价） |
| `integrate_positive` | 非负功率/流率 `interface` | 按项目步长 `step_duration` 积分 `e[t] = p[t] * step_duration` |
| `sum_positive` | 区间累计能量/量 `interface` | 直接作为同轴序列 `e[t]`，不再积分 |

- 每个 `tariff_binding` 基于非负同轴能量序列 `e_b[t]` 逐点计价（禁止聚合平均价格后再乘总能量）：
  ```text
  raw_charge(b)             = Σ_t price_b[t] * e_b[t]
  period_energy_purchase(b) = +raw_charge(b)   # direction: purchase
  period_energy_sale(b)     = -raw_charge(b)   # direction: sale
  ```
- `period_energy_purchase` 与 `period_energy_sale` 为已带会计符号的贡献（负电价自然反转方向），规划表达式统一使用加法汇总。
