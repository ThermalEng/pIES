# 命名金额、支付与资产关系

> 所有者：`finance`；上位规范：[架构宪法](../ARCHITECTURE_CONSTITUTION.md)

本页定义财务三件套 `1.0.0` 中**命名成本明细**、`amounts`、`cash_movements`、`assets` 及**派生税费**的字段契约与覆盖纪律。基础成本、能源价格、VAT、所得税及直接来源见[财务 YAML 契约](finance-yaml.md)，评价基准比较见[公共契约](../contracts.md#评价范围与冻结基准)。

## 金额、发生与用途三层分离

1. **金额定义（`amounts`）**：声明数值公式关系，不自动产生现金流、资产或税前扣除。
2. **经济发生项（`cash_movements` 等）**：拥有独立发生身份，即使复用同一数值配方也是各自独立的交易发生。
3. **用途关系**：付款计划、进项确认、资本化与折旧分别引用发生身份；平台按发生身份拦截重复支付或同时全额费用化与折旧扣除。

## 命名成本明细

在 `finance_types.<type>.<component>` 下新增可选 `items` 映射（省略等价于 `{}`），与原有的 `fixed`/`linear` 基础叶子**相加**构成同一分量：

| 字段 | 规则 |
|---|---|
| `items.<item_id>` | 稳定局部费用标识（独立于 `driver` 与显示名），至少包含 `fixed` 或 `linear` 之一 |
| `items.<item_id>.fixed` | 可选 `Money`（`upfront_capex` 为币种，`annual_fixed_om` 为币种/年；`period_variable_om` 不支持） |
| `items.<item_id>.linear` | 可选 `driver -> {unit_cost: Money}`，复用既有装配 `driver` 绑定与增量/沉没校验 |

```yaml
annual_fixed_om:
  items:
    materials:
      linear:
        capacity_kw: {unit_cost: {value: "2", unit: CNY/kW/a}}
    service:
      fixed: {value: "10", unit: CNY/a}
      linear:
        capacity_kw: {unit_cost: {value: "5", unit: CNY/kW/a}}
```

- **税基与发生条件路径**：扩展为 `<type>.<component>.items.<item_id>` 及可选 `.fixed` / `.linear.<driver>`；在实例 `cost_occurrence` 中去掉 `<type>.` 前缀。整分量路径覆盖其全部实际叶子（包括 `items`），整项与子项不可重叠配置。

## 通用金额定义 `amounts`

Profile 与 Effective 支持可选 `amounts` 映射（省略规范化为 `{}`），按 `constant + sum(coefficient * source)` 声明标量或逐期金额关系：

| 字段 | 规则 |
|---|---|
| `unit` | 必填输出单位（本币金额或本币费率单位，如 `CNY`、`CNY/a`） |
| `axis` | 必填：`scalar`、`financial_year` 或 `tax_month` |
| `constant` | 必填 `Money`，单位须与 `unit` 一致（无常数项写 `"0"`） |
| `terms` | 必填映射 `term_id -> {coefficient: Money, source: Reference}`（无项写 `{}`）；`coefficient` 为已知有限十进制数，不得为决策量 |

`source` 支持以下 12 类显式引用（禁止直接或间接同期间自引用）：

| `kind` | 必填字段与语义 |
|---|---|
| `driver` | `finance_type, driver, scope, period`；绑定后的实例属性或运行聚合量 |
| `system_quantity` | `block, variable, aggregation, period`；引用 `system_equations` 辅助变量（`aggregation` 为 `integrate_positive` 或 `sum_positive`） |
| `construction_unit` | `unit_ref, period: {kind: scalar}`；引用装配建设单元同次 `0/1` 激活状态（单位 `"1"`） |
| `cost` | `path, scope, basis, period`；引用设备成本分量或命名叶子路径 |
| `tariff` / `demand` | `price`/`charge, bindings, basis, period`；引用明确非空 `tariff_bindings` / `demand_bindings` 集合（取 `raw` 原始正额，符号由公式系数显式指定） |
| `maintenance` | `year, position, basis`；引用评价清单同年发生项 |
| `amount` | `id, period`；引用本映射其他已声明金额 |
| `movement` | `id, basis, period`；按发生期引用独立收付发生项 |
| `asset` | `id, measure, period`；`measure` 为 `basis`（`scalar`）/ `depreciation` / `book_value`（`financial_year`） |
| `tax` | `source, measure, period`；见下表「税额输出引用」 |
| `baseline` | `id, period`；引用本次冻结的 `evaluation.comparison.facts` 别名 |

- **`scope`**：`{kind: instances, instances: [...]}` 或 `{kind: all_new_instances}`（仅包含新建实例，项目无匹配实例时取零）。
- **`basis`**：`raw`（来源原口径）、`gross`（含税实际收付款）、`deductible_input`（可抵进项）、`net_basis`（支出 `gross - deductible_input`）或 `net_revenue`（收入 `gross - output_tax`）；非适用方向的口径在输入边界拒绝。
- **`period`**：判别联合支持 `{kind: scalar}`、`{kind: current}`（同轴当前期间）、`{kind: financial_year, year}`、`{kind: tax_month, month}`、`{kind: year_months, year}`（汇总当年 12 个税务月）与 `{kind: range, axis, first, last}`（闭区间汇总）。

### 税额输出引用

| `source` | `measure` | 单位与语义 |
|---|---|---|
| `{kind: rule, id}`（VAT） | `output_tax` / `deductible_input` | 该规则指定税务月确认的销项或可抵进项 |
| `{kind: pool, id}` | `paid` / `refund_confirmed` / `refund_received` / `credit_balance` / `receivable` | 共享池实缴、确认退款、到账退款、期末留抵或期末应收 |
| `{kind: rule, id}`（所得税） | `taxable_base` / `loss_used` / `assessed` / `paid` | 当年亏损前应税基础、亏损抵扣、应计与实缴所得税 |
| `{kind: rule, id}`（派生税费） | `assessed` / `paid` | 当期应计或实际支付期金额（`paid` 按支付滞后后的实际期取值） |

每个 `source`/`measure` 对应的封存输出字段见[税额引用与封存字段](solver-bundle.md#税额引用与封存字段)；进入规划约束的最小入口见[金额引用约束](assembly-yaml.md#金额引用约束)。

## 具名收付 `cash_movements`

`cash_movements.<movement_id>` 定义独立经济发生项，发生期身份为 `(movement_id, recognition_axis, period)`：

| 字段 | 规则 |
|---|---|
| `amount` | 必填 `{kind: amount, id, period}`，引用输出单位为本币金额的标量或同轴逐期 `amount` |
| `price_basis` | `tax_inclusive` 或 `tax_exclusive` |
| `direction` | `inflow`（流入）或 `outflow`（流出） |
| `recognition` | `{kind: at, at: TimePoint}` 或 `{kind: each, axis: financial_year|tax_month, first, last}`（`at` 的 `financial_year` 允许为 `0`） |
| `payments` | 必填有序列表 `[{offset, ratio}]`；`offset` 为从发生期起算的整数偏移（负值表示预付），`ratio` 为非负十进制字符串且精确合计 `"1"` |

- 确认发生期必须在评价期内，计算后的实际付款期须满足评价年 `>= 0` 或税务月 `>= 1`；超出评价期末的约定付款封存为 `end_unpaid`，不计入期内现金流。
- `net_cashflows.<id>.amount` 支持 `{kind: amount_ref, id}` 引用非负标量本币金额。

## 资本化与计提 `assets`

`assets.<asset_id>` 将特定支出的净基础组合为独立折旧资产，并在所得税规则 `depreciation[].base_from` 中通过 `{kind: asset, id}` 引用：

| 字段 | 规则 |
|---|---|
| `basis_from` | 引用 `cost`（限 `upfront_capex` 及其子项）、`maintenance` 或 `movement` 的 `net_basis`；或引用纯净基础线性加权组合的 `amount`（`constant` 为 `"0"`，非负已知系数） |
| `capitalized_at` | 必填 `TimePoint`（资本化时点，年时点须早于 `K + available_from`，月时点须不晚于 `12*(K + available_from - 1)`） |
| `available_from` | 必填正整数运营年（最早可开始计提年度） |

同一原始发生项被直接折旧认领与各资产分配的总份额不得超过 `1`；资本化的支出不得同时列入 `movement_cost` 当期费用扣除。示例（`CNY`、`K=1`、`E>=2`）：

```yaml
amounts:
  purchase_quote:
    unit: CNY
    axis: scalar
    constant: {value: "100", unit: CNY}
    terms: {}
cash_movements:
  purchase_invoice:
    amount: {kind: amount, id: purchase_quote, period: {kind: scalar}}
    price_basis: tax_inclusive
    direction: outflow
    recognition: {kind: at, at: {kind: financial_year, year: 0}}
    payments:
      - {offset: 0, ratio: "0.4"}
      - {offset: 2, ratio: "0.6"}
assets:
  asset_a:
    basis_from:
      kind: movement
      id: purchase_invoice
      basis: net_basis
      period: {kind: financial_year, year: 0}
    capitalized_at: {kind: financial_year, year: 1}
    available_from: 1
```

## 派生税费与收付税务规则

### 派生税费

- **派生税费（`tax_rules.<id>` 中 `kind: derived_tax`）**：包含 `{label_ref, axis, base_from, rate, payment_lag}`；`axis` 为 `tax_month` 或 `financial_year`，`base_from` 引用 `kind: tax`（期间为 `current` 或跨轴 `year_months`），应计额等于 `base_from * rate`，在 `period + payment_lag` 期发生现金支付（期外不计入期内现金）。
- **所得税扩展列表**：`income_tax` 规则增加可选列表 `movement_revenue`、`movement_cost`（按发生期分别计入税基）与 `derived_tax_cost`（按应计期计入可扣费用）。
- **收付 VAT 条目**：`vat` 规则新增可选 `movement_input_tax: [{movement, rate, deductible_ratio, confirmation}]` 与 `movement_output_tax: [{movement, rate, confirmation}]`；`confirmation` 为 `{method: recognition_period}` 或 `{method: explicit_schedule, occurrences: [{at, schedule: [{period, ratio}]}]}`（同一发生项只能由一条 VAT 规则确认）：

```yaml
movement_input_tax:
  - movement: service
    rate: {value: "0.1", unit: "1"}
    deductible_ratio: {value: "1", unit: "1"}
    confirmation:
      method: explicit_schedule
      occurrences:
        - at: {kind: financial_year, year: 1}
          schedule: [{period: 13, ratio: "1"}]
        - at: {kind: financial_year, year: 2}
          schedule: [{period: 25, ratio: "1"}]
```

## 合并、冻结与能力边界

| 扩展节 | Overrides 覆盖规则 | 所需生成器能力 |
|---|---|---|
| 命名成本 `items` | 仅可按叶子替换 `fixed` / `linear` 单价，不可增删 `item` | `finance.named_items` |
| `amounts` | 可替换已有 `constant`/`coefficient`、实例/绑定范围及固定期间，不可改单位、轴或来源种类 | `finance.amount_references` |
| `cash_movements` | 可整体替换 `recognition` 与 `payments`，不可改方向、口径或金额来源 | `finance.cash_movements` |
| `assets` | 可替换实例范围、`capitalized_at` 与 `available_from` | `finance.assets` |
| `derived_tax` & VAT/CIT 收付列表 | 可替换派生税 `rate`/`payment_lag` 与既有 `movement` VAT 条目的费率及完整 `occurrences` | `finance.derived_tax` |

以上关系在进入计算求解前须按实际使用项核对完整生成器能力（均依赖 `finance.financial_facts`）；对应同次解输出字段见[关系事实映射](solver-bundle.md#关系事实映射)。

## 最小手算对照示例

| 测试输入 | 预期核算事实 |
|---|---|
| `q=3 kW`，`materials` 2 CNY/kW/a（进项 10% 全抵），`service` 10 CNY/a + 5 CNY/kW/a（无进项） | 两项年费分别为 `6`、`25` CNY/a（总额 `31`），仅对 `materials` 独立拆分进项 |
| `100 CNY` 含税（无 VAT）发生项分 `40%/60%` 支付，直线折旧 5 年残值 `0` | 现金流出 `40` 与 `60` 各一次，年折旧额恒为 `20` |
| 某税实缴 `10`，派生税率 `0.04`，`payment_lag: 1` | 应计期记录 `0.4`，下一期实付 `0.4` 进入现金流 |
| 期末净现金回收引用 `9 CNY/kW × 3 kW` | 回收金额 `27` 随同次解规模确定，账面残值不产生第二笔回款 |
