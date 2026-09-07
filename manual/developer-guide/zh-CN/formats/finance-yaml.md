# 财务 YAML 契约

> 契约标识与版本：`ies.finance-profile@1.0.0`、`ies.finance-overrides@1.0.0`、`ies.effective-finance-config@1.0.0`
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

- `FinanceProfile` 是已注册、内容寻址、可复用的地区财务基准；系统不得把它硬编码为对所有项目生效的全局默认，也不得把样例文件当作隐式默认绑定。仓库内或插件包中的样例只作为示范或注册 provider 的输入。
- `FinanceOverrides` 精确引用 Profile 的稳定 ID 与内容摘要，只做稀疏原子覆盖，是 authoring 输入。
- `EffectiveFinanceConfig` 只能由合并器从 Profile 与 Overrides 生成；可以导出、导入、进入快照，但用户不能直接 authoring。导入时连同精确 Profile 与 Overrides 重新合并验证。

金额一律为 `{value, unit}` 原子：`value` 为十进制定点字符串（禁止 `float`、`NaN/Infinity`、`null`），`unit` 来自公共单位规范并经 `normalize_unit` 规范化；禁止部分金额（缺 `value` 或 `unit`）与 `null`。成本分量金额非负（允许 `0`，零费用类别、零固定成本均合法）。能源价格不设符号限制：允许有限正数、零与负数（见 `energy_prices` 行与「计价规则」）；禁止 NaN/Infinity。

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
  price_basis: tax_inclusive   # 文件内金额是否已含适用税目；本版分量直接使用金额，不做计税/扣税
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
      leap_year: false
      point_count: 8760
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
    rate: {value: "0.13", unit: "1"}
    applies_to: grid_import     # 引用 price_id（energy_prices 键）或 finance_types 分量路径
