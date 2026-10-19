# 财务模型 YAML 契约

> 契约标识与版本：`ies.financial-model@1.0.0`（新增）
> 推荐文件名（项目包内固定名称）：`financial_model.yaml`
> 文档状态：生效目标契约；本页只定义目标文件语义，不声明实现进度。

本页定义财务模型的完整 YAML 结构与校验语义，是[财务 YAML 契约](finance-yaml.md)与[财务计算](../modules/finance.md)的补充。财务模型定义收入、费用、计税、折旧和现金流的具体计算关系；程序提供通用执行框架；项目选择模型并显式提供参数。本页字段与 `backend/iesplan/finance/schema/financial-model-1.0.0.schema.json` 一一对应，完整示例见[示例目录](examples/)。

财务模型不是设备技术模型：不进入设备选择器，不随设备 revision 固定，不携带价格、成本或实例绑定。模型文件只包含规则；参数值与输入绑定属于项目财务配置。模型规则随任务固定，历史结果不读取最新模型重算。

```text
ies.financial-model@1.0.0（财务模型：人工 authoring，不可变规则）
        │                                       │
        │ 项目引用模型 ID + revision              │ 已接受 revision 的规范化正文
        │ 并提供参数值与输入绑定                   │
        ▼                                       │
ies.finance-overrides@3.0.0                      │
（增加 model_ref + parameters_values + input_bindings）
        │                                       │
        ▼                                       ▼
merger（确定性合并 + 完整校验 + 冻结正文副本）── 生成 ies.effective-finance-config@3.0.0
        │        （包含 model_ref + model_rules + parameters_values + input_bindings）
        ▼
assembly / computation / 项目包（精确引用 Effective YAML）
        │
        ▼
GeneratorProvider 五项直接输入之一：finance = EffectiveFinanceConfig（不读模型库、目录或对象存储）
```

- 财务模型以稳定 ID 与 revision 标识；发布后不可变，修改形成新 revision。模型文件只包含规则，不包含参数值、实例 ID 或绑定。
- 程序可附带普通模型模板（如 `ies.built-in.linear_income_tax`），用户可选择、复制和修改；内置模板与用户模型遵循同一公开契约，不隐式绑定。
- 模型既为求解提供影响方案选择的关系，也为评价提供同源明细；不在生成器与评价器分别维护两套业务公式。
- 影响优化的模型关系通过公开契约参与生成与求解，并与求解后评价使用同一冻结模型和参数；不支持的能力明确拒绝，不退化为仅事后补算。
- 不新增第六类核心输入、全局配置副本、财务任务系统、通用编程语言、商城或插件热加载。

## 模型身份与修订

财务模型文件必须声明以下顶层字段：

```yaml
schema: ies.financial-model
schema_version: "1.0.0"
model:
  id: ies.built-in.linear_income_tax       # 稳定命名空间 ID
  revision: 1                               # 单调递增 revision，发布时固定
  names:
    zh-CN: 线性所得税直线折旧模型
    en: Linear income tax with straight-line depreciation
  description: 适用于项目投资口径的简化税后评价模型
  investor_perspective: project_equity      # 核算主体口径（模型身份，非程序分支）
  required_capabilities:
    - affine
    - piecewise_linear
```

### 字段表

| 路径 | 必需 | 规则 |
|---|---|---|
| `schema` | 是 | 固定 `ies.financial-model` |
| `schema_version` | 是 | 固定 `"1.0.0"` |
| `model.id` | 是 | 稳定命名空间 ID：`ies.built-in.<slug>` 或 `user.<public_namespace>.financial.<slug>`；`lower_snake_case`、不含 `__` |
| `model.revision` | 是 | 单调递增正整数；发布时固定，相同内容幂等返回同一 revision |
| `model.names` | 是 | 本地化显示名映射（至少 `zh-CN` 或 `en` 之一） |
| `model.description` | 是 | 非空字符串，描述模型适用范围 |
| `model.investor_perspective` | 是 | 核算主体口径标识（如 `project_equity`）；是模型的身份声明，不是程序分支枚举；允许自定义主体口径 |
| `model.required_capabilities` | 是 | 生成器能力标识数组，取值与语义见「生成器能力需求」；声明必须等于按实际语法推导的需求集合 |

模型内所有声明 ID 为 `lower_snake_case`、不含 `__`，且不得使用保留名 `year`、`max`、`min`。命名空间与对应关系：

