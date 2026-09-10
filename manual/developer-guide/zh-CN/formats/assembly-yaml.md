# 装配 YAML

> 契约标识：`ies.assembly`；目标 schema：`2.0.0`；推荐文件名：`<assembly-id>.assembly.yaml`。
> 文档状态：生效目标契约；本页只定义目标文件语义，不声明实现进度。

装配 YAML 是系统模型与规划意图的完整、可审查文本：它固定项目计算基线，实例化精确设备内容，绑定已校验的预定义来源声明、规范数据和不可变输入引用，连接可连接的真实 interfaces，区分存量/新增设备，为每个设备实例显式声明**设备成本**财务绑定（判别联合 `costed`/`none`），并用独立的 `tariff_bindings` 把能源价格条目（`price_id`）绑定到计费点实例/接口与聚合（计费点可以是 `type: none` 的纯计量实例），用精确引用固定装配前合并签发的不可变有效财务快照，并承载规划配置。它不包含物化后的未来序列、generator、solver、预测算法、计算精度或求解选项；这些计算事项在规范装配产物转换为计算包时固定。财务三件套的 schema、字段与摘要规则见[财务 YAML 契约](finance-yaml.md)。

## 最小结构示例

```yaml
schema: ies.assembly
schema_version: "2.0.0"

assembly:
  id: campus_demo
  name: 园区最小算例

project_baseline:
  resolution: 1h
  leap_year: false
  scenario_mode: single

resources:
  datasets:
    campus_load:
      source:
        kind: relative_file
        path: data/campus_load.data.csv

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
        data_ref: campus_load

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
  profile_id: cn-north-demo  # 规范内容摘要

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
      type: none                     # 无设备成本；只作能源计费计量点（费用由 tariff_bindings 表达）
    load:
      type: none

tariff_bindings:
  import_meter:                      # binding_id：lower_snake_case、不含 __、不得与 instance_id 相同
    price: grid_import               # 引用 energy_prices 的 price_id（其 carrier/direction 由 Profile 显式定义）
    instance: grid
    interface: electricity_import
    aggregation: integrate_positive
  export_meter:
    price: pv_export
    instance: grid
    interface: electricity_export
    aggregation: integrate_positive

planning:
  objective:
    sense: minimize
    expression: "1 year * pv_1__annual_fixed_om + pv_1__period_variable_om + import_meter__period_energy_purchase + export_meter__period_energy_sale"
  variables: {}
  constraints:
    capex_budget:
      expression: "pv_1__upfront_capex <= 900000 CNY"

extensions: {}
```

被引用的有效快照需含本装配用到的 `finance_type`（如 `pv_system`）与 `energy_prices` 的 `price_id`（如 `grid_import`/`pv_export`），完整字段定义见[财务 YAML 契约](finance-yaml.md)。实例 `instance_id`（如 `pv_1`/`grid`）、计量绑定 `binding_id`（如 `import_meter`）、财务 `finance_type`（如 `pv_system`）与技术模型 `device.id`（如 `ies.device.pv`）严格分离：`grid` 无设备成本、不绑定 `finance_type`（不再虚构 `grid_connection` 等零成本连接类别），其计量与计费完全由 `tariff_bindings` 表达。