content_sha256: "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
```

示例摘要为占位值；正式文件必须写入按[规范化与摘要](#规范化与摘要)计算的真实摘要。同一载体允许登记多个价格条目（例如工商业与峰谷两套购电方案、多个出口电价），不需要改动核心 schema。

### 字段表

| 路径 | 必需 | 规则 |
|---|---|---|
| `schema` | 是 | 固定 `ies.finance-profile` |
| `schema_version` | 是 | 固定 `"1.0.0"` |
| `profile.id` | 是 | 稳定 ASCII 小写 ID（命名空间点分式、`lower_snake_case` 或短横线，同文件内保持一致），如 `cn-north-demo`；Overrides 与 Effective 沿用它做引用 |
| `profile.region` | 是 | 地区标识（展示与追溯，不参与计算） |
| `profile.currency` | 是 | `CNY` / `USD` |
| `profile.base_year` | 是 | 整数财务基准年（1900–2999） |
| `profile.price_basis` | 是 | `tax_inclusive` / `tax_exclusive`：文件内金额是否已含适用税目（与 `taxes` 配合阅读）；本版成本与价格分量直接使用文件金额原值，不执行计税或扣税运算，含税价格不得在计算中再次加税 |
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
| `energy_prices.*.ref` | time_series 必填 | 内容寻址对象引用（64 位小写十六进制，对象完整字节 SHA-256），指向经校验的完整年度能源价格序列；序列值同样为有限 Decimal、允许零与负 |
| `energy_prices.*.series_meta` | time_series 必填 | 不可变时间元数据：`resolution`（15min/30min/1h）、`leap_year`（bool）、`point_count`（整数）、`unit`；装配时与项目基线（resolution、闰年、点数）校验，不一致阻断且不重采样；Profile 不固定唯一项目分辨率 |
| `taxes` | 可选（无税目可写 `{}`） | 税目登记映射：`id` → `{display_name, tax_type, rate, applies_to}`，见下「税目登记」 |
| `content_sha256` | 派生 | 规范内容摘要（见「规范化与摘要」）；存在时必须与重算值一致，否则拒绝 |

单位与范围校验：

- 成本分量金额非负（允许 `0`）；每个线性分量的 `unit_cost` 单位与装配绑定的规范化 driver 单位相乘后必须得到 `currency`（upfront/period）或 `currency/year`（annual_fixed_om）量纲；
- 能源价格为有限 Decimal（正、零、负均合法），单位量纲 = currency /（对应 carrier 的能源量纲）；constant 的 `value.unit` 与 time_series 的 `series_meta.unit` 必须一致；禁止 NaN/Infinity；
- `taxes.*.applies_to` 引用的 price_id 或 `finance_types` 分量路径必须存在；`taxes.*.rate` 为比例（`0 ≤ rate ≤ 1`，无量纲 `unit: "1"`）。

### 税目登记

`taxes` 只做声明性登记，供人工核验与审计；**本版成本与价格计算不消费它**：

- `id`：唯一 `lower_snake_case` 稳定标识；
- `display_name`：人类可读税种名；
- `tax_type`：税种标识（如 `value_added_tax`）；本契约只登记不解析税种语义，不预建税务引擎；
- `rate`：法定税率（`Decimal` 比例，`0 ≤ rate ≤ 1`，`unit: "1"`）；
- `applies_to`：明确适用对象，二选一——引用本文件 `energy_prices` 的 `price_id`（如 `grid_import`），或 `finance_types` 的分量路径（如 `pv_system.upfront_capex`）；一个税目对应一个适用对象，多个对象登记多个税目。

金额口径与防重复加税：文件内金额按 `profile.price_basis` 解释（`tax_inclusive` = 已含适用税目）。本版把成本与价格分量按文件金额直接使用：不含税金额不会由系统自动计税，含税金额也不会被再次加税或拆税；税后换算（含税价扣税、不含税价计税后进入目标）不属于本契约定义范围，不得在 `taxes` 之外引入隐式税务行为。

### 时间口径与分量

`FinanceProfile` 分别输出时间口径明确的分量，**不静默相加成默认总成本**：

- `upfront_capex`：建设期一次性；金额量纲 = currency；
- `annual_fixed_om`：按年度计；金额量纲 = currency/年（按年值，不随计算窗口缩放）；
- `period_variable_om`、`period_energy_purchase`、`period_energy_sale`：按运行/计费窗口合计；金额量纲 = currency；两个能源分量为**有符号记账贡献**，符号约定见「计价规则」（分量生成与计价规则见下文「聚合与计价衔接」）。

口径换算与组合规则：

- 按年分量（currency/年）与窗口合计分量（currency）相加前，必须把按年分量乘以**显式时间跨度**换算到同一窗口后再相加。若计算窗口恰好是完整一年，示例把 `annual_fixed_om` 乘以显式时间跨度 `1 year`（对应项目基线的完整年度）得到该窗口金额（currency），随后与 `period_*` 相加；计算窗口不是完整一年时，不得默认按 1 年相加或做其他隐式缩放；
- 本契约不定义“总成本”聚合，也不提供隐式“系统总成本”标识符；`upfront_capex`（一次性）与运行期成本之间不存在本契约默认的转换——不做隐式年化，不隐含资本回收系数、利率或年限等值；
- 具体如何把一次性投资转换为可与运行期成本比较的规划分量（例如时间口径一致的显式目标分量），属于 `PlanningConfig` 的明确、版本化规则，须在规划配置契约中定义后再使用；本财务 Profile 不隐式定义或默认选择这种转换，也不因不做后评价而禁止规划配置契约定义此类显式分量；
- 后评价指标（NPV/IRR 等）不属于本契约输入（属 finance 模块长期蓝图，见[财务计算](../modules/finance.md)）；
- 规划表达式引用这些分量的合法标识符编码与单位字面量规则见[装配 YAML](assembly-yaml.md)「规划表达式中的财务分量标识符」。

## `ies.finance-overrides@1.0.0`

### 完整示例

```yaml
schema: ies.finance-overrides
schema_version: "1.0.0"
profile_ref:
  id: cn-north-demo
  content_sha256: "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
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
      leap_year: false
      point_count: 8760
      unit: CNY/kWh
