# 装配 YAML

> 契约标识：`ies.assembly`；目标 schema：`5.0.0`（规划约束新增显式端口引用与逐步技术关系；运行基线沿用 `start_year`）；推荐文件名：`<assembly-id>.assembly.yaml`。
> 文档状态：生效目标契约；本页只定义目标文件语义，不声明实现进度。

装配 YAML 是系统模型与规划意图的完整、可审查文本：它固定项目计算基线，实例化精确设备内容，绑定已校验的预定义来源声明、规范数据和不可变输入引用，连接可连接的真实 interfaces，区分存量/新增设备，为每个设备实例显式声明**设备成本**财务绑定（判别联合 `costed`/`none`），并用独立的 `tariff_bindings` 把能源价格条目（`price_id`）绑定到计费点实例/接口与聚合（计费点可以是 `type: none` 的纯计量实例），用精确引用固定装配前合并签发的不可变有效财务快照，并承载规划配置。它不包含物化后的未来序列、generator、solver、预测算法、计算精度或求解选项；这些计算事项在规范装配产物转换为计算包时固定。财务三件套的 schema 与字段见[财务 YAML 契约](finance-yaml.md)。

## 最小结构示例

本页字段表与 YAML 示例固定为 `5.0.0`。下文「建设激活目标语义」及[端口级网络语义](../modules/assembly.md#端口级网络语义)约束后续扩展，不是此版本隐含支持的字段或连接解释；发布时须更新装配、规范化及相关消费者的精确版本。

```yaml
schema: ies.assembly
schema_version: "5.0.0"

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

被引用的有效快照需含本装配用到的 `finance_type`（如 `pv_system`）与 `energy_prices` 的 `price_id`（如 `grid_import`/`pv_export`），完整字段定义见[财务 YAML 契约](finance-yaml.md)。实例 `instance_id`（如 `pv_1`/`grid`）、计量绑定 `binding_id`（如 `import_meter`）、财务 `finance_type`（如 `pv_system`）与技术模型 `device.id`（如 `ies.device.pv`）严格分离：`grid` 无设备成本、不绑定 `finance_type`（不再虚构 `grid_connection` 等零成本连接类别），其计量与计费完全由 `tariff_bindings` 表达。

规划目标示例把成本项显式列出：`pv_1__annual_fixed_om`（currency/年）、`pv_1__period_variable_om`、`import_meter__period_energy_purchase`、`export_meter__period_energy_sale`（currency，运行/计费窗口合计）。这些标识符是受限表达式可解析的普通 `Name`，其编码与映射规则见下文「规划表达式中的财务分量标识符」：设备成本分量 = `<instance_id>__<分量>`，能源计费分量 = `<tariff_binding_id>__period_energy_purchase|sale`，不使用点分路径。`annual_fixed_om` 与 `period_*` 量纲不同（currency/年 vs currency），不能直接相加：示例中计算窗口恰好是快照冻结运营期覆盖的完整时间轴，表达式通过**显式时间跨度字面量（示例为单年窗口的 `1 year`，运营年限由提交时显式确认，此处取 1；非默认）**把按年值换算为窗口金额（currency）后再与 `period_*` 相加；缺少显式时间跨度时，不得默认相加或做其他隐式缩放（规则见[财务 YAML 契约](finance-yaml.md)「时间口径与分量」）。会计符号由被引用价格的显式 `direction` 统一决定（定义见[财务 YAML 契约](finance-yaml.md)「计价规则」）：每个计量绑定先算 `raw_charge = Σ price×energy`，`purchase`（如 `grid_import`）分量为 +raw_charge、`sale`（如 `pv_export`）分量为 −raw_charge。分量是**已带符号的记账贡献**（正售电价时 sale 分量自身为负，收益自动抵减成本），因此目标表达式对所有分量**统一使用加法**——示例写 `+ export_meter__period_energy_sale`，不再书写额外负号，也不把“sale 以负号计入”写为规则。该示例是“预算上限约束下的年度运行成本最小化”这一**具体示例，不是通用经济目标**：一次性 `upfront_capex` 不进该目标，改由预算上限约束显式处理（示例上限 `900000 CNY` 为规划人员输入的带单位常数，单位必须与 `EffectiveFinanceConfig.currency` 一致，不是系统财务规则）；具体如何把一次性投资转换为可与运行期成本比较的规划分量，属于 `PlanningConfig` 的明确、版本化规则，需在规划配置契约中定义，本财务 Profile 不隐式定义或默认选择这种转换（不隐含资本回收系数、利率或年限等值）。以上财务标量表达式沿用规划配置的受限语法；本段定义财务分量标识符的映射与量纲，逐步技术关系见下节；分量标识符引用设备成本绑定（`pv_1`）与能源计费绑定（`import_meter`/`export_meter`），不依赖任何隐式“系统总成本”聚合。

## 顶层职责

| 字段 | 作用 |
|---|---|
| `assembly` | 本装配的稳定 ID 和人类可读名称 |
| `project_baseline` | 项目创建时固定的时间分辨率、场景模式与运行起始年（`3.0.0` 删除 `leap_year`，闰年一律逐年按公历推导） |
| `devices` | 设备实例、精确设备内容、存量/新增身份、允许的 property 覆盖和预定义接口绑定 |
| `connections` | 可连接的真实 `<device>.<interface>` 之间的有向连接 |
| `finance` | 对装配前由 `FinanceProfile` + `FinanceOverrides` 合并生成、完整校验的不可变 `EffectiveFinanceConfig` 的引用：`ref` 携带对象身份，`profile_id` 携带来源标识；规划与财务计算共同消费同一引用；装配内不内联另一份完整财务配置 |
| `finance_binding` | 每个设备实例的**设备成本**财务判别联合：`type: costed`（含 `finance_type` 与 `driver → property/interface` 映射）或 `type: none`（该实例无设备成本贡献，可作纯计量点；能源计费不在此表达）。缺口、未知 `finance_type`、非法映射、按增量语义缺失的 driver 与单位量纲不兼容在装配校验中阻断 |
| `tariff_bindings` | 能源价格计费绑定：`binding_id → {price, instance, interface, aggregation}`，`price` 引用有效快照 `energy_prices` 的 `price_id`（`carrier`/`direction` 由 Profile 条目显式声明）；独立于设备成本绑定，计费点实例可以是 `type: none`；逐点计价与会计方向规则见[财务 YAML 契约](finance-yaml.md) |
| `demand_bindings` | 必填独立需量计量映射；无需量费时 `{}`，不依赖能源价格绑定，见「自然月需量计量绑定」 |
| `planning` | 目标函数、规划变量、上下界和规划/系统约束；目标逐项引用设备成本或能源计费分量（见上方规划示例），不依赖隐式总成本 |
| `extensions` | 命名空间化扩展 |

`planning.constraints` 为命名映射；每项必须显式声明约束 `type` 与 `expression`，`enabled` 省略时为 `true`。既有标量约束使用普通标识符 `Name`；`step_relation` 使用下节定义的局部端口引用与 `[t]` 索引。上方示例用 `annual_purchase_cost_limit` 限制运行窗口内的购能成本；一次性 `upfront_capex` 不会被系统隐式并入该约束或目标。把一次性投资转换为可与运行期成本比较的规划分量属于 `PlanningConfig` 的版本化规则，本财务 Profile 不隐式定义或默认选择（见[财务 YAML 契约](finance-yaml.md)「时间口径与分量」）。数值常量规则：**有维度的常量必须带单位字面量**（金额如 `900000 CNY`、时间跨度如 `1 year`），单位必须与 `EffectiveFinanceConfig.currency` 一致，量纲不一致阻断；**纯无量纲系数可写裸数字**（如 `0.5 * pv_1__period_variable_om` 中的 `0.5`），不需要也不允许编造单位。财务分量引用的合法标识符与单位字面量规则见下文「规划表达式中的财务分量标识符」。

## 逐步技术约束

跨实例的端口技术关系写入 `planning.constraints.<id>`，以 `type: step_relation` 明确区别于全期标量约束；不另建网络资源或设备专用约束字段。

| 字段 | 语义 |
| --- | --- |
| `type` | 固定为 `step_relation` |
| `references` | 必填非空映射，局部别名 → `{instance, interface}`；引用本装配实例及其真实公开端口 |
| `expression` | 必填受限技术关系，端口以 `别名[t]` 引用 |
| `enabled` | 布尔值，省略为 `true`；`false` 保留完整声明，但不施加关系 |

别名为 `lower_snake_case`，作用域只限本条约束，不与规划变量、`step_duration` 或保留财务标识符重名。引用只指向可连接的非标量 `in/out/bidirectional` 接口；设备内部变量、状态和预定义数据通过设备自身方程表达。引用使用实例键和真实 interface ID，不使用显示名或求解器列名。

表达式支持仿射的 `==`、`<=`、`>=` 关系。规划变量作为无索引标量参与每一步，端口量必须带 `[t]`；有量纲常数携带单位，`step_duration` 为基线确定的带单位步长。相加和比较的量纲必须相容。此类技术关系不消费财务分量或评价指标；变量乘变量、跨步索引、固定步下标及区间聚合不属于本版表达范围，跨步设备状态仍由设备模型定义。

启用关系在完整运营时域的每个 `t` 成立，时域与设备方程、物化序列一致，不包含建设期现金时点。生成器将别名绑定到设备和母线已经使用的同一端口变量，不新建替代流量。示例 `boundary[t] <= supply[t]` 只要求逐步外送功率不超过指定供给端口功率；它不分配电量来源，也不建立母线隔离。需要来源分配时，模型必须显式给出相应关系，不能仅以画布缺边或路由内部守恒代替。

字段、引用存在性、索引和量纲在公开规划／装配输入边界核对一次，错误定位到约束及引用。生成器通过 `technical.step_constraints` 声明完整执行能力；实际启用此类约束而所选生成器不具备该能力时，任务提交返回 `TASK-REQ-007`。生成阶段消费可信声明，只处理数学降级能力，不重做输入校验。

此扩展改变可行域，旧消费者不能安全忽略，故装配与规范化算法分别使用 `ies.assembly@5.0.0`、`ies.assembly.canonical@5.0.0`。嵌入的规划结构随装配版本解释，不增加另一份规划格式字头。其余标量约束、目标与规划变量保持原有语义；输入、冻结、执行和编辑入口统一迁移，不提供静默忽略新关系的旧字头路径。

## 设备内容固定与实例

每个设备实例用 `definition.id` 固定具体设备内容（按字头校验，不做内容摘要）。`schema_version` 版本化统一设备格式，不是某台设备的语义版本；装配不得出现 `device_version`、`model@version`、`latest` 或设备私有命令版本。

实例规则：

- `asset_origin` 必须是 `existing` 或 `new`；不得从设备类型、创建时间或是否填写成本推断；
- `properties` 只能覆盖设备定义已声明且允许实例化的非时变技术常量，并保留明确单位；不能新增字段，也不能放价格、成本或计算精度；
- `predefined_interfaces` 只能绑定设备中 `type: predefined` 的 interface；每个 predefined 槽都必须恰好有一个显式绑定，缺失或绑定不存在/非 predefined 接口均阻断；
- 来源判别联合严格为 `constant: {mode, value}`、`data_repeat: {mode, path}`、`data_full: {mode, path}`、`data_predict: {mode, path, target_type}`；`path` 只能是项目目录内 CSV 相对路径。缺少必填字段、混入其他模式字段和所有额外字段均拒绝，没有默认 mode 或默认值；
- `constant/data_repeat/data_full/data_predict` 只固定来源声明及项目相对 CSV 路径，不得在装配前替换为物化后的未来序列；规范化文本必须完整保留 `value/path/target_type`；
- `in/out/bidirectional` 通过 connections 取得外部交互；`blind` 既不能连接，也不能绑定预定义数据；
- 每项覆盖、绑定和身份均进入校验回执；
- 资产身份与增量成本强关联：`existing` 的沉没历史建设成本不计入增量目标，`new` 按新增驱动量计入固定建设成本与线性分量；但所有设备仍计算决策相关可变 O&M 与能源购售（能源购售只经 `tariff_bindings` 计量点产生，见[财务 YAML 契约](finance-yaml.md)）。

## 连接

本版连接的已发布生成语义见[生成器版本说明](../modules/generators.md#项目级线性模型与-mps-writer)。端口级网络扩展以[装配模块](../modules/assembly.md#端口级网络语义)为唯一语义正文，不把旧连接的方向标签直接当作独立支路流量。

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

`project_baseline` 在创建项目时一次性固定，当前包含：

- `resolution`：全项目统一计算分辨率，必须能确定性切分一天；
- `scenario_mode`：当前固定为 `single`；
- `start_year`：运行起始年，公历整数年，允许过去、现在或未来，不做“必须是未来年”校验，不涉闰秒；与财务 `base_year`（金额口径基年）是两个概念，不得互相推断。

基线不保存时区、典型日/周/年或计算截取区间。计算序列统一使用从 `0` 开始的连续 `step`；全周期点数与年度边界 step 下标由 `resolution`、`start_year` 与提交时确认的运营年限 `operation_years` 逐年按公历唯一推导（闰年按实际公历年份确定，无 `leap_year` 输入），随快照冻结。公开计算变量 `step_duration` 由 `resolution` 唯一推导并携带明确时间单位，是状态方程和能量积分使用的唯一公开步长变量。已有项目基线变更或多场景必须通过新的公开契约定义，不能作为本格式的隐式例外。

## 预定义序列与资源

实例化配置直接以项目相对路径绑定 CSV：

```yaml
path: data/campus_load.data.csv
```

`path` 是实例化配置中的项目相对 CSV 路径，不能逃逸项目目录。后端在实例化入口直接读取并解析该文件，随后固定来源绑定；进入可信内部流程后不再因文件交接而重算校验。网络 URL、宿主机绝对路径、临时上传路径和存储 provider 私有路径不得进入可执行快照。

CSV 必须固定相同的设备 ID，并且列 ID、单位、`source_mode`、分辨率、输入 `step` 和有效区间与目标 predefined interface 一致。装配使用实例化入口形成的项目相对 CSV 来源绑定。装配只确认每个来源自身规则（与项目基线分辨率一致、文件内 `step` 从零开始连续、`data_repeat` 基线形态完整）；`data_full` 原样覆盖全周期全部点数的核对不在装配时做，统一在任务提交边界（取得已确认运营年限 `operation_years` 后），不足或超出阻断，不循环、不外推。任一来源自身不符合时必须阻断，不重采样、插值、聚合、融合或补齐。计算阶段物化后，所有计算序列统一为全周期点数和连续 `step`，并在产物与回执中固定。

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
- 引用价格来源的元数据与项目基线须相容；来源字段及日曲线／全期长度要求以[财务 YAML 契约](finance-yaml.md)为准，装配不另定义价格来源规则。实际来源与已确认运营时轴的核对统一在任务提交边界；不一致阻断，不重采样、不插值。时变价格物化后逐点计价（`Σ price[t]×e[t]`），聚合与计价衔接见财务契约；
- `planning` 只保存目标函数、目标权重、规划变量、上下界和规划/系统约束。目标函数用普通标识符逐项引用财务分量（映射规则见下文「规划表达式中的财务分量标识符」；示例见上方最小结构：`1 year * pv_1__annual_fixed_om + pv_1__period_variable_om + import_meter__period_energy_purchase + export_meter__period_energy_sale`，其中 `1 year` 为显式时间跨度字面量（示例为单年窗口），把 currency/年 的按年值换算为窗口金额（currency）后再相加；能源计费分量**已带符号**——`purchase` 分量 = +raw_charge、`sale` 分量 = −raw_charge（`raw_charge = Σ price[t]×e[t]`，正售电价时 sale 分量自身为负），所以目标表达式对所有分量统一使用加法；负价自然反转该方向效果；缺少显式时间跨度不得默认相加）；该示例是“预算上限约束下的运行成本最小化”的具体示例，不是通用经济目标——把一次性 `upfront_capex` 转换为可与运行期成本比较的显式规划分量，属于 `PlanningConfig` 的版本化规则，须在该契约中定义后再使用；有效快照**不静默相加成默认总成本**，不提供隐式“系统总成本”标识符，Profile 不隐式年化或默认选择转换规则；规划配置显式选择或组合哪些分量；
- 项目覆盖是 authoring 输入，`EffectiveFinanceConfig` 是装配与计算唯一消费的不可变快照：运行期不继承 Profile、不读取“最新地区价格”、不静默默认值。计税折旧与税务关系由[财务模块](../modules/finance.md#税务参与规划与现金流)定义，装配通过有效财务快照引用；评价指标由同次解生成。

### 规划表达式中的财务分量标识符

全周期财务目标由 `planning.objective` 显式选择：`{sense: maximize, expression: system.after_tax_npv}`。该保留标记只能作为完整目标使用，不作为普通表达式或约束中的操作数；它不开放属性访问语法。目标金额单位为有效财务配置的币种，含义为同一冻结评价确认和财务规则下的全周期税后 NPV，公式与现金时点以[财务计算](../modules/finance.md#规划目标与同源评价)为准。公开规划/装配入口识别该目标并核对方向；具体计算能力在生成器能力边界判断。其余合法目标仍显式保留，不被自动替换。

财务标量表达式只接受普通标识符（`Name`）：财务分量不以点分路径或属性访问形式出现。财务分量在规划表达式中的合法标识符统一为：

```text
设备成本分量：<instance_id>__<分量>              # 分量 ∈ {upfront_capex, annual_fixed_om, period_variable_om}
能源计费分量：<binding_id>__period_energy_purchase | <binding_id>__period_energy_sale
需量计费分量：<billing_id>__period_demand_charge
```

例如 `pv_1__annual_fixed_om`、`import_meter__period_energy_purchase`、`export_meter__period_energy_sale`。新增需量分量是货币量纲的全运营期费用，前缀必须存在于 `demand_bindings`，引用其真实产生的费用。下列实例／能源计费命名约束同时扩展到需量绑定，三个前缀集合彼此不冲突。映射规则（装配规划校验强制）：

- 两个前缀集合共用同一表达式命名空间：`instance_id` 与 `binding_id` 都必须是 `lower_snake_case`、不含连续双下划线（`__`）、文件内唯一，且 **`binding_id` 不得与任何 `instance_id` 相同**（同名会把设备成本与能源计费两类分量混在同一前缀下）；因此 `<前缀>__<后缀>` 从左向右按首个 `__` 切分即可唯一解析，不与其它标识符碰撞；
- 后缀决定分量族：`upfront_capex`/`annual_fixed_om`/`period_variable_om` 只与实例前缀（设备成本）合法；`period_energy_purchase`/`period_energy_sale` 只与绑定前缀（能源计费）合法，且必须与被引用价格的显式 `direction` 匹配——`direction: purchase` 的 price 产生 `<binding_id>__period_energy_purchase`，`direction: sale` 的 price 产生 `<binding_id>__period_energy_sale`；方向不靠 binding_id 或 price_id 的名称推断，后缀与方向不匹配即阻断；
- 标识符到 `(前缀, 分量)` 的映射唯一可逆。装配校验确认该前缀在绑定下**实际产生**该分量：设备成本分量要求实例为 `type: costed`、其 `finance_type` 声明该分量且相关 driver 按增量语义齐全；能源计费分量要求该 `binding_id` 在 `tariff_bindings` 中绑定存在的 `price_id`（`carrier` 与接口相容、方向匹配）。`existing` 实例不得引用被排除的沉没分量（`upfront_capex`、`annual_fixed_om`）；不满足即阻断，防止把不存在的量写进目标；
- 规划配置不得把上述保留集合中的标识符声明为其它含义（与变量或技术量同名冲突即拒绝）；其余表达式标识符（模型技术量、规划变量等）由规划配置契约定义，本小节只定义财务分量引用的映射。

量纲与单位字面量：

- 财务分量标识符自带量纲：`upfront_capex`、`period_variable_om`、`period_energy_purchase`、`period_energy_sale` 为 currency；`annual_fixed_om` 为 currency/年。能源计费分量是**有符号记账值**：`purchase` 分量 = +raw_charge（正价时为正值）、`sale` 分量 = −raw_charge（正售电价时为负、负售电价时为正）；负号是记账符号而非物理量取负，能量序列本身非负；量纲恒为 currency、与符号无关；
- 表达式中的**有维度常量**以带单位字面量书写（`数值 + 单位标识符`，如 `900000 CNY`、`1 year`），单位标识符来自公共单位词汇；常量单位必须与 `EffectiveFinanceConfig.currency` 一致（示例币种 CNY），量纲不一致在装配规划校验阻断；
- **纯无量纲系数**（如 `0.5`、整数权重）可以裸数字书写，不需要单位字面量；含维度加减与比较的表达式由规划校验做维度检查；
- `annual_fixed_om` 与窗口合计分量相加前，必须显式乘以时间跨度字面量（示例为单年窗口的 `1 year`，运营年限由提交时显式确认，此处取 1；非默认）；缺少显式时间跨度不得默认相加；
- 本小节只定义财务分量标识符的编码、映射与量纲规则，不定义规划表达式语言本身（语法与运算符由规划配置契约提供）。

## 建设激活目标语义

本节规定建设决策的目标语义，不为 `5.0.0` 新增可解析字段。公开字段与配套版本在该能力实现前于本格式页完整定义，不能由消费者自创 `trigger`、`allow_zero_skip` 等别名。

- 建设状态归明确的装配实例或建设单元，由规划声明必建、可选建及其规模关联。一个建设单元的多个规模变量共享同一激活状态；`asset_origin: new` 本身不表示已决定建设。
- 可选建状态 z 为二元决策；未激活时所关联新增规模为零，相关新增运行能力由模型约束归零；已激活时规模满足显式活动区间。有限上界使用[统一上下界](../modules/generators.md#统一上下界)，不从参考设备值取得。
- 关系 `L_active × z <= q <= U × z` 中，正的 `L_active` 只能来自明确的最小建设规模。未规定正最小规模时允许激活而规模为零，结果原样报告 z 与 q；不把 z 称为 `q>0` 的严格指示，不猜 epsilon，也不依赖目标函数修改其含义。
- 费用分别声明无条件发生或依赖某建设状态；固定项仅表示金额对规模不变，不自动代表任何触发条件。建设状态与费用发生期间、支付时点分别定义，不优化建设年份。
- 同一状态可被初建费用、年度费用和相关资产关系引用，但引用不会自动新增现金支付、税前扣除或残值。既有资产不因新建状态为零而被删除。
- 建设状态、容量与相应费用从同次主解恢复并封存；前端、报告不得依据容量阈值另算选建结果。

合成语义例：同一设备的容量 C 与功率 P 满足 `C = 3 hour × P`，两者共享一个 z；建设费 `80 CNY × z + 6 CNY/kWh × C` 随选建发生，另行声明的项目管理费 `20 CNY` 无条件发生。两笔费用的支付时点仍分别由财务声明。此例不定义新的 YAML 写法。

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
- 技术接口的 `in/out` 描述能量进出设备，价格的 `purchase/sale` 描述本次核算主体的付款或收款，两者独立声明；合法的单向计量接口均可承载显式购买或销售，不以技术方向推断经济方向，也不按设备 ID 或名称特判。价格 `carrier` 必须与接口载体相容，单位与非负计量语义仍按既有规则核对；不扩展双向净值或绝对值计量，不自动识别核算主体；
- 实例与绑定状态互不约束：被 `tariff_bindings` 引用的实例可以声明 `type: none`（纯计量点，如示例 `grid`），也可以 `type: costed`；`type: none` 只表示无设备成本，不禁止承担能源计费；装配校验确认计量点实例与接口存在、接口可用、其预定义/连接状态与该计量一致；
- 时序计费使用财务来源物化后的逐点价格，常数每点同价；来源与项目基线相容性按上文「规划配置与有效财务快照引用」处理。聚合与计价规则以[财务 YAML 契约](finance-yaml.md)为准，禁止先聚合时变价格再与总能量相乘。

## 自然月需量计量绑定 `demand_bindings`

必填映射 `billing_id → {charge, instance, interface}`；无需量计费时为 `{}`。`charge` 引用有效财务快照的 `demand_charges`，实例与接口由用户指定，不引用能源价格或 tariff_binding；仅有需量费而没有按电量计价同样有效。

计量接口必须是电力载体、方向为 `out` 的非负时序功率接口，表示外部向系统供电；不接受双向净值、绝对值、标量或区间电量，不积分、不隐式从电量推导功率。实例可以为 `type: none`。接口、量纲和引用在装配输入边界核对，内部流程不重复校验。

`billing_id` 与实例、能源计费 binding_id 共用互不冲突的局部命名空间（不含 `__`）。同一费项与同一实例接口的重复绑定拒绝；同点不同费项可以由用户显式声明并分别计费，不自动合并不同实例功率。

每个绑定公开全运营期费用分量 `<billing_id>__period_demand_charge`，单位为币种、费用非负，金额沿快照 `price_basis` 等于各月费率乘峰值的合计；可在规划表达式与约束中显式引用。现金流按明确税务规则形成实际含税付款并只扣一次。无论是否将该分量写入目标，财务事实都必须反映该解的真实月峰与费用。

## 首年建设与全周期运行优化

本计算课题在项目首年启动建设，按明确建设期完成后投产，优化建设选择、容量与全周期运行，不优化何年启动建设。中期扩建属于新的计算课题，不在本课题中增加分年建设决策。首年启动建设不等于立即投产、投资一次付清或只求解首年运行。

- 规划变量按设备实例及参数定义建设容量与上下界，不携带年度新增索引；每个候选实例是否建设及建设容量由规划方案决定，不建立逐年新增变量或累计在产容量链。
- 建设投入：选建实例的固定 `upfront_capex` 计一次，容量建设费按选定容量计算；未选建实例不计固定建设费。总建设费用与支付安排分开，费用按明确支付时点进入现金流，不自动全部归 `t=0` 或均摊。固定费必须与建设选择联动，不能作为所有候选实例无条件收取的常数。
- 全周期运行：保留连续时间轴上的技术方程、状态、预测与时变价格；年度运维与能源收支由同一次全期解按年度边界归集，不复制单年结果。维护换新不作为新增建设容量。
- 同一次全期解形成现金流：建设期、支付安排、运行期与评价时点的关系及维护换新口径见[财务计算](../modules/finance.md)；不得省略建设阶段或提前产生运行收益。

## 计算包生成边界

装配 YAML 不包含 `calculation`。规范 `ValidatedAssemblyArtifact` 与独立计算配置一起进入计算包生成用例。计算配置固定 mode、预测目标所用算法与参数、计算精度、generator、solver、耦合预测收敛条件、时间限制、选项、随机种子和输出选择，并在生成 Solver Bundle 前完成能力兼容校验。

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
2. 校验回执：校验器 ID/版本、schema、项目基线、设备内容锁、方程 contract、规划配置 revision、有效财务快照引用和零阻断诊断。

生成器直接消费这两者。人工修改规范文本、替换资源或变更财务快照/绑定必须形成新的装配产物；任务创建后只消费该产物，不得重新读取“当前设备”“当前价格”或“最新项目”。

规范化算法标识为 `ies.assembly.canonical@5.0.0`，包含需量绑定及逐步技术约束的完整声明；引用映射按稳定键排序，表达式与启用状态均保留。相同语义必须得到相同规范文本和回执；算法语义变化必须升级版本并保留历史解释能力。

## 完成标准

- 示例的真实设备引用、财务精确引用、成本／能源计费及需量绑定、逐步技术约束必须满足 `5.0.0` schema；
- 装配没有设备独立版本、parameters/ports/model commands 或设备经济字段；财务成本模型来自被引用的有效快照，不来自设备文件，也不在装配内内联完整财务配置；
- 五类 interface、来源模式（`constant`/`data_repeat`/`data_full`/`data_predict`）、存量/新增、设备成本绑定（`costed`/`none`）、按增量语义的 driver 齐全性、`tariff_bindings` 的 `price_id`/`carrier`/`direction` 校验（含 `type: none` 计量点）、财务分量标识符映射（实例前缀与绑定前缀的命名空间规则）与带单位常量量纲校验、规划目标逐项引用及有效财务快照身份/血缘均有契约测试；
- 非法文件没有旁路进入生成器，合法文件只产生一个规范形态；
- 生成器只读 `ValidatedAssemblyArtifact`，不重新解释原始项目、CSV 路径或 GUI 状态；
- 逐步关系覆盖完整运营时域，使用同次主解的真实端口变量；容量规划参与关系时不被预代入常数。非法引用、索引和量纲在公开入口拒绝，无能力生成器不接收启用的逐步关系；
- `ies.assembly` `1.0.0` 是未发布开发草案；其 schema、迁移器、迁移回执、样例和兼容路径直接删除，不提供 1.0/2.0 双轨或离线迁移入口。
- `ies.assembly` `4.0.0` 新增必填 `demand_bindings`（无需量时为空）；开发数据按现行目标重建，不提供旧字头解析或离线迁移入口。版本化历史事实不以新字段补默认值解释。
- `ies.assembly` `5.0.0` 新增 `step_relation`；项目保存、导入导出、规范装配和任务冻结保留同一声明，历史产物按自身固定版本解释，不改写为新版。