规划目标示例把成本项显式列出：`pv_1__annual_fixed_om`（currency/年）、`pv_1__period_variable_om`、`import_meter__period_energy_purchase`、`export_meter__period_energy_sale`（currency，运行/计费窗口合计）。这些标识符是受限表达式可解析的普通 `Name`，其编码与映射规则见下文「规划表达式中的财务分量标识符」：设备成本分量 = `<instance_id>__<分量>`，能源计费分量 = `<tariff_binding_id>__period_energy_purchase|sale`，不使用点分路径。`annual_fixed_om` 与 `period_*` 量纲不同（currency/年 vs currency），不能直接相加：示例中计算窗口恰好是项目基线覆盖的完整一年，表达式通过**显式时间跨度字面量 `1 year`** 把按年值换算为完整年度窗口金额（currency）后再与 `period_*` 相加；计算窗口不是完整一年时，不得默认相加或做其他隐式缩放（规则见[财务 YAML 契约](finance-yaml.md)「时间口径与分量」）。会计符号由被引用价格的显式 `direction` 统一决定（定义见[财务 YAML 契约](finance-yaml.md)「计价规则」）：每个计量绑定先算 `raw_charge = Σ price×energy`，`purchase`（如 `grid_import`）分量为 +raw_charge、`sale`（如 `pv_export`）分量为 −raw_charge。分量是**已带符号的记账贡献**（正售电价时 sale 分量自身为负，收益自动抵减成本），因此目标表达式对所有分量**统一使用加法**——示例写 `+ export_meter__period_energy_sale`，不再书写额外负号，也不把“sale 以负号计入”写为规则。该示例是“预算上限约束下的年度运行成本最小化”这一**具体示例，不是通用经济目标**：一次性 `upfront_capex` 不进该目标，改由预算上限约束显式处理（示例上限 `900000 CNY` 为规划人员输入的带单位常数，单位必须与 `EffectiveFinanceConfig.currency` 一致，不是系统财务规则）；具体如何把一次性投资转换为可与运行期成本比较的规划分量，属于 `PlanningConfig` 的明确、版本化规则，需在规划配置契约中定义，本财务 Profile 不隐式定义或默认选择这种转换（不隐含资本回收系数、利率或年限等值）。表达式与约束的具体语法由规划配置契约定义，本格式页只定义财务分量标识符的映射与量纲，不定义表达式语言本身；分量标识符引用设备成本绑定（`pv_1`）与能源计费绑定（`import_meter`/`export_meter`），不依赖任何隐式“系统总成本”聚合。

## 顶层职责

| 字段 | 作用 |
|---|---|
| `assembly` | 本装配的稳定 ID 和人类可读名称 |
| `project_baseline` | 项目创建时固定的时间分辨率、闰年口径和场景模式 |
| `resources` | 数据等外部资源及其可验证来源 |
| `devices` | 设备实例、精确设备内容、存量/新增身份、允许的 property 覆盖和预定义接口绑定 |
| `connections` | 可连接的真实 `<device>.<interface>` 之间的有向连接 |
| `finance` | 对装配前由 `FinanceProfile` + `FinanceOverrides` 合并生成、完整校验的不可变 `EffectiveFinanceConfig` 的引用：`ref` 携带对象身份，`profile_id` 携带来源标识；规划与财务计算共同消费同一引用；装配内不内联另一份完整财务配置 |
| `finance_binding` | 每个设备实例的**设备成本**财务判别联合：`type: costed`（含 `finance_type` 与 `driver → property/interface` 映射）或 `type: none`（该实例无设备成本贡献，可作纯计量点；能源计费不在此表达）。缺口、未知 `finance_type`、非法映射、按增量语义缺失的 driver 与单位量纲不兼容在装配校验中阻断 |
| `tariff_bindings` | 能源价格计费绑定：`binding_id → {price, instance, interface, aggregation}`，`price` 引用有效快照 `energy_prices` 的 `price_id`（`carrier`/`direction` 由 Profile 条目显式声明）；独立于设备成本绑定，计费点实例可以是 `type: none`；逐点计价与会计方向规则见[财务 YAML 契约](finance-yaml.md) |
| `planning` | 目标函数、规划变量、上下界和规划/系统约束；目标逐项引用设备成本或能源计费分量（见上方规划示例），不依赖隐式总成本 |
| `extensions` | 命名空间化扩展 |

`planning.constraints` 为命名映射；表达式只能使用受限声明式语法（语法、运算符与变量声明由规划配置契约定义；解析器只接受普通标识符 `Name`）。预算型约束示例见上方最小结构示例的 `capex_budget`：一次性 `upfront_capex` 经预算上限显式处理，不混入年度运行目标加和；把一次性投资转换为可与运行期成本比较的规划分量属于 `PlanningConfig` 的版本化规则，本财务 Profile 不隐式定义或默认选择（见[财务 YAML 契约](finance-yaml.md)「时间口径与分量」）。数值常量规则：**有维度的常量必须带单位字面量**（金额如 `900000 CNY`、时间跨度如 `1 year`），单位必须与 `EffectiveFinanceConfig.currency` 一致，量纲不一致阻断；**纯无量纲系数可写裸数字**（如 `0.5 * pv_1__period_variable_om` 中的 `0.5`），不需要也不允许编造单位。财务分量引用的合法标识符与单位字面量规则见下文「规划表达式中的财务分量标识符」。

## 设备内容固定与实例