- **各节内部唯一**：`parameters` 键、`inputs[].id`、`relations[].id`、`state[].id`、`outputs[].id` 分别在各自节内不得重复。
- **跨节默认互斥**：参数、输入、状态、关系式、输出各自构成独立命名空间；同一 ID 不得同时出现在两个命名空间（如参数与输入同名、输入与关系式同名、`npv` 输出与任何已用 ID 同名），出现即拒绝。
- **仅两类既定对应允许同名**：① `state[].id` 与它的同 ID 更新关系式（恰好一个，必需）；② `kind: output` 关系式与其同 ID 的 `series` 输出（恰好一个，必需）。这两类同名是同一逻辑量的定义配对，不构成声明冲突；除此之外不得成为同名的“第二个来源”。
- 不含 `__` 使模型 ID 与财务分量标识符空间（`<instance_id>__<分量>`、`<binding_id>__<分量>`）天然分离。

## 规则、参数与事实的归属

财务模型涉及四类内容，权威归属如下：

| 类别 | 权威来源 | 载体 | 可变性 |
|---|---|---|---|
| **规则**（关系式、状态声明、参数槽位声明、输入声明、输出声明） | 模型文件 | `financial_model.yaml` | 不可变（发布后固定） |
| **参数值**（税率、折旧年限等） | 项目财务配置 | `FinanceOverrides.parameters_values` | 工作区可变，任务冻结时固定 |
| **输入绑定**（哪个计量绑定/设备实例为模型输入供数） | 项目财务配置 | `FinanceOverrides.input_bindings` | 工作区可变，任务冻结时固定 |
| **事实**（已物化分量、同次建设总额、付款额） | 同次求解与评价确认 | 评审通过的解析输入 | 同次确定 |

- **规则**在模型文件中定义，不可变；修改规则形成新 revision。
- **参数值**由项目财务配置提供，工作区可修改；任务提交时随快照冻结，历史任务不读取最新值重算。
- **输入绑定**由项目财务配置逐输入提供；用户数据（参数值、实例 ID、绑定 ID）不写入模型规则。
- **事实**来自同次求解结果（已物化分量）、同次建设总额（规划求解产生）和评价确认（付款计划 × 建设总额）。

### 参数槽位声明

模型文件声明参数槽位（类型、显示名、期望单位、必要范围），不包含值：

```yaml
parameters:
  income_tax_rate:
    type: ratio
    display_name: 所得税率
    unit: "1"                   # 期望单位
    range: {min: "0", max: "1"} # 必要范围
  depreciation_years:
    type: integer
    display_name: 折旧年限
    unit: "1"
    range: {min: 1, max: 50}
```

| 路径 | 必需 | 规则 |
|---|---|---|
| `parameters` | 是 | 参数槽位映射（可为空 `{}`）；键名为稳定 `lower_snake_case` ID |
| `parameters.*.type` | 是 | `ratio` / `money` / `integer` / `decimal` |
| `parameters.*.display_name` | 是 | 人类可读参数名 |
| `parameters.*.unit` | 是 | 期望单位；`ratio`/`integer` 为 `"1"`；`money` 为模型要求的币种单位（如 `CNY`），合并时与 Profile `currency` 一致校验；`decimal` 为声明的显式单位 |
| `parameters.*.range` | 是 | 必要范围 `{min, max?}`：`min` 必须声明，`max` 按需声明；数值为十进制字符串或数字；提供值必须落在范围内（`ratio` 额外要求 `0..1`、`integer` 要求整数值、`money` 要求非负） |

### 参数值与输入绑定（项目财务配置）

参数值与输入绑定由项目财务配置（`FinanceOverrides`）提供，不写入模型文件；字段级定义以[财务 YAML 契约](finance-yaml.md)为唯一权威。本样例对应的完整文件见 `examples/finance-overrides-linear-income-tax.yaml` 与 `examples/finance-overrides-progressive-tax-ddb.yaml`：

```yaml
# finance_overrides.yaml（节选）
schema: ies.finance-overrides
schema_version: "3.0.0"
profile_ref:
  id: cn-north-demo
model_ref:
  id: ies.built-in.linear_income_tax
  revision: 1
parameters_values:
  income_tax_rate: {value: "0.25", unit: "1"}
  depreciation_years: {value: "10", unit: "1"}
input_bindings:
  operating_revenue:
    - tariff_binding: pv_export_1      # 只选该计量绑定；同类 pv_export_2 未列入即不参与
  operating_cost:
    - instance: pv_system_1            # 显式列出同类设备成本分量
    - instance: pv_system_2
```

绑定规则（本页定义语义，字段表见财务 YAML 契约）：