content_sha256: "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
```

### 字段表与覆盖纪律

| 路径 | 规则 |
|---|---|
| `schema` / `schema_version` | 固定 `ies.finance-overrides` / `"1.0.0"` |
| `profile_ref` | 必填 `{id, content_sha256}`：`id` 与目标 Profile 一致，`content_sha256` 必须等于该 Profile 的真实内容摘要；不一致拒绝合并 |
| `finance_types` | 只允许覆盖目标 Profile **已存在** 的 `finance_type`；禁止新增或删除 `finance_type` |
| `finance_types.*` | 只允许按 Profile 实际存在的叶子路径整体替换 `Money` 原子：`<分量>.fixed`（如 `upfront_capex.fixed`）替换为 `{value, unit}`；`<分量>.linear.<driver>.unit_cost` 替换为 `{value, unit}`。被替换叶子必须已在 Profile 声明；禁止新增或删除分量、`fixed` 节点或 `driver`（`driver` 集合不可增删）、禁止改分量类型与 `cost_method`、禁止改 `unit`（规范化后必须与 Profile 相同）；替换值必须为完整 `{value, unit}` 原子，禁止部分金额与 `null`；成本金额非负 |
| `energy_prices` | 只允许覆盖 Profile **已存在** 的 `price_id`，禁止新增或删除 `price_id`。每个条目的 `energy_prices.<price_id>` 只写定价定义：`{kind: constant, value}` 或 `{kind: time_series, ref, series_meta}`（与 Profile 原 kind 无关）；`carrier` 与 `direction` 由 Profile 继承，在 Overrides 中出现即拒绝；`value.unit`（constant）或 `series_meta.unit`（time_series）必须与 Profile 原项相同；`time_series` 的 `ref` 指向经校验的完整年度价格序列对象，`series_meta` 必须完整并可与 Profile 引用不同序列；不允许部分混合替换（如只换 `ref` 保留旧 `series_meta`，或只换 kind 不带全量字段） |
| 禁改字段 | 除 `profile_ref` 外，顶层不得出现 `profile.*`、`currency`、`base_year`、`price_basis`、`cost_method`、`taxes`（税目只属于 Profile 登记，项目不得新增/覆盖/删除）、价格条目的 `carrier`/`direction` 或任何税率字段；出现即拒绝 |
| `content_sha256` | 派生；同 Profile 规则 |

覆盖只影响合并结果中的对应叶子；未覆盖条目原样保留（见 Effective 示例中未覆盖的 `battery_system`、`heat_supply`、`taxes` 等）。合并后重新跑完整校验，任一失败不产生 Effective。

## `ies.effective-finance-config@1.0.0`

### 完整示例

合并自上面示例的 Profile（`content_sha256 = ccc…`）与 Overrides（`content_sha256 = ddd…`）。未覆盖条目全部保留：

```yaml
schema: ies.effective-finance-config
schema_version: "1.0.0"
profile_id: cn-north-demo
profile_sha256: "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
overrides_sha256: "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
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
  pv_export:                                           # 被覆盖定价：换用另一完整年度序列
    carrier: electricity
    direction: sale
    kind: time_series
    ref: "9999999999999999999999999999999999999999999999999999999999999999"
    series_meta:
      resolution: 1h
      leap_year: false
      point_count: 8760
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
    rate: {value: "0.13", unit: "1"}
    applies_to: grid_import
content_sha256: "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
```

生成规则：

- `EffectiveFinanceConfig` 只能由合并器生成：`merge(FinanceProfile, FinanceOverrides | None)`；无覆盖时也必须显式携带空 Overrides 摘要（`overrides_sha256` = 空 Overrides 文档的摘要，见下），不允许省略来源字段。
- 合并结果重新跑完整校验：币种/`price_basis`/`cost_method` 一致性、分量完整性、`price_id` 唯一性与存在性、每项 `carrier`/`direction` 合法且与 Profile 一致、价格有限性（正零负均允许，禁止 NaN/Infinity）、`time_series` 元数据完整、单位量纲规则、`taxes` 引用完整性全部重验；任一失败不产生 Effective。
- 稀疏覆盖只替换被覆盖叶子：未覆盖的 `finance_types`、能源价格与 `taxes` 全部原样保留，不允许省略、裁剪或按“示例只写用到的部分”生成。
- `EffectiveFinanceConfig` 可以导出、导入、进入快照；导入时连同精确 Profile 与 Overrides 重新合并验证（`profile_ref` 摘要、`overrides_sha256`、重算 `content_sha256` 全部一致），任何不一致拒绝导入。
- 装配与计算只消费该不可变快照：不运行期继承 Profile、不读取“最新地区价格”、不静默默认值。

## 规范化与摘要

统一算法：解析 YAML（安全子集）→ 校验 → 移除派生摘要字段 → 生成唯一规范 YAML 字节 → `SHA-256(canonical_bytes)` → 写 `content_sha256`。**`content_sha256` 不参与自身摘要。**

区分两类摘要，用途与验证对象不同，**不要求相等**：

- **对象字节摘要（bytes SHA-256）**：对落盘/存储对象文件的**完整字节**（含文件内 `content_sha256` 字段）计算，用于内容寻址（`object_id`、`ref.sha256`）与存储/项目包对象校验；
- **规范内容摘要（`content_sha256`）**：移除派生摘要字段后的规范 YAML 字节摘要，用于内容身份、覆盖引用与血缘（`profile_ref`、`profile_sha256`/`overrides_sha256`/`content_sha256`）。

由于文件携带自身 `content_sha256` 字段，同一文件的两个摘要通常不相等；两者分别验证、缺一不可（验证顺序见「装配与项目包引用」）。

| 文档 | 摘要输入（移除派生字段后） | 语义变化 |
|---|---|---|
| FinanceProfile | `schema`、`schema_version`、`profile.*`、`finance_types.*`（全部分量）、`energy_prices.*`（price_id、carrier、direction 与定价定义）、`taxes.*` | Profile 内容摘要，被 Overrides `profile_ref` 与 Effective `profile_sha256` 引用 |
| FinanceOverrides | `schema`、`schema_version`、`profile_ref`（含 Profile 摘要）、覆盖子树 | Overrides 内容摘要，被 Effective `overrides_sha256` 引用 |
| EffectiveFinanceConfig | `schema`、`schema_version`、`profile_id`、`profile_sha256`、`overrides_sha256`、`currency`、`base_year`、`price_basis`、`cost_method`、合并后的 `finance_types`、`energy_prices`、`taxes` | 自身内容摘要 = 装配/规划/计算引用的权威摘要 |

规范字节由公开纯函数生成：映射键稳定排序、金额用定点十进制字符串、单位保留原始拼写（不做数值换算，仅校验时规范化）、注释与空行移除、LF 换行、非 ASCII 保留；相同语义产生相同字节。规范化算法变化必须升级算法版本并保留历史解释能力。无覆盖时的空 Overrides 文档：

```yaml
schema: ies.finance-overrides
schema_version: "1.0.0"
profile_ref:
  id: <profile-id>
  content_sha256: <profile-content-sha256>
