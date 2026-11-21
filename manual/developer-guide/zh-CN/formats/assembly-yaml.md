# 装配 YAML

> 契约标识：`ies.assembly`；目标 `schema_version`：`"1.0.0"`；推荐文件名：`<assembly-id>.assembly.yaml`

装配 YAML 描述完整可校验的系统建模与规划输入：项目计算基线、设备实例与数据源绑定、真实端口连接、显式设备成本绑定与计量计费绑定、对不可变有效财务快照的精确引用以及规划配置。它不包含计算器选项（`generator`/`solver`/精度等，归 `CalculationConfig`）、Shell 命令或路径。配套版本关系见[单一当前契约](../file-formats.md#单一当前契约与项目包依赖)。

## 最小结构示例

```yaml
schema: ies.assembly
schema_version: "1.0.0"

assembly:
  id: campus_demo
  name: 园区最小算例

project_baseline:
  resolution: 1h
  scenario_mode: single
  start_year: 2025

devices:
  grid:
    definition:
      id: ies.device.grid_connection
    asset_origin: existing
    properties: {}
    predefined_interfaces: {}
  pv_1:
    definition:
      id: ies.device.pv
    asset_origin: new
    properties:
      rated_capacity_kwp:
        value: 100
        unit: kWp
    predefined_interfaces:
      solar_irradiance: {mode: constant, value: 800}
      ambient_temperature: {mode: constant, value: 25}
  load:
    definition:
      id: ies.device.electric_load
    asset_origin: existing
    properties: {}
    predefined_interfaces:
      electricity_demand:
        mode: data_repeat
        path: data/campus_load.data.csv

connections:
  grid_to_load:
    from: grid.electricity_import
    to: load.electricity_in
  pv_to_grid:
    from: pv_1.electric_out
    to: grid.electricity_export

finance:
  ref:
    kind: object                     # 或包内 relative_file
    object_id: "4444444444444444444444444444444444444444444444444444444444444444"
    media_type: application/yaml
  profile_id: cn-north-demo

finance_binding:
  instances:
    pv_1:
      type: costed
      finance_type: pv_system
      drivers:
        capacity_kw:
          property: rated_capacity_kwp
        generated_kwh:
          interface: electric_out
          aggregation: integrate_positive
    grid:
      type: none                     # 纯计量点，无设备成本；能源费用由 tariff_bindings 表达
    load:
      type: none

tariff_bindings:
  import_meter:                      # binding_id：lower_snake_case、不含 __、不与 instance_id 同名
    price: grid_import
    instance: grid
    interface: electricity_import
    aggregation: integrate_positive
  export_meter:
    price: pv_export
    instance: grid
    interface: electricity_export
    aggregation: integrate_positive

demand_bindings: {}                  # 无需量费时显式为空；独立于能源电量计费

planning:
  objective:
    sense: minimize
    expression: "1 year * pv_1__annual_fixed_om + pv_1__period_variable_om + import_meter__period_energy_purchase + export_meter__period_energy_sale"
  variables:
    pv_capacity:
      device_ref: pv_1
      parameter: rated_capacity_kwp
      lower_bound: "0"
      upper_bound: "1000"
      unit: kWp
  constraints:
    annual_purchase_cap:
      type: annual_purchase_cost_limit
      expression: "import_meter__period_energy_purchase <= 900000 CNY"
    export_step_cap:
      type: step_relation
      references:
        boundary: {instance: grid, interface: electricity_export}
        supply: {instance: pv_1, interface: electric_out}
      expression: "boundary[t] <= supply[t]"
      enabled: true

extensions: {}
```

## 顶层字段职责

| 字段 | 作用 |
|---|---|
| `assembly` | 稳定标识 `id` 与人类可读名称 `name` |
| `project_baseline` | 项目创建时固定的 `resolution`、`scenario_mode`（`single`）与公历 `start_year` |
| `devices` | 设备实例、引用设备类型、资产来源（`existing`/`new`）、属性覆盖与 `predefined_interfaces` 来源绑定 |
| `connections` | 真实可连接 `<device>.<interface>` 之间的有向连接 |
| `system_equations` | 可选系统方程块映射，完整字段见[网络边界与系统方程](network-relations.md) |
| `finance` | 指向装配前由 `FinanceProfile` + `FinanceOverrides` 合并签发的 `EffectiveFinanceConfig` 精确引用 |
| `finance_binding` | 每个实例的设备成本归属判别联合（`costed` / `none`）与 `driver` 映射 |
| `tariff_bindings` | 能源价格计量绑定：`binding_id -> {price, instance, interface, aggregation}` |
| `demand_bindings` | 自然月需量计量绑定映射（无需量费时填 `{}`） |
| `planning` | 优化目标、规划变量、可选 `construction_units` 与 `constraints` |
| `outputs.system_series` | 可选系统辅助输出序列选择列表，见[网络边界与系统方程](network-relations.md#保存冻结与公开输出) |
| `extensions` | 命名空间化扩展区 |

## 设备实例与预定义来源

- **实例规则**：`asset_origin` 必填 `existing` 或 `new`；`properties` 仅可覆盖设备模型已声明的属性并保留显式单位；`blind` 接口不可连线或绑定来源。
- **预定义接口绑定**：设备的每个 `predefined` 槽必须在 `predefined_interfaces` 中恰好声明以下一种判别联合来源（`path` 为项目内相对 CSV 路径，`data_full` 的全运营期点数在任务提交边界核对）：
  - `{mode: constant, value}`
  - `{mode: data_repeat, path}`
  - `{mode: data_full, path}`
  - `{mode: data_predict, path, target_type}`

## 连接

`connections` 中的每项 `{from, to}` 连接两个真实端口 `<device-instance>.<interface-id>`：

- 按载体与连接的传递闭包形成理想公共母线，每条母线逐 `step` 满足流量守恒（`out` 供给为正、`in` 吸收为负、`bidirectional` 按有符号净供给计）。同一设备的不同端口不会自动短接。
- 连接双方必须载体相同、量纲兼容，禁止自环、重复边以及引用 `predefined` 或 `blind` 端口。
- 未参与任何连接的可连接端口作为独立关闭边界处理（流量约束为零），完整边界与分配规则见[端口级网络语义](../modules/assembly.md#端口级网络语义)与[完整网络边界](network-relations.md#完整网络边界)。

## 项目计算基线与财务引用

- **`project_baseline`**：`resolution` 须能整除一天；`start_year` 为公历运行起始年（闰年按公历推导，不设 `leap_year`），与财务口径基准年 `base_year` 相互独立；步长变量 `step_duration` 由 `resolution` 唯一推导。
- **`finance` 引用**：`assembly.finance` 仅保存对 `EffectiveFinanceConfig` 的对象存储或相对路径引用及来源 `profile_id`，不在装配内联完整财务配置。

## 规划配置、财务标识符与约束

`planning.constraints.<id>` 支持标量财务/规划约束、`step_relation` 与 `amount_relation`，`enabled` 省略时为 `true`。

### 规划表达式中的财务分量标识符

全周期税后最优规划使用保留目标 `{sense: maximize, expression: system.after_tax_npv}`（仅用于完整目标表达式，详情见[规划目标与同源评价](../modules/finance.md#规划目标与同源评价)）。

普通财务标量表达式中的分量采用以下唯一标识符映射（`instance_id`、`binding_id` 与 `billing_id` 共享同一 `lower_snake_case` 且不含 `__` 的局部命名空间，彼此不可同名冲突）：

```text
设备成本分量：<instance_id>__<upfront_capex | annual_fixed_om | period_variable_om>
能源计费分量：<binding_id>__<period_energy_purchase | period_energy_sale>
需量计费分量：<billing_id>__period_demand_charge
```

- **符号与后缀匹配**：能源计费分量的后缀必须与所引用 `energy_prices` 的 `direction`（`purchase|sale`）严格一致；其中 `purchase` 分量记账为 `+raw_charge`，`sale` 分量记账为 `-raw_charge`（`raw_charge = Σ price[t] * e[t]`，正售电价下自身为负），因此目标表达式统一采用加法汇总（如 `+ export_meter__period_energy_sale`）。
- **增量与沉没排除**：`existing` 实例不计入也不允许引用 `upfront_capex` 与 `annual_fixed_om` 分量。
- **量纲与单位字面量**：`annual_fixed_om` 量纲为 `currency/year`，与窗口合计分量（`currency`）相加前必须显式乘以时间跨度字面量（如 `1 year`）；有维度常量须带单位（如 `900000 CNY`），无量纲权重可写裸数字（如 `0.5`）。一次投资与运行费用的对比换算须由规划表达式显式定义，财务 Profile 不做隐式年化。

## 逐步技术约束

跨实例的端口逐步技术关系在 `planning.constraints.<id>` 中声明 `type: step_relation`：

| 字段 | 语义 |
|---|---|
| `type` | 固定为 `step_relation` |
| `references` | 必填非空映射，局部别名 → `{instance, interface}`（限非标量可连接端口） |
| `expression` | 必填仿射关系式（`==` / `<=` / `>=`），端口量以 `别名[t]` 引用，规划变量无下标，允许带单位常量与 `step_duration` |
| `enabled` | 布尔值，省略为 `true`；`false` 保留定义但不施加约束 |

启用后在完整运营期每一步 `t` 施加约束（不包含跨步状态索引，也不表达来源隔离；需要局部辅助变量或来源账户时应使用 `system_equations`）。要求生成器具备 `technical.step_constraints` 能力。

## 金额引用约束

`planning.constraints.<id>` 可声明无步索引的标量金额关系 `type: amount_relation`，将明确期间的命名金额或冻结基准绑定到本条局部别名：

| 字段 | 语义 |
|---|---|
| `type` | 固定为 `amount_relation` |
| `references` | 局部别名 → `{kind: amount, id, period}` 或 `{kind: baseline, id, period}` |
| `expression` | 消费局部别名与带单位常数的仿射 `==` / `<=` / `>=` 表达式 |
| `enabled` | 布尔值，省略为 `true` |

`period` 仅允许 `scalar`、固定 `financial_year`/`tax_month`、`year_months` 与 `range`（禁止 `current`），比较双方必须同为本币金额或同量纲费率。示例：

```yaml
planning:
  constraints:
    spend_cap:
      type: amount_relation
      references:
        spend: {kind: amount, id: additional_spend, period: {kind: financial_year, year: 2}}
        budget: {kind: baseline, id: budget_limit, period: {kind: financial_year, year: 2}}
      expression: "spend <= budget"
      enabled: true
```

启用该约束要求生成器具备 `finance.amount_constraints` 及对应来源能力。

## 建设激活目标语义

新建设备的建设选择与多规模联动在 `planning.construction_units` 中显式声明（省略等价于 `{}`，不从正固定费或零容量下界隐式推导选建开关）：

| 字段 | 规则 |
|---|---|
| `construction_units.<unit_id>` | 建设单元稳定局部 ID |
| `device_ref` | 必填，指向一个 `asset_origin: new` 的装配实例（单实例最多归属一个建设单元） |
| `mode` | 必填：`mandatory`（必建，状态恒为 `1`）或 `optional`（可选建，二元决策 `z ∈ {0, 1}`） |
| `scales.<var_id>.active_lower_bound` | 非负十进制字符串（省略 `"0"`），仅激活时生效的最小规模 |
| `scales.<var_id>.active_upper_bound` | 非负十进制字符串或 `null`（省略为 `null`） |

`optional` 单元未激活时（`z = 0`）关联规划变量归零，激活时（`z = 1`）满足活动区间并始终与无条件 `lower_bound`/`upper_bound` 及模型有效域取交集；需要活动上界时由[统一上下界](../modules/generators.md#统一上下界)推导。示例：

```yaml
planning:
  construction_units:
    storage_build:
      device_ref: store
      mode: optional
      scales:
        store_energy: {active_lower_bound: "0", active_upper_bound: null}
        store_power: {active_lower_bound: "0", active_upper_bound: null}
```

### 费用发生条件

`type: costed` 实例可通过 `cost_occurrence` 指定哪些费用随本实例的建设状态触发（未声明或未覆盖叶子默认为无条件 `{kind: always}`）：

| 配置项 | 写法与语义 |
|---|---|
| 路径键 | `upfront_capex`、`annual_fixed_om`、`period_variable_om` 整项或其 `.fixed` / `.linear.<driver>` 子项（命名明细路径见[命名成本明细](finance-relations.md#命名成本明细)）；整项与子项禁止重叠 |
| 条件值 | `{kind: always}` 或 `{kind: construction_unit, unit_ref: <unit_id>}`（只能引用本实例的建设单元） |

## 设备成本绑定 `finance_binding`

`finance_binding.instances` 必须显式覆盖全部设备实例（`type: costed` 绑定 `finance_type` 与 `drivers`；`type: none` 表示无设备成本，禁止携带 `finance_type` 或 `drivers`）：

- **聚合语义**：`property` 仅对应标量（可省略 `aggregation`）；时序 `interface` 必须声明 `integrate_positive`（按 `step_duration` 对非负功率积分）或 `sum_positive`（直接取区间累计能量）；拒绝负值序列，不接受模糊净值或绝对值。
- **Driver 齐全性**：`new` 实例须映射所用类别在 `upfront_capex`、`annual_fixed_om` 与 `period_variable_om` 中声明的全部线性 `driver`；`existing` 实例仅须映射 `period_variable_om` 所需 `driver`。成本系数乘规范 `driver` 单位必须匹配 `currency` 或 `currency/year`。

## 能源计费绑定 `tariff_bindings`

`tariff_bindings` 将有效财务快照的 `energy_prices` 条目显式绑定到计费点接口：

```yaml
tariff_bindings:
  import_meter:
    price: grid_import
    instance: grid            # 可为 type: costed 或 type: none
    interface: electricity_import
    aggregation: integrate_positive
```

- 计费端口必须是单向时序 `in` 或 `out` 接口（`bidirectional` 与标量属性不可直接计费），载体须与价格条目一致。
- 端口的物理流向（`in`/`out`）与价格条目的会计方向（`purchase|sale`）各自独立声明，不互相推断。

## 自然月需量计量绑定 `demand_bindings`

`demand_bindings` 必填（无需量费时显式为 `{}`），按 `billing_id -> {charge, instance, interface}` 声明：

- `charge` 引用有效快照中的 `demand_charges` 费项；`instance.interface` 必须是电力载体的非负 `out` 时序功率接口（不可为电量接口或双向口）。
- 同一 `charge` 与同一实例端口不可重复绑定；每个绑定公开货币量纲的全周期分量 `<billing_id>__period_demand_charge`，按各运营自然月实际峰值乘以费率逐月计取并纳入同次求解与财务评价。

## 校验流程与 `ValidatedAssemblyArtifact`

1. **四阶段校验**：按 `结构校验 → 模型与数据校验 → 图与系统校验 → 规划与财务完整性校验` 顺序执行，任一阻断错误均不签发产物。
2. **不可变输出二件套**：规范化装配文本（字段稳定排序、剥离非语义空白、固定资源引用，规范算法标识 `ies.assembly.canonical@1.0.0`）与零阻断校验回执共同构成 `ValidatedAssemblyArtifact`，作为下游计算生成器的唯一输入凭证。