- `input_bindings` 的键必须恰好是模型 `inputs` 中 `source: materialized_component` 的输入 ID：缺任一输入即拒绝，出现非物化分量输入（`construction_total`、`evaluation_confirmation` 来源）的键即拒绝。
- 每个键的取值是**非空列表**，列表项为单键判别对象：`{tariff_binding: <binding_id>}`（能源计费分量）或 `{instance: <instance_id>}`（设备成本分量）。输入值 = 所列各项解析出的声明分量之和再按 `sign` 处理；**不隐式聚合**——未列入的同类别量不参与，多分量组合必须显式列出。
- 列表内每一项都必须实际产生该输入声明的 `component`：`tariff_binding` 项要求该 `binding_id` 在 `tariff_bindings` 中存在且其价格 `direction` 产生所述能源分量；`instance` 项要求实例存在、为 `type: costed` 且其 `finance_type` 声明所述成本分量。存在性、分量可得性与单位相容在装配边界核对，不满足即阻断。
- 两个同类计量绑定、两个同类设备实例因此各自唯一定位：选择即“列入该项”，组合即“列入多项”，两者都不依赖名称猜测或隐式求和。
- 参数值必须覆盖模型声明的全部参数槽位，类型、单位与范围逐项校验；未知键、缺项、单位不一致或越界即拒绝。

## 时间轴与年度语义

财务模型的时间轴是**年度级**，独立于设备方程的 step 级时间轴。模型接收年度归集后的分量，输出年度现金流。

```text
建设期          运营期
t=0    t=1      t=K      t=K+1        t=K+N
│──────│────────│────────│────────────│
开工   建设付款  投产    第1运营年末   第N运营年末
```

- `K = construction_period`、`N = operation_years`，`evaluation_years = K + N`，三者来自任务提交冻结的**评价确认**（`evaluation` 块），模型不声明、不覆盖，也没有业务默认值。
- 评价确认的持久化归属是任务快照（`CalcSnapshot.evaluation`，见[公共契约](../contracts.md)）；生成阶段的唯一消费载体是五项直接输入中的第三项（同次物化事实集合 `MaterializedSet.evaluation`）：application 编排从快照原样搬运业务字段，生成器只读该块，不读快照、数据库或对象存储，不新增第六输入，也不重复校验。`construction_payment` 与 `maintenance_expenses` 输入即从该块取值（见「输入来源与形状」）。
- `year=0`：开工年（建设期起点）。
- `year=K`：投产年（建设期末，第 0 个运营年）；投产年没有前序完整运营年，运行事实为 0。
- `year=K+j`：第 j 个完整运营年末（j=1..N）；第 j 个运营年的年度归集量对应财务年 `K+j`。
- 评价期现金流覆盖 `CF[0..K+N]`，长度 `evaluation_years + 1`。
- 建设期与投产年（`year=0..K`）：只有建设付款（按评价确认计划），运行事实为 0；运营期（`year=K+1..K+N`）：有运行收益和成本。
- `op_year(y) = y − K`：财务年对应的运营年序号（建设期为负值，投产年为 0）。

### 时间索引与求值顺序（唯一定义）

- `[year]`：当前财务年的值；引用关系式、状态变量或年度序列输入时使用。
- `[year-1]`：上一财务年的值；**只允许引用 `state` 声明的状态变量**，引用其他 ID 即拒绝。
- 禁止 `[year+k]`（k>0，未来引用）、禁止 `[year-k]`（k>1）。
- 参数与标量输入不带索引（裸引用，广播到每年）；年度序列输入必须带 `[year]` 索引。
- 财务年按 `y = 0..K+N` 顺序求值。每年内部：
  1. 先读取上一财务年最终值：状态变量的 `[year-1]` 引用在 `year=0` 时读取 `initial`；其余年份读取上一年结束时固化的状态值。
  2. 当年关系式按依赖拓扑序计算：`[year]` 引用读取**当年已求值**的同一 ID；依赖无法推进（循环或未声明）即拒绝。
  3. 每个状态变量的当年值等于其同 ID 更新关系式的当年值；所有关系式（含状态更新）计算完成后，当年值即固化。
- 求值顺序不依赖声明先后；只由依赖关系决定，保证模型文件的规则顺序不改变结果。

### 有效区间（`active`）与区间外语义

关系式可声明有效区间（`active`），限定关系成立的运营年范围：

```yaml
relations:
  - id: depreciation
    expression: initial_cost / depreciation_years
    unit: CNY
    kind: intermediate
    active: {from_op_year: 1, to_op_year: depreciation_years}  # 第 1 至 depreciation_years 个运营年
```

- `from_op_year`/`to_op_year` 为**运营年**（1-based）：每个端点是正整数或整型参数引用（如 `depreciation_years`），参数端点按冻结参数值解析；要求 `from_op_year ≤ to_op_year`。区间对应财务年 `K+from_op_year .. K+to_op_year`，与 `K` 的映射由公开时间语义给出，不硬编码绝对年。
- 未声明 `active` 的关系式在所有年份有效。
- **区间外语义（唯一）**：非状态关系式在区间外取值为 0（折旧等费用类为 0；其所有引用者读取 0）；状态更新关系式在区间外不执行，状态保持上年值（`state[year] = state[year-1]`，`year=0` 时为 `initial`）。不存在“无值”或“未约束”的第三种解释。