每个设备实例用 `definition.id` 固定具体设备内容（按字头校验，不做内容摘要）。`schema_version` 版本化统一设备格式，不是某台设备的语义版本；装配不得出现 `device_version`、`model@version`、`latest` 或设备私有命令版本。

实例规则：

- `asset_origin` 必须是 `existing` 或 `new`；不得从设备类型、创建时间或是否填写成本推断；
- `properties` 只能覆盖设备定义已声明且允许实例化的非时变技术常量，并保留明确单位；不能新增字段，也不能放价格、成本或计算精度；
- `predefined_interfaces` 只能绑定设备中 `type: predefined` 的 interface；每个 predefined 槽都必须恰好有一个显式绑定，缺失或绑定不存在/非 predefined 接口均阻断；
- 来源判别联合严格为 `constant: {mode, value}`、`data_repeat: {mode, data_ref}`、`data_predict: {mode, data_ref, target_type}`；缺少必填字段、混入其他模式字段和所有额外字段均拒绝，没有默认 mode 或默认值；
- `constant/data_repeat/data_predict` 只固定来源声明及所需的不可变输入引用，不得在装配前替换为物化后的未来序列；规范化文本必须完整保留 `value/data_ref/target_type`；
- `in/out/bidirectional` 通过 connections 取得外部交互；`blind` 既不能连接，也不能绑定预定义数据；
- 每项覆盖、绑定和身份均进入规范装配摘要与校验回执；
- 资产身份与增量成本强关联：`existing` 的沉没历史建设成本不计入增量目标，`new` 按新增驱动量计入固定建设成本与线性分量；但所有设备仍计算决策相关可变 O&M 与能源购售（能源购售只经 `tariff_bindings` 计量点产生，见[财务 YAML 契约](finance-yaml.md)）。

## 连接

连接端点使用 `<device-instance>.<interface-id>`。装配校验必须确认：

- 两个实例和 interface 存在；
- `out → in`、与语义相容的 `bidirectional` 连接成立；
- carrier 相同、单位量纲兼容、取值区间不冲突；
- `predefined` 和 `blind` 从不出现在 connections；
- 无禁止的自环、重复边和未满足的必需连接；
- 损耗、延迟和转换由明确设备方程或系统约束表达；
- 网络在所选计算模式下具备完整供需和平衡语义。

校验失败不能删边后继续，也不能创建默认 interface、把缺失 type 猜成双向或改变 `blind` 的含义。

## 项目计算基线

`project_baseline` 在创建项目时一次性固定，当前只包含：

- `resolution`：全项目统一计算分辨率，必须能确定性切分一天；
- `leap_year`：是否按 366 天生成全周期序列；
- `scenario_mode`：当前固定为 `single`。

基线不保存时区、开始/结束时间、典型日/周/年或计算截取区间。计算序列统一使用从 `0` 开始的连续 `step`，点数由 `resolution` 和 `leap_year` 唯一推导。公开计算变量 `step_duration` 由 `resolution` 唯一推导并携带明确时间单位，是状态方程和能量积分使用的唯一公开步长变量。已有项目基线变更或多场景必须通过新的公开契约定义，不能作为本格式的隐式例外。

## 预定义序列与资源

人工文件可以绑定包内相对文件：

```yaml
source:
  kind: relative_file
  path: data/campus_load.data.csv
```

也可以绑定内容寻址对象：

```yaml
source:
  kind: object
  object_id: "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
  media_type: text/csv
```

`relative_file` 只用于外部作者包的导入入口，不能逃逸包目录。入口完成文件解析并登记内容对象后，规范装配只保留已登记的内容寻址引用；进入可信内部流程后不再因文件交接而重算校验。网络 URL、宿主机绝对路径、临时上传路径和存储 provider 私有路径不得进入可执行快照。

CSV 必须固定相同的设备 ID 与内容摘要，并且列 ID、单位、`source_mode`、分辨率、输入 `step` 和有效区间与目标 predefined interface 一致。装配绑定经校验和规范化的重复基线、训练目标、历史输入与未来已知协变量的内容寻址引用。装配必须确认每个来源与项目基线分辨率一致、文件内 `step` 从零开始连续，并符合对应来源模式的覆盖要求；不同原始来源在物化前不要求点数相同或 `step` 一一对应。`data_repeat` 的完整来源序列整体作为重复基线，可为完整日、周或年，不另设周期字段。任一来源自身不符合时必须阻断，不重采样、插值、聚合、融合或补齐。计算阶段按项目基线物化后，所有计算序列统一点数和连续 `step`，并在产物与回执中固定。

