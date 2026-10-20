# 财务 YAML 契约

> 契约标识与版本：`ies.finance-profile@3.0.0`、`ies.finance-overrides@3.0.0`、`ies.effective-finance-config@3.0.0`。本版增加计算规则与抵扣池，税率归计算规则；不接受旧字头、旧税率登记或双轨解析。
> 推荐文件名（项目包内固定名称）：`finance_profile.yaml`、`finance_overrides.yaml`、`effective_finance.yaml`
> 文档状态：生效目标契约；本页只定义目标文件语义，不声明实现进度。

本页定义财务三件套的完整 YAML 结构与校验语义，是 [文件格式标准](../file-formats.md) 与[财务计算](../modules/finance.md)的字段级权威正文。装配如何引用有效快照见[装配 YAML](assembly-yaml.md)。

```text
FinanceProfile（地区财务基准，人工 authoring）
FinanceOverrides（项目覆盖，人工 authoring）
        │ 两者并列
        ▼
merger（确定性合并 + 完整校验）── 生成不可变 EffectiveFinanceConfig（不可人工 authoring）
        ▼
assembly / computation / 项目包（精确引用 Effective YAML）
```

- `FinanceProfile` 是已注册、可复用的地区财务基准（以稳定 ID 与 revision 标识）；系统不得把它硬编码为对所有项目生效的全局默认，也不得把样例文件当作隐式默认绑定。仓库内或插件包中的样例只作为示范或注册 provider 的输入。
- `FinanceOverrides` 精确引用 Profile 的稳定 ID 与 revision，只做稀疏原子覆盖，是 authoring 输入。
- `EffectiveFinanceConfig` 只能由合并器从 Profile 与 Overrides 生成；可以导出、导入、进入快照，但用户不能直接 authoring。项目包导入在外部上传边界连同精确 Profile 与 Overrides 从来源重新合并，包内 Effective 必须与来源合并结果一致；当前组与历史依赖组遵守同一规则。确认阶段只发布已接受内容，不再解析校验或重新合并。

金额一律为 `{value, unit}` 原子：`value` 为十进制定点字符串（禁止 `float`、`NaN/Infinity`、`null`），`unit` 来自公共单位规范并经 `normalize_unit` 规范化；禁止部分金额（缺 `value` 或 `unit`）与 `null`。成本分量金额非负（允许 `0`，零费用类别、零固定成本均合法）。能源价格不设符号限制：允许有限正数、零与负数（见 `energy_prices` 行与「计价规则」）；禁止 NaN/Infinity。

## `ies.finance-profile@3.0.0`

### 完整示例

```yaml
schema: ies.finance-profile
schema_version: "3.0.0"
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
taxes:
  grid_import_vat:
    display_name: 购电增值税
    tax_type: value_added_tax
    applies_to: grid_import     # 引用 price_id（energy_prices 键）或 finance_types 分量路径
credit_pools: {}
tax_rules: {}                  # 本例只有展示登记，没有启用税务计算规则
```

示例按字头校验；不需要写入内容摘要。同一载体允许登记多个价格条目（例如工商业与峰谷两套购电方案、多个出口电价），不需要改动核心 schema。

### 字段表