## 关系式语法

财务模型关系式复用 `core/equation_grammar` 的白名单词法，并扩展年度时间索引与受控函数。

### 允许的语法

- 标识符：`[A-Za-z_][A-Za-z0-9_]*`，必须引用已声明的参数、输入、状态或关系 ID
- 数字：整数或十进制
- 运算符：`+`、`-`、`*`、`/`
- 括号：`( )`
- 时间索引：`[year]`、`[year-1]`
- 受控函数：`max(a, b)`、`min(a, b)`（白名单，禁止其他函数调用）

### 语义限制

- **决策相关性**：输入（`materialized_component`、`construction_total`、`evaluation_confirmation`）在求解前均决策相关（取决于容量/运行决策）；数值字面量与参数为常数。
- **乘法**：`*` 至少一侧必须是常数；两个决策相关表达式相乘不是仿射，也不属于 `1.0.0` 的能力集合，提交边界拒绝——不近似、不新增未版本化能力。
- **除法**：除数只允许是数值字面量或参数（求解时取常数）；除数为决策相关量（实例变量、其他关系式等）即拒绝——不近似、不回退。
- 关系式与状态引用必须带 `[year]` 或 `[year-1]` 索引；参数与标量输入裸引用；年度序列输入带 `[year]` 索引。
- 单位量纲校验：表达式内所有项的量纲必须与 `relations[].unit` 一致；金额单位量纲 = currency（如 `CNY`），比例单位量纲 = 无量纲（`unit: "1"`）；复用 `core.units` 的单位解析与量纲校验。

### 禁止的语法

- 函数调用（除 `max`/`min` 外）
- 属性访问（如 `obj.attr`）
- 任意下标（如 `arr[0]`）、对 `[year]`/`[year-1]` 之外的时间索引
- 幂运算（`^`）、模运算（`%`）、比较运算
- 未来引用（`[year+k]`，k>0）、深层过去引用（`[year-k]`，k>1）
- 模块路径、反射、文件、网络、环境变量、shell
- 任意代码或可执行入口

## 状态变量与跨年结转

状态变量用于表达跨年累积量（如未抵扣亏损余额、资产账面净值）：

- 状态变量通过 `state` 节声明，具有 `initial` 初值；`initial` 是**开工前（`year=-1`）的存量值**，不是 `year=0` 的当前值。`year=0` 的 `[year-1]` 引用读取 `initial`，随后该年的关系式（含状态更新）正常求值。
- 每个状态变量必须有**恰好一个同 ID 更新关系式**（`kind: intermediate`），该关系式的当年值即状态变量当年的值；禁止引用自身当前年（循环拒绝）。
- 状态初值两种形态：
  - `{value, unit}`：十进制常量初值（如未抵扣亏损余额 `{value: "0", unit: CNY}`）。
  - `{input: <input_id>}`：引用**标量形状输入**（如 `construction_total` 来源的 `initial_cost`），把同次建设总额作为期初存量；年度序列输入不得用作初值（初值在 `year=0` 前解析）。
- 区间外状态更新规则见「有效区间与区间外语义」。

```yaml
state:
  - id: book_value
    unit: CNY
    initial: {input: initial_cost}           # 从标量输入取值（同次建设总额）
    description: 资产账面净值
  - id: loss_carryforward_balance
    unit: CNY
    initial: {value: "0", unit: CNY}         # 常量初值
    description: 未抵扣亏损余额（本模型显式结转；不表述任何法定到期规则）
```

| 路径 | 必需 | 规则 |
|---|---|---|
| `state` | 是 | 状态变量数组；可为空 `[]` |
| `state[].id` | 是 | 稳定 `lower_snake_case` ID；必须有同 ID 更新关系式 |
| `state[].unit` | 是 | 状态变量单位 |
| `state[].initial` | 是 | 初值 `{value, unit}` 或 `{input: <标量输入 ID>}` |
| `state[].description` | 是 | 非空字符串 |

## 输入声明

输入是模型引用的外部事实。模型只声明“需要哪一类分量”，**不声明具体实例或绑定**；具体供数对象由项目财务配置的 `input_bindings` 提供。