## 规划配置与有效财务快照引用

设备 YAML 始终保持纯技术。有效财务快照是装配前由地区 `FinanceProfile`（已注册、可复用的地区财务基准，不得以硬编码内置充当全局默认）与项目 `FinanceOverrides`（引用 `profile {id}`，对既有 `finance_type` 分量叶子做 `{value, unit}` 原子替换、对能源价格条目做定价定义整项替换；税目只属于 Profile，Overrides 不可新增/覆盖/删除）在装配前确定性合并并完整校验后签发，并以引用形式进入装配校验回执。

`assembly.finance` 采用**精确引用**作为规范形态，不在装配内内联完整财务配置：

```yaml
finance:
  ref:
    kind: object            # 或包内 relative_file（作者包内不逃逸）
    object_id: "4444444444444444444444444444444444444444444444444444444444444444"
    media_type: application/yaml
  profile_id: cn-north-demo
```

引用规则：

- `object_id` 是在对象存储写入或外部导入边界确定的对象身份；内部装配按该身份取得已登记对象；
- 外部导入时可在入口完成验证，并从来源 Profile 与 Overrides 重新合并验证财务语义；进入可信流程后不重复校验，也不静默改用其他来源；
- 引用 `time_series` 价格的条目（在 Profile/Overrides 中声明）携带 `resolution`/`leap_year`/`point_count`/`unit` 等不可变序列元数据，装配时与项目基线核对：序列引用对象必须已校验为完整年度序列且元数据一致；不一致阻断，不重采样、不插值（Profile 不固定唯一项目分辨率，校验以被引用序列元数据为准）；时变价格按逐点计价（`Σ price[t]×e[t]`），聚合与计价衔接见[财务 YAML 契约](finance-yaml.md)「聚合与计价衔接」；
- `planning` 只保存目标函数、目标权重、规划变量、上下界和规划/系统约束。目标函数用普通标识符逐项引用财务分量（映射规则见下文「规划表达式中的财务分量标识符」；示例见上方最小结构：`1 year * pv_1__annual_fixed_om + pv_1__period_variable_om + import_meter__period_energy_purchase + export_meter__period_energy_sale`，其中 `1 year` 为显式时间跨度字面量，把 currency/年 的按年值换算为完整年度窗口金额（currency）后再相加；能源计费分量**已带符号**——`purchase` 分量 = +raw_charge、`sale` 分量 = −raw_charge（`raw_charge = Σ price[t]×e[t]`，正售电价时 sale 分量自身为负），所以目标表达式对所有分量统一使用加法；负价自然反转该方向效果；窗口不是完整一年时不得默认相加）；该示例是“预算上限约束下的运行成本最小化”的具体示例，不是通用经济目标——把一次性 `upfront_capex` 转换为可与运行期成本比较的显式规划分量，属于 `PlanningConfig` 的版本化规则，须在该契约中定义后再使用；有效快照**不静默相加成默认总成本**，不提供隐式“系统总成本”标识符，Profile 不隐式年化或默认选择转换规则；规划配置显式选择或组合哪些分量；
- 项目覆盖是 authoring 输入，`EffectiveFinanceConfig` 是装配与计算唯一消费的不可变快照：运行期不继承 Profile、不读取“最新地区价格”、不静默默认值。本契约成本分量不包含折旧、融资、还款与 IRR 等后评价输入。

### 规划表达式中的财务分量标识符

受限表达式解析器只接受普通标识符（`Name`）：财务分量不以点分路径或属性访问形式出现。财务分量在规划表达式中的合法标识符统一为：

```text
设备成本分量：<instance_id>__<分量>              # 分量 ∈ {upfront_capex, annual_fixed_om, period_variable_om}
能源计费分量：<binding_id>__period_energy_purchase | <binding_id>__period_energy_sale
```

例如 `pv_1__annual_fixed_om`、`import_meter__period_energy_purchase`、`export_meter__period_energy_sale`。映射规则（装配规划校验强制）：