```

其规范字节摘要即空覆盖的 `overrides_sha256`；该文档本身有效（没有覆盖任何内容），合并结果等于 Profile。

## 聚合与计价衔接（装配绑定引用）

装配把财务 driver 绑定到实例的设备 `property`/`interface`，把能源价格（`price_id`）经 `tariff_bindings` 绑定到计费点实例/接口时，必须声明下列聚合之一：

| 枚举 | 适用目标 | 语义 |
|---|---|---|
| `scalar` | 设备 `property`（非时变标量） | 直接取实例化后的 property 值；只用于成本 driver 的年度/一次性量，不用于能源计价；时序 interface 禁止使用 |
| `integrate_positive` | 非负功率/流率 `interface` | 每步乘 `step_hours` 积分：`e[t] = p[t] × step_hours[t]`，得到与价格序列同轴的能量序列 |
| `sum_positive` | 区间累计量 `interface` | 接口序列本身即为区间累计能量/累计量序列，直接作为 `e[t]`，不再二次积分 |

时序 `interface` 必须写 `aggregation`；`property` 只能对应 `scalar`，装配映射 `property` 时 `aggregation` 字段可省略（语义固定为 `scalar`）。本契约不接受模糊的双向净值或绝对值：购/售分别绑定方向明确的接口（如 `electricity_in`/`electricity_out`），能量/流量序列在聚合前必须非负并阻断负值，不自动取绝对值；价格本身的零/负值与能量非负是两个独立约束，不得混淆。

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

- 装配 YAML 的 `finance` 节使用**精确引用**指向 Effective YAML：`ref`（`kind: object` 或包内 `relative_file`）携带对象 ID 与**对象字节摘要**；血缘字段（`profile_id`/`profile_sha256`/`overrides_sha256`/`content_sha256`）携带**规范内容摘要**。装配不在内内联一份完整配置；`ValidatedAssemblyArtifact` 固定引用与两类摘要。
- 验证顺序：读取被引用对象 → 对完整字节重算 SHA-256，必须等于 `ref.sha256`（对象完整性）→ 解析并重算规范内容摘要，必须等于 `content_sha256` 且等于 Effective 文件自身 `content_sha256` 字段（内容身份）。两类摘要分别验证，互不相等也合法。
- 项目包可携带 `finance_profile.yaml`、`finance_overrides.yaml`、`effective_finance.yaml` 三个文件；导入时按上述顺序分别验证各文件，并对 Profile、Overrides 与 Effective 重新合并验证。
- 数据库内部存储使用列/JSON 表示，不强制 YAML；HTTP JSON DTO 仍为 JSON。