```yaml
inputs:
  - id: operating_revenue
    source: materialized_component
    component: period_energy_sale
    unit: CNY
    sign: negative                             # 正价时 period_energy_sale 为负，收入取其相反数
  - id: operating_cost
    source: materialized_component
    component: period_variable_om
    unit: CNY
    sign: positive
  - id: initial_cost
    source: construction_total
    component: construction_total
    unit: CNY
    sign: positive
  - id: construction_payment
    source: evaluation_confirmation
    component: construction_payment
    unit: CNY
    sign: positive
  - id: maintenance_expense
    source: evaluation_confirmation
    component: maintenance_expenses
    unit: CNY
    sign: positive                             # 已确认的维护/换新金额（费用向）
```

| 路径 | 必需 | 规则 |
|---|---|---|
| `inputs` | 是 | 外部事实引用数组；至少一项 |
| `inputs[].id` | 是 | 稳定 `lower_snake_case` ID，在关系式中引用 |
| `inputs[].source` | 是 | `materialized_component` / `construction_total` / `evaluation_confirmation` |
| `inputs[].component` | 是 | 引用的分量：`materialized_component` 仅允许 `annual_fixed_om` / `period_variable_om` / `period_energy_purchase` / `period_energy_sale`；`construction_total` 仅 `construction_total`；`evaluation_confirmation` 仅 `construction_payment` / `maintenance_expenses` |
| `inputs[].unit` | 是 | 期望单位（量纲校验） |
| `inputs[].sign` | 是 | `positive` / `negative`：输入值 = 解析分量 × 符号系数（`negative` 取相反数） |

### 输入来源与形状

| 来源 | 值 | 形状 | 供数要求 |
|---|---|---|---|
| `materialized_component` | 项目绑定解析出的已物化财务分量（逐财务年） | 年度序列 | 必须经 `input_bindings` 逐输入给出绑定；不隐式聚合 |
| `construction_total` | 同次建设的总额 | 标量 | 求解前是决策表达式（由规划变量与快照成本函数构成，`initial_cost` 即该表达式）；求解后是同次解的事实；不得用事后事实反推求解，也不与 `upfront_capex` 分量输入重复计入 |
| `evaluation_confirmation`（`construction_payment`） | 评价确认的建设付款 | 年度序列 | `construction_payment[year] = construction_total × Σ_{t=year} ratio_t`（`t` 为确认中自开工起的年时点）；无对应时点为 0 |
| `evaluation_confirmation`（`maintenance_expenses`） | 评价确认的维护／换新清单 | 年度序列 | `maintenance_expenses[year]` = 确认清单中 `year` 等于该财务年的金额之和（同一财务年多笔显式求和）；发生年范围为 `K+1..K+N`（提交边界已限定），其余年份为 0 |

- 形状与广播：标量输入在所有财务年取同一值（广播）；年度序列输入按财务年取值；`materialized_component` 序列输入只在有对应运营年的财务年（`K+1..K+N`）有值，`year=0..K` 为 0；`construction_payment` 按评价确认的付款计划取值，建设期非零；`maintenance_expenses` 按确认清单在实际发生年为确认金额、其余年为 0；标量与序列运算时标量广播到每年。
- 符号约定：`period_energy_sale` 正价时为负记账贡献（见[财务 YAML 契约](finance-yaml.md)「计价规则」），收入输入声明 `sign: negative` 取其相反数；`period_energy_purchase` 正价时为正记账贡献，成本输入声明 `sign: positive`；`construction_total`、`construction_payment` 与 `maintenance_expenses` 为正数（费用向）。
- 维护／换新必须由模型显式消费：模型声明 `maintenance_expenses` 输入并在其关系式中决定税额与现金流处理；框架不在模型之外另扣一次。任务确认了非空清单而所选模型未声明该输入时，提交边界明确拒绝——不静默忽略、不自动均摊、不猜测发生年。

## 输出声明

模型输出通过 `outputs` 节声明。输出是规划与评价可引用的唯一模型接口；每个输出条目是判别联合，`kind` 决定形态：

```yaml
outputs:
  - id: before_tax_cashflow
    kind: series
    unit: CNY
    cashflow: {tax_basis: before_tax}          # 机器可识别的现金流口径声明
    description: 税前现金流（逐年序列）
  - id: after_tax_cashflow
    kind: series
    unit: CNY
    cashflow: {tax_basis: after_tax}
    description: 税后现金流（逐年序列）
  - id: before_tax_npv
    kind: npv
    source: before_tax_cashflow
    unit: CNY
    description: 税前净现值（按冻结评价期与折现率贴现）
  - id: after_tax_npv
    kind: npv
    source: after_tax_cashflow
    unit: CNY
    description: 税后净现值（按冻结评价期与折现率贴现）
```