- 两个前缀集合共用同一表达式命名空间：`instance_id` 与 `binding_id` 都必须是 `lower_snake_case`、不含连续双下划线（`__`）、文件内唯一，且 **`binding_id` 不得与任何 `instance_id` 相同**（同名会把设备成本与能源计费两类分量混在同一前缀下）；因此 `<前缀>__<后缀>` 从左向右按首个 `__` 切分即可唯一解析，不与其它标识符碰撞；
- 后缀决定分量族：`upfront_capex`/`annual_fixed_om`/`period_variable_om` 只与实例前缀（设备成本）合法；`period_energy_purchase`/`period_energy_sale` 只与绑定前缀（能源计费）合法，且必须与被引用价格的显式 `direction` 匹配——`direction: purchase` 的 price 产生 `<binding_id>__period_energy_purchase`，`direction: sale` 的 price 产生 `<binding_id>__period_energy_sale`；方向不靠 binding_id 或 price_id 的名称推断，后缀与方向不匹配即阻断；
- 标识符到 `(前缀, 分量)` 的映射唯一可逆。装配校验确认该前缀在绑定下**实际产生**该分量：设备成本分量要求实例为 `type: costed`、其 `finance_type` 声明该分量且相关 driver 按增量语义齐全；能源计费分量要求该 `binding_id` 在 `tariff_bindings` 中绑定存在的 `price_id`（`carrier` 与接口相容、方向匹配）。`existing` 实例不得引用被排除的沉没分量（`upfront_capex`、`annual_fixed_om`）；不满足即阻断，防止把不存在的量写进目标；
- 规划配置不得把上述保留集合中的标识符声明为其它含义（与变量或技术量同名冲突即拒绝）；其余表达式标识符（模型技术量、规划变量等）由规划配置契约定义，本小节只定义财务分量引用的映射。

量纲与单位字面量：

- 财务分量标识符自带量纲：`upfront_capex`、`period_variable_om`、`period_energy_purchase`、`period_energy_sale` 为 currency；`annual_fixed_om` 为 currency/年。能源计费分量是**有符号记账值**：`purchase` 分量 = +raw_charge（正价时为正值）、`sale` 分量 = −raw_charge（正售电价时为负、负售电价时为正）；负号是记账符号而非物理量取负，能量序列本身非负；量纲恒为 currency、与符号无关；
- 表达式中的**有维度常量**以带单位字面量书写（`数值 + 单位标识符`，如 `900000 CNY`、`1 year`），单位标识符来自公共单位词汇；常量单位必须与 `EffectiveFinanceConfig.currency` 一致（示例币种 CNY），量纲不一致在装配规划校验阻断；
- **纯无量纲系数**（如 `0.5`、整数权重）可以裸数字书写，不需要单位字面量；含维度加减与比较的表达式由规划校验做维度检查；
- `annual_fixed_om` 与窗口合计分量相加前，必须显式乘以时间跨度字面量（示例 `1 year`，窗口为项目基线完整年度）；窗口不是完整一年时不得默认相加；
- 本小节只定义财务分量标识符的编码、映射与量纲规则，不定义规划表达式语言本身（语法与运算符由规划配置契约提供）。

## 设备成本绑定 `finance_binding`

`finance_binding.instances` 为**每个设备实例**都给出显式判别联合：`type: costed`（绑定财务类别并把财务 `driver` 映射到本实例设备的技术 `property`/`interface`），或 `type: none`（该实例无设备成本贡献）。`finance_binding` 只表达设备成本；能源计费由独立的 `tariff_bindings` 表达，`type: none` 实例（如计量点 `grid`）可以承担能源计费。不再根据经济相关性猜测哪些实例需要绑定，也不允许实例缺席。

```yaml
finance_binding:
  instances:
    pv_1:
      type: costed
      finance_type: pv_system
      drivers:
        capacity_kw:
          property: rated_capacity_kwp             # property 只对应 scalar（映射可省略 aggregation）
        generated_kwh:
          interface: electric_out                  # 时序 interface 必须写 aggregation
          aggregation: integrate_positive
    grid:
      type: none                                   # 无设备成本；作为能源计费计量点（见 tariff_bindings）
    load:
      type: none                                   # 无设备成本与计费，显式声明
```

规则：