| 路径 | 必需 | 规则 |
|---|---|---|
| `schema` | 是 | 固定 `ies.finance-profile` |
| `schema_version` | 是 | 固定 `"3.0.0"` |
| `profile.id` | 是 | 稳定 ASCII 小写 ID（命名空间点分式、`lower_snake_case` 或短横线，同文件内保持一致），如 `cn-north-demo`；Overrides 与 Effective 沿用它做引用 |
| `profile.region` | 是 | 地区标识（展示与追溯，不参与计算） |
| `profile.currency` | 是 | `CNY` / `USD` |
| `profile.base_year` | 是 | 整数财务基准年（1900–2999） |
| `profile.price_basis` | 是 | `tax_inclusive` / `tax_exclusive`：文件内金额是否已含税；按明确 `tax_rules` 拆分或形成含税实际收付款。登记标签不触发计税，含税金额不重复加税 |
| `profile.cost_method` | 是 | `fixed_plus_linear`：表示成本函数为一个固定建设成本加多个独立线性分量 |
| `finance_types` | 是 | `finance_type` → 成本分量块；键名与 `device.id`/`instance_id` 相互独立 |
| `finance_types.*.upfront_capex` | 至少一个分量存在 | 建设期一次性成本，仅 `new` 计入 |
| `finance_types.*.upfront_capex.fixed` | 可选 | `Money`；单位量纲 = currency |
| `finance_types.*.upfront_capex.linear` | 可选 | `driver` → `{unit_cost: Money}`；`unit_cost` 单位量纲 = currency / driver 单位 |
| `finance_types.*.annual_fixed_om.linear` | 可选 | 每年固定 O&M；`unit_cost` 单位量纲 = currency / driver 单位 / 年（如 `CNY/kW/a`）；不使用费率模型 `fixed_om_rate` |
| `finance_types.*.period_variable_om.linear` | 可选 | 运营期可变 O&M；`unit_cost` 单位量纲 = currency / 运行量单位（如 `CNY/kWh`） |
| `energy_prices` | 是 | `price_id` → 价格条目。`price_id` 为稳定自定义名（`lower_snake_case`、不含 `__`、文件内唯一），同一载体可登记多个 price_id（不同方案/对象），新增载体或方案不需要改核心 schema |
| `energy_prices.*.carrier` | 是 | 载体标识，必须来自公共载体词汇（与设备接口 `carrier` 同一词汇源；取值与规则以[设备模型 YAML](device-model-yaml.md)「interfaces」为准，本页不复制载体清单）；装配按它与接口载体相容性校验 |
| `energy_prices.*.direction` | 是 | `purchase` / `sale` 二选一：显式会计方向。`purchase` 分量 = +raw_charge、`sale` 分量 = −raw_charge（`raw_charge = Σ price×energy`，定义见「计价规则」）；方向只由本字段决定，不靠 price_id 或键名后缀推断 |
| `energy_prices.*` | 是 | 定价定义判别联合：`{kind: constant, value: Money}` 或 `{kind: time_series, ref, series_meta}`，二选一 |
| `energy_prices.*.value` | constant 必填 | 有限 Decimal，可正、可零、可负；单位量纲 = currency /（该 carrier 的能源单位量纲） |
| `energy_prices.*.ref` | time_series 必填 | 对象引用：存储已有的不透明对象 ID（`oid`，随机对象 ID，不是内容 hash、路径或数据集版本），指向经校验、覆盖运营期全部点数的连续能源价格序列；年度价格不同直接在对应 step 给出不同值，不设逐年引用；序列值同样为有限 Decimal、允许零与负。`oid` 由价格文件经数据集上传入口校验落盘后在质量报告 `price_object_id` 中返回（格式见[价格序列 CSV](price-series-csv.md)）；Profile/Overrides 只存 `ref` + `series_meta`，不内联价格值；任务提交时以已确认运营时轴对实际对象做唯一一次覆盖/声明元数据核对并冻结引用 |
| `energy_prices.*.series_meta` | time_series 必填 | 不可变时间元数据：`resolution`（15min/30min/1h）、`point_count`（整数，必须等于运营期总点数）、`unit`（无 `leap_year`，闰年由运行起始年逐年按公历推导）；运营期覆盖在任务提交边界核对（取得已确认运营年限 `operation_years` 后），不一致阻断且不重采样；Profile 不固定唯一项目分辨率 |
| `taxes` | 可选（无税目可写 `{}`） | 展示标签映射：`id` → `{display_name, tax_type, applies_to}`，无税率；见下「税目登记」 |
| `credit_pools` | 是 | 抵扣池映射，无池时显式 `{}`；期初余额与退税仅在池声明 |
| `tax_rules` | 是 | 计算规则映射，无规则时显式 `{}`；规则结构见下「税务计算输入」 |