| 路径 | 必需 | 规则 |
|---|---|---|
| `outputs` | 是 | 输出声明数组；至少一项 |
| `outputs[].id` | 是 | 稳定 `lower_snake_case` ID；不含 `__` |
| `outputs[].kind` | 是 | `series`（逐年序列）或指标：`npv` / `irr` / `payback_static` / `payback_dynamic` |
| `outputs[].cashflow` | 仅 `kind: series` 可选 | `{tax_basis: before_tax\|after_tax}`：机器可识别的税前/税后口径声明；声明后该序列可作指标 `source`；未声明的序列不得被指标引用 |
| `outputs[].source` | 指标必需 | 必须是本模型一条**已声明 `cashflow`** 的 `kind: series` 输出 ID；`kind: series` 条目不得出现 |
| `outputs[].unit` | 是 | `npv` 与 `source` 单位一致（金额=声明币种）；`irr` 固定 `"1"`；`payback_static`/`payback_dynamic` 固定 `"year"` |
| `outputs[].description` | 是 | 非空字符串 |

- `kind: series` 输出的 `id` 必须对应一个 `kind: output` 的关系式（一一对应：每个 `kind: output` 关系式都必须被声明为 `series` 输出，每个 `series` 输出都必须由 `kind: output` 关系式支撑）。
- 指标输出不新增关系式，由执行框架按下面公开语义从 `source` 序列计算。
- 指标只消费模型**显式声明**的现金流：`source` 指向哪条序列、口径是税前还是税后，只由 `cashflow.tax_basis` 与 `source` 解释；不假设任何 ID 命名（如 `before_tax_*`/`after_tax_*`），不解析 `description`。

### 指标语义与规划引用

指标输出把模型声明的现金流序列构成可被展示、导出与规划引用的结果：

- 现金流符号：序列值为带符号净额（正 = 现金流入、负 = 流出）；建设付款在其发生时点为负。符号由模型关系式显式书写，指标按序列值直接累计，不另设方向。
- `npv`：`npv = Σ_{y=0..K+N} source[y] / (1 + r)^y`；`K`、`N` 取冻结评价确认的 `construction_period`/`operation_years`，`r` 取冻结 `discount_rate`（无量纲比例，零合法，无业务默认值）；`y=0` 不折现；单位等于 `source` 声明单位。
- `irr`：满足 `Σ_{y=0..K+N} source[y] / (1 + x)^y = 0`（`x > -1`）的贴现比例；状态采用[财务计算](../modules/finance.md)公开语义（`normal/no_solution/multiple/degenerate/out_of_domain/numeric_failure`），仅 `normal` 时有值，其余状态值为空。
- `payback_static`：从 `y=0` 起累计 `source`，累计值首次非负的年份；跨过零点的年份按 `t-1 + (-C_{t-1}) / source[t]` 线性插值（`C_{t-1}` 为上一累计值，`source[t] > 0`）；评价期内不回收为无值并附 `not_recovered`。
- `payback_dynamic`：对同一 `source` 先按 `r` 贴现（`source[t]/(1+r)^t`）再按静态回收期同一规则；`r=0` 时与静态回收期一致。
- 规划引用：只有 `kind: npv` 输出是标量目标（关于现金流的仿射/分段结构，可进入求解）；`irr` 与两种回收期只作结果指标，不得作为规划目标或约束项——需要目标时由模型声明相应 `npv` 输出。
- 命名与冲突：模型输出 ID 不含 `__`，与财务分量标识符空间（`<前缀>__<后缀>`）分离；模型输出进入规划表达式符号表后，规划配置不得以规划变量、模型技术量或其它标识符重定义同名符号（与装配既有保留集合规则一致，冲突即拒绝）；模型未声明的名字（如未声明 `after_tax_npv` 的模型）不存在该符号，引用即拒绝。
- 系统**不提供默认目标**：目标方向（`minimize`/`maximize`）与表达式由规划配置显式选择。完整规划样例见 `examples/planning-linear-income-tax.yaml`、`examples/planning-progressive-tax-ddb.yaml` 与 `examples/planning-maintenance-metrics.yaml`：

```yaml
objective:
  sense: maximize
  expression: after_tax_npv   # 普通标识符，非属性访问；模型声明的 npv 输出
```

### 税前/税后结果与版本归属

- 需要税前/税后结果的任务由模型声明对应现金流输出（`cashflow.tax_basis`）及其消费的指标输出；口径由声明识别，不由 ID 命名或 `description` 猜测。
- 封存结果的字段、口径与版本归属见[财务计算](../modules/finance.md)「封存评价块」：模型身份、逐年明细与指标值属**封存评价块自身的契约** `ies.financial-evaluation@1.0.0`，块内显式声明自己的 `schema`/`schema_version`；不继承 `CalcSnapshot` 的版本号（`5.0.0` 是任务输入快照 schema），也不与之机械同步；旧快照保持原封存块，不回填、不重算。
- 结果读取只消费同一任务的封存事实；历史任务不复制最新模型/参数/价格重算，也不因模型新 revision 改变。