- `type: costed` 的 `finance_type` 必须存在于被引用有效快照的 `finance_types`；`type: none` 不允许写 `finance_type` 或 `drivers`；本契约不虚构零成本设备类别（如 `grid_connection`）来让计费点“costed”——计量点无设备成本时直接声明 `type: none`，其费用由 `tariff_bindings` 表达；
- `drivers` 中出现的 `driver` 必须为该 `finance_type` 已声明的 `driver`，映射必须指向本实例设备真实存在的 `property`/`interface`，不通过名称/载体/`device.id` 猜测；
- 聚合语义：`property` 只对应 `scalar`，映射 `property` 时可省略 `aggregation`（语义固定为 `scalar`）；时序 `interface` 必须配 `integrate_positive`（非负功率/流率按 `step_duration` 积分）或 `sum_positive`（区间累计接口直接作为能量序列）；本契约不接受模糊双向净值/绝对值，负值序列在聚合前阻断；
- **driver 齐全性校验（防止漏成本）**：装配校验不能只检查已提供的 driver 合法，还必须按实例资产身份校验其实际计入的分量所需 driver 全部存在——`new` 实例要求该 `finance_type` 计入的每个分量（`upfront_capex.linear`、`annual_fixed_om.linear`、`period_variable_om.linear` 中已声明的 driver）都有映射，缺项阻断；`existing` 实例只要求仍计入的 `period_variable_om.linear` driver 齐全，沉没的 `upfront_capex` 与 `annual_fixed_om` 不要求提供 driver（不为被排除的沉没成本强制映射无用 driver）；
- 量纲校验：成本系数单位与规范化 driver 单位相乘后必须得到 `currency`（upfront/period）或 `currency/year`（annual_fixed_om），否则阻断；成本金额非负（允许零成本类别）；能源价格为有限 Decimal、允许正/零/负（符号与载体规则在价格条目与 `tariff_bindings` 校验，见[财务 YAML 契约](finance-yaml.md)）；
- 增量语义：`new` 计入 `upfront_capex`（固定 + 线性）与新增固定 O&M 相关的 `annual_fixed_om`；`existing` 不计沉没历史建设成本与决策无关的固定 O&M；`costed` 设备仍计算决策相关可变 O&M（`period_variable_om`），能源购售只经 `tariff_bindings` 计量点产生，与实例是 `costed` 还是 `none` 无关。

## 能源计费绑定 `tariff_bindings`

`tariff_bindings` 是 `binding_id → {price, instance, interface, aggregation}` 的显式映射：把有效快照 `energy_prices` 的 `price_id` 绑定到发生计费的实例、接口与聚合。它独立于设备成本绑定 `finance_binding`；同一个价格条目可被多个绑定复用（如多个子计量点共用同一购电价），每个绑定独立产生可追溯的计费分量：

```yaml
tariff_bindings:
  import_meter:              # binding_id：lower_snake_case、不含 __、文件内唯一、不得与 instance_id 相同
    price: grid_import       # 引用有效快照 energy_prices 的 price_id
    instance: grid           # 计费点实例：type: costed 或 type: none 均可（none = 无设备成本的纯计量点）
    interface: electricity_import
    aggregation: integrate_positive
  export_meter:
    price: pv_export
    instance: grid
    interface: electricity_export
    aggregation: integrate_positive
```

规则：

- `price` 引用的 `price_id` 必须存在于有效快照 `energy_prices`；价格条目的会计方向与载体由 Profile 显式声明（`direction: purchase|sale`、`carrier`），**不靠 binding_id、price_id 或键名后缀推断**，装配也不改写它们；
- 计费点必须明确落在实例与接口上：不从连接图推断计费点、不自动把“未绑定价格”绑定到唯一出入口、不接受双向净值或绝对值口径；计量端点必须是方向明确的时序 `in`/`out` 接口（`bidirectional` 接口不直接作为计费端点），`scalar` 不用于能源计价；
- 载体与方向相容性校验：价格 `carrier` 必须与绑定接口的载体相容（同一公共载体词汇元素；载体取值与规则见[设备模型 YAML](device-model-yaml.md)「interfaces」，本格式页不复制清单）；`direction` 必须与接口流向语义一致——`purchase` 计量的能量由公共设施/外部经 `grid.electricity_import` 流入本站，`sale` 计量的能量由本站经 `grid.electricity_export` 流向公共设施/外部；流向不符或需猜测的绑定阻断；
- 实例与绑定状态互不约束：被 `tariff_bindings` 引用的实例可以声明 `type: none`（纯计量点，如示例 `grid`），也可以 `type: costed`；`type: none` 只表示无设备成本，不禁止承担能源计费；装配校验确认计量点实例与接口存在、接口可用、其预定义/连接状态与该计量一致；
- 时序计费按被引用价格的定价定义：`constant` 每点同价；`time_series` 按上文「规划配置与有效财务快照引用」的元数据规则与项目基线核对，不匹配阻断且不重采样；时变价格按逐点计价 `Σ price[t]×e[t]`（聚合与计价衔接见[财务 YAML 契约](finance-yaml.md)），禁止先对时变价格总量聚合再与总能量相乘。