单位与范围校验：

- 成本分量金额非负（允许 `0`）；每个线性分量的 `unit_cost` 单位与装配绑定的规范化 driver 单位相乘后必须得到 `currency`（upfront/period）或 `currency/year`（annual_fixed_om）量纲；
- 能源价格为有限 Decimal（正、零、负均合法），单位量纲 = currency /（对应 carrier 的能源量纲）；constant 的 `value.unit` 与 time_series 的 `series_meta.unit` 必须一致；禁止 NaN/Infinity；
- `taxes.*.applies_to` 引用的 price_id 或 `finance_types` 分量路径必须存在；计算规则中的税率、可抵比例为 `0..1` 的 `{value, unit: "1"}` 原子。

### 税目登记

`taxes` 只做声明性登记，供人工核验与审计；**本版成本与价格计算不消费它**：

- `id`：唯一 `lower_snake_case` 稳定标识；
- `display_name`：人类可读税种名；
- `tax_type`：税种标识（如 `value_added_tax`）；本契约只登记不解析税种语义，不预建税务引擎；
- `applies_to`：明确适用对象，二选一——引用本文件 `energy_prices` 的 `price_id`（如 `grid_import`），或 `finance_types` 的分量路径（如 `pv_system.upfront_capex`）；一个税目对应一个适用对象，多个对象登记多个税目。