## 生成器能力需求

模型通过 `required_capabilities` 声明需要的数学能力；生成器通过 `GeneratorDescriptor.capabilities` 声明支持的能力（见[计算生成器](../modules/generators.md)）。能力标识使用同一词汇，本契约定义如下闭集（`1.0.0`）：

| 能力标识 | 语义 | 何时必须声明（推导规则） |
|---|---|---|
| `affine` | 全部关系式在决策量上仿射：`+ - * /`（乘法至少一侧为数值或参数、除数仅为数值或参数）、年度索引、跨年线性结转 | 所有模型都包含（无 `max`/`min` 时为唯一项） |
| `piecewise_linear` | 关系式中的 `max`/`min` 需要精确分段线性化能力 | 任一关系式出现 `max(` 或 `min(` 时追加 |
| `mutex_bigm` | 设备方程域的确定性互斥能力（`x*y=0`、有限界二进制 Big-M） | 财务模型语法不产生该需求，不声明 |

- **声明必须等于推导集合**（不是自由声明）。推导按实际语法的**决策相关性**判定：① 先按「关系式语法」的限制核对（乘法至少一侧为常数、除数必须是常数）；出现决策相关表达式相乘或以决策相关量为除数时，模型不被 `1.0.0` 支持，提交边界拒绝——不把此类算术当作仿射，也不近似。② 通过后：全部模型含基础 `affine`（`+ - * /`（常除）、年度索引、跨年线性结转）；任一关系式出现 `max`/`min` 时追加 `piecewise_linear`。多写、少写、写错都在模型校验拒绝。
- 提交边界核对：推导集合 ⊆ 所选 `GeneratorDescriptor.capabilities`，否则明确拒绝；不近似、不静默降级、不回退为仅税前计算。
- 能力只在描述符声明；`CalculationConfig` 保持封闭，不承载模型需求或能力自报。
- 本页合成样例需要 `piecewise_linear`；只声明 `affine` 与 `mutex_bigm` 的 provider 不能接受它们。文件结构合法不等于任意 provider 均能执行，实际可用能力以所选 provider 的公开描述符为准。

### max/min 精确支持条件

声明 `piecewise_linear` 的生成器只有在满足以下全部条件时才能支持含 `max`/`min` 的模型；任一条件不成立即公开拒绝：

1. **精确表示**：以辅助变量与二进制指示、经显式约束精确线性化（如 `max(0, x)` 的析取式），不采用松弛近似；
2. **有限界来自公开声明**：所需上下界必须由规划变量上下界、参数范围、输入界与成本函数推导得出；推导不出有限界即拒绝，不得为通过能力核对而猜测界或要求用户补声明无依据的容量上限；
3. **不猜 M/ε**：不得猜测 big-M 或容差，不得使用求解器默认界；
4. **不依赖目标结构**：不得依赖“目标恰好会把辅助变量压到正确一边”等巧合；正确性由约束保证，而非由目标或求解器行为保证。

## 核算主体

`model.investor_perspective` 是模型的核算主体口径声明，是模型的身份标识，不是程序分支枚举：

- 项目投资口径（如 `project_equity`）与投资方/用能方口径（如 `equity_investor`、`energy_user`）是**不同的评价基础**，不是同一模型内的枚举分支；不同口径是不同模型文件。
- 允许自定义主体口径，字段只要求稳定标识形态；不得把任何单一口径写成唯一合法值，也不得据此启用融资、资本金或还款实现——本契约只做概念澄清，不引入融资/资本金计算。
- 程序不按客户/设备/核算主体名称分支。

## 完整示例

完整合法样例见[示例目录](examples/)：

- 模型（规则）：
  - [线性所得税 + 直线折旧模型](examples/financial-model-linear-income-tax.yaml)
  - [累进所得税 + 双倍余额递减折旧 + 亏损结转模型](examples/financial-model-progressive-tax-ddb.yaml)
  - [维护清单 + 指标消费模型](examples/financial-model-maintenance-metrics.yaml)
- 项目侧（参数值、绑定、评价确认、同次事实）：
  - [样例 A 的 Overrides](examples/finance-overrides-linear-income-tax.yaml)、[样例 A 的完整事实与逐年预期](examples/facts-linear-income-tax.yaml)、[样例 A 的规划配置](examples/planning-linear-income-tax.yaml)
  - [样例 B 的 Overrides](examples/finance-overrides-progressive-tax-ddb.yaml)、[样例 B 的完整事实与逐年预期](examples/facts-progressive-tax-ddb.yaml)、[样例 B 的规划配置](examples/planning-progressive-tax-ddb.yaml)
  - [样例 C 的 Overrides](examples/finance-overrides-maintenance-metrics.yaml)、[样例 C 的完整事实与逐年预期](examples/facts-maintenance-metrics.yaml)、[样例 C 的规划配置](examples/planning-maintenance-metrics.yaml)