## 计算包生成边界

装配 YAML 不包含 `calculation`。规范 `ValidatedAssemblyArtifact` 与独立计算配置一起进入计算包生成用例。计算配置固定 mode、预测目标所用算法与参数、计算精度、离散化、generator、solver、阶段二适用时的收敛容差与最大迭代数、时间限制、选项、随机种子和输出选择，并在生成 Solver Bundle 前完成能力兼容校验。

更换 generator、solver、精度或求解选项只会形成新的计算配置和 Solver Bundle，不改变装配文本。

装配 YAML 禁止 shell、executable、参数字符串、脚本、动态导入路径、环境变量、凭证、宿主机工作目录和输出文件路径。这些执行细节只由受信任生成器写入 [Solver Bundle](solver-bundle.md)。

## 四阶段校验

1. **结构校验**：安全 YAML、schema、字段类型、ID和引用形状（含 `finance` 引用、`finance_binding` 判别联合、`tariff_bindings` 的 binding_id 结构与命名规则）；
2. **模型与数据校验**：设备内容、properties、equations、predefined interfaces、数据时间覆盖、单位与状态；
3. **图与系统校验**：interface 类型、连接、carrier、拓扑、平衡和系统约束；校验 `existing`/`new` 身份，以及 `tariff_bindings` 的实例/接口存在性、接口流向与方向可用性；
4. **规划与财务完整性校验**：每个实例的显式设备成本绑定（`costed`/`none`）、按增量语义的 driver 齐全性（`new` 全量、`existing` 仅周期可变分量）、`tariff_bindings` 的 `price_id` 存在性、载体/方向相容性与 binding_id 唯一性、目标函数逐项引用（财务分量标识符映射与带单位常量的量纲/`currency` 一致性）、规划变量/约束、有效财务快照引用与 `driver` 映射的聚合/量纲/存在性校验。

同阶段尽量聚合可修复诊断；任何 error 都不产生可执行产物。

## `ValidatedAssemblyArtifact`

成功结果是不可变二件套：

1. 规范装配文本：序列统一为项目基线下连续 `step`，资源变为内容 ID，字段和集合按规定排序；`finance` 引用、`finance_binding` 与 `tariff_bindings` 亦按稳定顺序规范化；
2. 校验回执：校验器 ID/版本、schema、项目基线摘要、设备内容锁、方程 contract、规划配置 revision、有效财务快照引用、资源摘要和零阻断诊断。

生成器直接消费这两者。人工修改规范文本、替换资源或变更财务快照/绑定必须形成新的装配产物；任务创建后只消费该产物，不得重新读取“当前设备”“当前价格”或“最新项目”。

规范化算法标识为 `ies.assembly.canonical@2.0.0`。相同语义必须得到相同规范文本、摘要和回执；算法语义变化必须升级版本并保留历史解释能力。

## 完成标准

- 示例补齐真实设备摘要、`finance` 精确引用、`finance_binding` 判别联合与 `tariff_bindings` 绑定映射后可通过 `2.0.0` schema；
- 装配没有设备独立版本、parameters/ports/model commands 或设备经济字段；财务成本模型来自被引用的有效快照，不来自设备文件，也不在装配内内联完整财务配置；
- 五类 interface、三类 predefined 来源、存量/新增、设备成本绑定（`costed`/`none`）、按增量语义的 driver 齐全性、`tariff_bindings` 的 `price_id`/`carrier`/`direction` 校验（含 `type: none` 计量点）、财务分量标识符映射（实例前缀与绑定前缀的命名空间规则）与带单位常量量纲校验、规划目标逐项引用及有效财务快照身份/血缘均有契约测试；
- 非法文件没有旁路进入生成器，合法文件只产生一个规范形态；
- 生成器只读 `ValidatedAssemblyArtifact`，不重新解释原始项目、CSV 路径或 GUI 状态；
- 旧装配通过显式离线迁移和迁移回执进入新格式，不保留运行期兼容分支。