`applies_to` 是标签展示对象，不是计算范围。计算唯一消费 `tax_rules` 中的明确对象、方法与参数，不从税种名称或标签推断税率、范围或抵扣资格。金额、余额及同次优化语义见[财务计算](../modules/finance.md#税务参与规划与现金流)。

### 税务计算输入

规则 ID 与池 ID 为文件内稳定 `lower_snake_case` 名称。规则使用 `kind` 判别，不接收可执行表达式、脚本或求解器选项。Profile 声明方法与范围，Overrides 提供明确项目参数，Effective 保留完整结果。评价参数与维护现金清单仍归评价确认，不复制进财务文件。

| 位置 | 完整结构与含义 |
| --- | --- |
| `credit_pools.<id>` | `{opening_credit: Money, receivable_policy: end_receivable, refunds: [...]}`；期初存量不含本课题随容量变化的建设进项。余额币种与 Profile 相同且非负 |
| `refunds[]` | `{period, amount: Money, arrival_period}`；在 period 按声明金额扣减可用留抵，arrival_period 到账；二者为正整数且到账不早于确认。超出可用余额不可执行，不自动截小 |
| `kind: vat` | 必填 `label_ref, settlement: monthly, in_tax, out_tax, om_input_tax, maintenance_input_tax, credit_pool, confirmation: same_period` |
| `in_tax[]` | `{price, rate, deductible_ratio}`，price 引用 purchase 价格条目；多个税率可进同池 |
| `out_tax[]` | `{price, rate}`，price 引用 sale 价格条目 |
| `om_input_tax[]` | `{for, rate, deductible_ratio}`，for 为已有 `<finance_type>.annual_fixed_om` 或 `.period_variable_om` 分量路径 |
| `maintenance_input_tax[]` | `{year, position, rate, deductible_ratio}`；引用评价维护清单中的一项，复用本规则抵扣池与确认方法 |
| `kind: construction_input_vat` | 必填 `label_ref, for, rate, deductible_ratio, credit_pool, confirmation`；for 为已有 `<finance_type>.upfront_capex`，金额按实例选建及容量派生，不填写固定建设税额 |
| 建设 `confirmation` | 判别联合 `{method: payment_period}` 或 `{method: explicit_schedule, schedule: [{period, ratio}]}`；只接受所选方法字段。schedule 比例为十进制字符串，非负且精确合计1，作用于派生的可抵建设进项总额，不再次乘付款比例 |
| `kind: income_tax` | 必填 `label_ref, rate, revenue, energy_cost, annual_om, variable_om, deductible_maintenance, depreciation, loss_carryforward_years, opening_losses`；按年核算 |
| 所得税范围列表 | revenue / energy_cost 引用已有 sale / purchase 价格；annual_om / variable_om 引用对应已有财务分量。各项以列表显式声明，不因键名或未列出而猜测范围 |
| `deductible_maintenance` | `{mode: explicit_items, items: [{year, position, deductible}]}`；deductible 为布尔值，表示所得税费用可扣性，独立于进项抵扣 |
| `depreciation[]` | `{asset, base_from, method: straight_line, life_years, residual: Money, first_year}`；asset 为已有 finance_type，base_from 仅为同类 `<finance_type>.upfront_capex_net` 派生引用。life_years、first_year 为正整数；残值非负，不另填固定计税基础 |
| `opening_losses[]` | `{amount: Money, last_usable_year}`；金额非负，最后可用评价年为正整数；同到期年保留输入顺序 |
| `loss_carryforward_years` | 非负整数；新生亏损按声明年限结转，具体使用与到期语义见财务模块 |

所有规则的 `label_ref`、`credit_pool` 及财务对象引用必须存在。同一税基不能被多个同类规则重复征税或抵扣；不同税率规则可共享池，池的期初余额和退款只声明一次。未启用进项规则的对象不猜抵扣资格。年度固定运维和维护现金具有年度时点，不由规则自动平均成月费；所选生成器支持的确认时点必须与该时间粒度一致。

维护引用 `(year, position)` 中，year 是评价年，position 是冻结清单同年内从1开始的顺序。现金金额仍唯一来自 `evaluation.maintenance_expenses`。所得税可扣项的费用基础为实际费用扣除明确可抵进项，不可抵部分保留；不可扣不表示没有现金支出。建设折旧基础同理：含税额减可抵建设进项；不含税输入先按明确税率形成含税额，不可抵进项留在计税基础中。

时间使用 `K=construction_period`、`N=operation_years`、`E=K+N`：税务月 period 从1起，覆盖建设与运营的连续月份；维护 year 与 last_usable_year 为评价年序号，折旧 first_year 为运营年序号，评价年=`K+运营年`。跟随付款确认取 `period=12t+1`，付款 `0<=t<=K<E`，不对 `t=K` 向前夹期。K=1 时 t=0/1 对应 period=1/13，首运营月为13，首运营年末为24；K=0 时 t=0 对应 period=1。

退款确认期须在评价期内；到账可晚于评价期。`end_receivable` 表示届时仍未到账的已确认退款列期末应收，不计入期内现金或默认为期末回款；未退留抵仍为余额。不得把终期应收与留抵同时作为现金收益。

以下合成配置展示完整规则形状；它替换本页 Profile 示例的 `taxes`、`credit_pools`、`tax_rules` 三节，其他分量不变。所有数值仅为示例，不代表税法或项目默认值：

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
      - {asset: pv_system, base_from: pv_system.upfront_capex_net, method: straight_line, life_years: 20, residual: {value: "0", unit: CNY}, first_year: 1}
    loss_carryforward_years: 5
    opening_losses: []
```

字段自身的类型、范围、比例和与文件内引用在财务公开输入边界检查；项目维护项对应及 `period<=12E` 等跨输入约束在任务提交边界核对一次。内部冻结、读取和生成不重复解析校验；尚未支持的计算关系在公开提交能力检查中明确拒绝，不可静默忽略税务规则后执行税前计算。

### 时间口径与分量

`FinanceProfile` 分别输出时间口径明确的分量，**不静默相加成默认总成本**：

- `upfront_capex`：本课题建设成本总额；金额量纲 = currency；固定建设费按选建实例只计一次，线性建设费按选定容量计算。“一次性建设成本”不表示付款只能发生一次或一律在 `t=0`；支付时点与建设期按[财务计算](../modules/finance.md)的明确项目安排处理，不隐式均摊；
- `annual_fixed_om`：按年度计；金额量纲 = currency/年（按年值，不随计算窗口缩放）；
- `period_variable_om`、`period_energy_purchase`、`period_energy_sale`：按运行/计费窗口合计；金额量纲 = currency；两个能源分量为**有符号记账贡献**，符号约定见「计价规则」（分量生成与计价规则见下文「聚合与计价衔接」）。

口径换算与组合规则：

- 按年分量（currency/年）与窗口合计分量（currency）相加前，必须把按年分量乘以**显式时间跨度**换算到同一窗口后再相加（示例为单年窗口：`annual_fixed_om × 1 year`，运营年限由提交时显式确认，此处取 1；非默认）；缺少显式时间跨度不得默认相加或做其他隐式缩放；
- 本契约不定义“总成本”聚合，也不提供隐式“系统总成本”标识符；`upfront_capex`（一次性）与运行期成本之间不存在本契约默认的转换——不做隐式年化，不隐含资本回收系数、利率或年限等值；
- 具体如何把一次性投资转换为可与运行期成本比较的规划分量（例如时间口径一致的显式目标分量），属于 `PlanningConfig` 的明确、版本化规则，须在规划配置契约中定义后再使用；本财务 Profile 不隐式定义或默认选择这种转换，也不因不做后评价而禁止规划配置契约定义此类显式分量；
- NPV、IRR 和回收期是评价输出；规划目标与财务关系的衔接见[财务计算](../modules/finance.md#税务参与规划与现金流)；
- 规划表达式引用这些分量的合法标识符编码与单位字面量规则见[装配 YAML](assembly-yaml.md)「规划表达式中的财务分量标识符」。

## `ies.finance-overrides@3.0.0`

### 完整示例

```yaml
schema: ies.finance-overrides
schema_version: "3.0.0"
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

| 路径 | 规则 |
|---|---|
| `schema` / `schema_version` | 固定 `ies.finance-overrides` / `"3.0.0"` |
| `profile_ref` | 必填 `{id}`：与目标 Profile 的 `id` 一致（文本文件按字头校验，不做内容摘要） |
| `finance_types` | 只允许覆盖目标 Profile **已存在** 的 `finance_type`；禁止新增或删除 `finance_type` |
| `finance_types.*` | 只允许按 Profile 实际存在的叶子路径整体替换 `Money` 原子：`<分量>.fixed`（如 `upfront_capex.fixed`）替换为 `{value, unit}`；`<分量>.linear.<driver>.unit_cost` 替换为 `{value, unit}`。被替换叶子必须已在 Profile 声明；禁止新增或删除分量、`fixed` 节点或 `driver`（`driver` 集合不可增删）、禁止改分量类型与 `cost_method`、禁止改 `unit`（规范化后必须与 Profile 相同）；替换值必须为完整 `{value, unit}` 原子，禁止部分金额与 `null`；成本金额非负 |
| `energy_prices` | 只允许覆盖 Profile **已存在** 的 `price_id`，禁止新增或删除 `price_id`。每个条目的 `energy_prices.<price_id>` 只写定价定义：`{kind: constant, value}` 或 `{kind: time_series, ref, series_meta}`（与 Profile 原 kind 无关）；`carrier` 与 `direction` 由 Profile 继承，在 Overrides 中出现即拒绝；`value.unit`（constant）或 `series_meta.unit`（time_series）必须与 Profile 原项相同；`time_series` 的 `ref` 为存储不透明对象 ID（`oid`，与 Profile 侧同一口径），指向经校验、覆盖全周期全部点数的价格序列对象，`series_meta` 必须完整并可与 Profile 引用不同序列；不允许部分混合替换（如只换 `ref` 保留旧 `series_meta`，或只换 kind 不带全量字段） |
| `tax_rules` | 只覆盖 Profile 已有规则的参数：税率／可抵比例 Money 原子、折旧年限／残值／起始运营年（按已有 asset 对应）、亏损结转年限。in_tax／out_tax／om_input_tax 的稀疏条目按 price／for 对应已有对象，只替换已有 rate／deductible_ratio。维护进项列表、所得税逐项维护列表及 opening_losses 按完整列表替换；建设确认 schedule 可整表替换，method 保持不变 |
| `credit_pools` | 只覆盖已有池的 opening_credit 原子或 refunds 完整列表；不可新增／删除池或改变 receivable_policy |
| 禁改字段 | 顶层 profile、currency、base_year、price_basis、cost_method、taxes，价格条目 carrier／direction，规则 id／kind／label_ref／方法／计税范围／池归属不允许覆盖；不得新增／删除规则。列表中的项目维护项绑定不属于改变规则结构 |
| 其余字段 | 按本页规则校验 |

覆盖只影响合并结果中的对应参数；未覆盖条目原样保留（见 Effective 示例中未覆盖的 `battery_system`、`heat_supply`、`taxes` 等）。合并边界检查覆盖权限及领域引用，任一失败不产生 Effective，不对可信输入重复校验。

启用上述合成税务 Profile 后，项目可按下例分别声明两笔同年维护的税务处理。金额仍只在评价确认清单中；示例仅证明两个维度独立，不代表真实税法：

```yaml
schema: ies.finance-overrides
schema_version: "3.0.0"
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

## `ies.effective-finance-config@3.0.0`

### 完整示例

合并自上面示例的 Profile 与 Overrides。未覆盖条目全部保留：

```yaml
schema: ies.effective-finance-config
schema_version: "3.0.0"
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
taxes:                                                 # 继承 Profile，原样保留
  grid_import_vat:
    display_name: 购电增值税
    tax_type: value_added_tax
    applies_to: grid_import
credit_pools: {}
tax_rules: {}
```

生成规则：

- `EffectiveFinanceConfig` 只能由合并器生成：`merge(FinanceProfile, FinanceOverrides | None)`；无覆盖时使用空 Overrides 文档，不允许省略来源字段。
- 合并边界检查本页覆盖纪律及合并后的领域引用；已类型化输入不重复做类型、数值或字头校验。任一覆盖或领域引用错误不产生 Effective。
- 稀疏覆盖只替换声明的参数或完整列表：未覆盖的成本、能源价格、税目、规则与池全部原样保留，不允许省略、裁剪或按“示例只写用到的部分”生成。
- `EffectiveFinanceConfig` 可以导出、导入、进入快照；项目包导入在外部上传边界连同来源 Profile 与 Overrides 一次性完成合并验证，确认阶段不重复执行。
- 装配与计算只消费该不可变快照：不运行期继承 Profile、不读取“最新地区价格”、不静默默认值。

## 规范化

文本财务文件只校验字头（`schema`/`schema_version`）与领域约束。规范化仍由公开纯函数完成（映射键稳定排序、金额用定点十进制字符串、单位保留原始拼写、注释与空行移除、LF 换行、非 ASCII 保留），用于确定性比较与测试。无覆盖时使用空 Overrides 文档：

```yaml
schema: ies.finance-overrides
schema_version: "3.0.0"
profile_ref:
  id: <profile-id>
```

该文档本身有效（没有覆盖任何内容），合并结果等于 Profile。

## 聚合与计价衔接（装配绑定引用）

装配把财务 driver 绑定到实例的设备 `property`/`interface`，把能源价格（`price_id`）经 `tariff_bindings` 绑定到计费点实例/接口时，必须声明下列聚合之一：

| 枚举 | 适用目标 | 语义 |
|---|---|---|
| `scalar` | 设备 `property`（非时变标量） | 直接取实例化后的 property 值；只用于成本 driver 的年度/一次性量，不用于能源计价；时序 interface 禁止使用 |
| `integrate_positive` | 非负功率/流率 `interface` | 每步乘项目基线公开变量 `step_duration`：`e[t] = p[t] × step_duration`，得到与价格序列同轴的能量序列；其时间单位由项目 `resolution` 唯一推导 |
| `sum_positive` | 区间累计量 `interface` | 接口序列本身即为区间累计能量/累计量序列，直接作为 `e[t]`，不再二次积分 |

时序 `interface` 必须写 `aggregation`；`property` 只能对应 `scalar`，装配映射 `property` 时 `aggregation` 字段可省略（语义固定为 `scalar`）。本契约不接受模糊的双向净值或绝对值：购/售分别绑定方向明确的接口（如 `electricity_in`/`electricity_out`），能量/流量序列在聚合前必须非负并阻断负值，不自动取绝对值；价格本身的零/负值与能量非负是两个独立约束，不得混淆。

`scalar` 成本 driver 用于 `upfront_capex` 的建设量或 `annual_fixed_om` 的年度存量计费；不得用于 `period_variable_om` 生成无年份、无时间维度的全期运维金额。`period_variable_om` 属使用运维费，按接口时序的实际运行量／发电量（如 kWh、Nm³）计费，年度费用由该年运行结果直接归集，能力全部保留，禁止把全期合计自动均摊。装配输入边界按 driver 实际消费的分量阻断上述标量组合，不禁止建设费或年度固定运维的合法 property 绑定；固定运维费的分类及显式发生年的维护／换新清单见[财务计算](../modules/finance.md)。

### 计价规则（时变价格逐点计价，会计方向由 `direction` 固定）

每个 `tariff_binding` 独立产生可追溯、**已带符号**的计费分量。聚合产物是与价格序列 `step` 一一对应的能量序列 `e[t]`（非负）；逐点计价后按被引用价格的显式 `direction` 加符号，会计方向不靠 price_id 或键名推断：

```
raw_charge(b) = Σ_t price_b[t] × e_b[t]          # 先求未加符号的原始计费额
period_energy_purchase(b) = +raw_charge(b)         # direction: purchase
period_energy_sale(b)     = −raw_charge(b)         # direction: sale
```

- 每个计量绑定产出的 `period_energy_purchase`/`period_energy_sale` 是**已带符号的记账贡献**（currency）：`purchase` 分量 = +raw_charge（正购电价时为正，成本向），`sale` 分量 = −raw_charge（正售电价时为负，收益以负值自动抵减成本）。分量负号是记账符号，不是把物理能量取负，能量序列本身始终非负；
- 规划表达式对已签名分量**统一使用加法**（见装配最小结构示例的 `… + export_meter__period_energy_sale`），不再书写额外负号；sale 的收益性体现在分量自身的负值上，不依赖目标写法；
- 价格允许有限正、零、负值：零电价合法；负价自然按该方向反转——负购电价使 `purchase` 分量为负（为用电付费抵减成本），负售电价使 `sale` 分量为正（为上网付费成为成本），不需要额外规则；
- `time_series` 价格按每点取价后与同点能量相乘再求和；`constant` 价格每点相同，结果等于 `price × Σ_t e[t]`，但语义仍是逐点计价，不允许把“先对时变价格做总量聚合再与总能量相乘”当作等价算法；
- 价格序列与能量序列必须同分辨率、同点数、`step` 一一对应（由项目基线与装配校验保证，不匹配阻断且不重采样）；
- 量纲：价格单位（currency / 能源单位）× `e[t]`（能源量）→ `currency`，逐点乘积与求和保持同一币种。

## 装配与项目包引用

- 装配 YAML 的 `finance` 节引用 Effective YAML：`ref`（`kind: object` 或包内 `relative_file`）携带对象 ID；`profile_id` 携带来源标识。装配不在内内联完整财务配置；`ValidatedAssemblyArtifact` 固定引用。
- 对象存储写入或项目包外部导入时，在边界一次性确定对象身份；内部读取按对象身份取得已登记内容，来源标识随快照传递。
- 项目包可携带 `finance_profile.yaml`、`finance_overrides.yaml`、`effective_finance.yaml` 三个文件；外部上传边界按清单接受文件，并从精确 Profile 与 Overrides 合并确定 Effective。来源一致性属于输入业务约束，不是内部文件完整性复核；确认阶段只消费已接受内容。
- 数据库内部存储使用列/JSON 表示，不强制 YAML；HTTP JSON DTO 仍为 JSON。