- 逐年手算与复算预期见[财务模型手算预期](examples/financial-model-hand-calculation.md)。

三份样例组合覆盖：`K=1`（第一完整运营年为财务年 2）、`N=3`/`N=5`、折旧年限 10 年（覆盖评价期）与 3 年（早于运营期结束）、两条同类计量绑定与两个同类设备实例的选择与显式组合、亏损在盈利年抵扣耗尽、非空维护清单按发生年进入模型税额与现金流（样例 C）、机器可识别的税前/税后口径声明与 `npv`/`irr`/静态/动态回收期指标消费（样例 C 的输出 ID 刻意不使用 `before_tax`/`after_tax` 命名，口径仍由 `cashflow.tax_basis` 与 `source` 识别）。

## 非法示例

### 非法示例 1：未来引用

```yaml
relations:
  - id: illegal_future_ref
    expression: revenue[year+1] - cost[year]    # 禁止 [year+1]
```

### 非法示例 2：未声明标识符

```yaml
relations:
  - id: illegal_undeclared
    expression: unknown_var[year] * 2           # unknown_var 未在 parameters/inputs/state/relations 声明
```

### 非法示例 3：缺少必需字段

```yaml
schema: ies.financial-model
schema_version: "1.0.0"
model:
  id: ies.built-in.incomplete
  # 缺少 revision、names、description、investor_perspective、required_capabilities
```

### 非法示例 4：参数值在模型文件中

```yaml
# 错误：参数值不应出现在模型文件中（由 FinanceOverrides.parameters_values 提供）
parameters_values:
  income_tax_rate: {value: "0.25", unit: "1"}
```

### 非法示例 5：状态变量循环引用

```yaml
relations:
  - id: balance
    expression: balance[year] + 100             # 禁止引用自身当前年
```

### 非法示例 6：绑定写进模型文件

```yaml
inputs:
  - id: operating_revenue
    source: materialized_component
    component: period_energy_sale
    unit: CNY
    sign: negative
    binding: tariff_binding__pv_export_1        # 禁止：实例绑定属于项目配置的 input_bindings
```

### 非法示例 7：规划目标直接引用序列输出

```yaml
# 错误：序列输出不能直接作目标；需要目标时由模型声明 kind: npv 标量输出
objective:
  sense: maximize
  expression: after_tax_cashflow                # 禁止：序列输出
```

### 非法示例 8：`[year-1]` 引用非状态关系式

```yaml
relations:
  - id: illegal_prev_nonstate
    expression: depreciation[year-1] + 1        # 禁止：[year-1] 只允许引用 state 声明的状态变量
```

### 非法示例 9：决策相关表达式相乘

```yaml
relations:
  - id: illegal_bilinear
    expression: operating_revenue[year] * operating_cost[year]   # 禁止：两个决策相关表达式相乘，不属于 1.0.0 的能力集合
```

### 非法示例 10：指标引用未声明口径的序列

```yaml
outputs:
  - id: some_series
    kind: series
    unit: CNY
    description: 未声明 cashflow 口径的序列
  - id: some_npv
    kind: npv
    source: some_series        # 禁止：指标 source 必须是已声明 cashflow 的 series 输出
    unit: CNY
    description: 非法指标
```

## 项目包携带

项目包增加 `financial_model.yaml` 文件，与财务三件套一起构成完整财务输入（见[项目包](project-package.md)）：

- 项目包内财务文件：`finance_profile.yaml`、`finance_overrides.yaml`、`effective_finance.yaml`、`financial_model.yaml`。
- `finance_overrides.yaml` 增加 `model_ref`、`parameters_values` 与 `input_bindings`；`effective_finance.yaml` 增加 `model_ref`、`model_rules`（规范化冻结副本）、`parameters_values` 与 `input_bindings`。
- 导入时校验模型 ID 与 revision 匹配，从模型文本、Profile 与 Overrides 重新合并核对 Effective；不静默替换为新模板或最新 revision。

## 版本演进

- `ies.financial-model@1.0.0` 是初始版本。
- 破坏性变化提升 MAJOR（如改变时间轴语义、关系式语法、输出判别联合）。
- 向后兼容的新增（如新受控函数、新能力标识、新输出种类）提升 MINOR。
- 不能识别的 MAJOR 必须拒绝，不得猜测或静默降级。
- 历史任务不随新 revision 改变早已批准的模型。

## 消费者迁移清单

受影响契约与消费者的迁移边界见 `docs/development/financial-model-migration.md`（内部实施控制材料，不属于帮助中心）。
