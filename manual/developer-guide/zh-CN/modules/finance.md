# 财务计算

> 文档状态：生效蓝图；代码边界：`backend/iesplan/finance/`。
> 财务三件套 `FinanceProfile` / `FinanceOverrides` / `EffectiveFinanceConfig` 的文件契约（schema、字段表、完整示例、规范化与校验、失败语义）以[财务 YAML 契约](../formats/finance-yaml.md)为唯一字段级权威正文，本页只描述模块职责、分割线与其消费方式。

## 作用

`finance` 管理整体模型共用财务参数，并把建设投入、运行结果和这些经济假设转换成可复核的现金流与财务指标。它必须让每个参数、指标的口径、时间点和状态可复核，避免把一个看似正常的数字当作完整财务结论。

财务模块分为两个范围，本页明确分段：

1. **财务文件契约**：`FinanceProfile` / `FinanceOverrides` / `EffectiveFinanceConfig` 的领域结构、YAML 解析、安全规范化与完整校验，以及把合并后的成本/能源分量交给装配、规划与计算消费的边界。字段级定义全部在[财务 YAML 契约](../formats/finance-yaml.md)。
2. **长期指标蓝图（FinancialResult）**：把规范化现金流与运行汇总解释为项目/资本金现金流及 NPV、IRR、LCOE、回收期等指标，形成不可变 `FinancialResult`。这是模块蓝图目标，与本次 Profile 文件契约分开规划；Profile 文件契约不包含评价期、折旧、融资等后评价专用输入，蓝图也不能据此提前实现输入字段。

## 边界

模块负责：

- 财务三件套的领域结构、解析、规范化与校验，以及确定性合并器的语义（字段细节见[财务 YAML 契约](../formats/finance-yaml.md)）；
- 按时间口径组织成本/能源分量：`upfront_capex`（建设期一次性，仅 `new`）、`annual_fixed_om`（按年度，currency/年）、`period_variable_om` / `period_energy_purchase` / `period_energy_sale`（按运行/计费窗口合计，currency；两个能源分量为**有符号记账贡献**、由 `tariff_bindings` 的每个计量绑定独立产生，符号约定见[财务 YAML 契约](../formats/finance-yaml.md)「计价规则」）；不把分量静默相加成默认总成本，按年分量与窗口合计相加前必须以显式时间跨度换算，组合目标由规划配置显式选择；
- 长期蓝图：从规范运行汇总生成不可变 `FinancialResult`，并对每个指标独立计算与分类失败状态。

模块不负责从数据库找价格、不选择当前项目版本、不判断用户权限、不调度任务，也不修改计算结果。`backend/iesplan/devices/catalog/prices.yaml` 与 `$price` 不作为第二权威源，不得静默回退或运行期兼容；不保留 `finance_config.json` 别名或 JSON/YAML 双格式兼容。

设备技术文件（`ies.device-model`）不提供任何价格或成本。财务成本模型按独立财务类别 `finance_type` 定义在地区 `FinanceProfile` 中；装配为每个实例显式声明**设备成本**绑定 `finance_binding`（判别联合 `type: costed`/`type: none`），`costed` 实例把 `finance_type drivers → device property/interface` 显式映射并校验存在性、聚合与单位量纲，且按 `new`/`existing` 增量语义校验实际计入分量所需 driver 齐全（防止漏成本，沉没分量不强配 driver），禁止按名称、载体或技术模型 ID 猜测财务类别。能源购售通过装配中的独立 `tariff_bindings`（`binding_id → {price, instance, interface, aggregation}`）把 `energy_prices` 的 `price_id` 绑定到计费点实例、接口与聚合：会计方向与载体由价格条目显式声明（`direction: purchase|sale`、`carrier`），不靠键名推断；计费点实例可以是 `type: none` 的纯计量点（`none` 只表示无设备成本，不禁止承担能源计费），`binding_id` 不得与 `instance_id` 相同（装配细节见[装配 YAML](../formats/assembly-yaml.md)）。

规划与财务评价是两个连续但不同的计算阶段，但共用同一份有效财务快照：规划配置独立定义目标函数、规划变量、边界和约束；`EffectiveFinanceConfig` 只定义两阶段都使用的财务参数，不包含目标函数、计算算法或仅在某一阶段应用的数据。规划求解使用这些参数影响“建什么、建多大、怎样运行”，finance 在方案确定后使用同一 `EffectiveFinanceConfig` 解释建设计划和运行结果，形成现金流及评价指标。

## 输入与有效快照消费

| 输入 | 关键要求 |
|---|---|
| `FinanceProfile` / `FinanceOverrides` | 见[财务 YAML 契约](../formats/finance-yaml.md)：Profile 是已注册、可复用的地区财务基准，不得以内置形态充当全局默认；Overrides 引用 `profile {id}`，对既有成本分量叶子做 `{value, unit}` 原子替换、对能源价格条目（`price_id`）做定价定义整项替换（`carrier`/`direction` 由 Profile 继承，禁止新增/删除 `price_id`） |
| `EffectiveFinanceConfig` | 唯一由确定性合并器从 Profile（+ Overrides）生成并完整校验的不可变快照；可导出/导入/进入快照，导入时连同来源 Profile 与 Overrides 重新合并验证；装配、规划与计算只消费同一引用（见[装配 YAML](../formats/assembly-yaml.md)） |
| 建设与替换计划 | 金额币种、发生期、`price_basis`（含税/不含税口径标签）和 `base_year` 明确 |
| 逐时或年度运行汇总 | 能量、成本、收益的单位和时间范围明确 |
| 规划与财务基准 | 与当前方案使用同一 `EffectiveFinanceConfig` |

金额与需要精确往返的费率使用十进制定点语义（`Decimal` 字符串，`{value, unit}` 原子，禁止 `null` 与部分金额）；物理量来自规范计算输出。模块不能自行读取“最新地区价格”补齐缺失输入，也不运行期继承 Profile 或覆盖。

### 成本语义与增量规则

- 成本函数为 `cost_method: fixed_plus_linear`：固定建设成本加多个独立线性分量，按明确 `driver` 计量；不做二次、分段、非线性或采购汇总。成本金额非负（允许 `0`，零费用类别合法）；
- 固定 O&M 按 `driver` 的年度单价计费，不使用费率模型 `fixed_om_rate`；可变 O&M 按明确运行 `driver` 计费。
- `new` 计算建设期一次性与新增固定 O&M；`existing` 不计沉没历史建设成本与决策无关的固定 O&M；所有设备仍计算决策相关能源购售与可变 O&M，不做基准方案双重求解。
- 时间口径组合：`annual_fixed_om` 为按年值（currency/年），`period_*` 为窗口合计（currency）；两者相加前必须经显式时间跨度换算（示例：窗口为项目基线完整一年时，按年值 × `1 year`），窗口不是完整一年时不得默认相加。把一次性 `upfront_capex` 转换为可与运行期成本比较的显式规划分量属于规划配置契约的版本化规则，本模块与 Profile 不隐式定义或默认选择该转换（不做隐式年化，不隐含资本回收系数、利率或年限等值）。

### 能源价格

`energy_prices` 是 `price_id → 价格条目` 的开放映射（`price_id` 为稳定自定义名，`lower_snake_case`、不含 `__`；不存在固定白名单价格键，封闭的载体清单只在公共载体词汇中维护——见[设备模型 YAML](../formats/device-model-yaml.md)「interfaces」的 `carrier` 规则，本页不复制）。每个条目显式携带 `carrier`（来自公共载体词汇，与设备接口同一词汇源）与 `direction: purchase|sale`（会计方向，不靠 price_id/键名推断）；定价定义为 `constant {value, unit}` 或引用经校验完整年度序列的 `time_series`。价格为有限 Decimal，正、零、负均合法，禁止 NaN/Infinity。每个计量绑定产出的计费分量是**已带符号的记账贡献**：先算 `raw_charge = Σ price[t]×e[t]`，`purchase` 分量 = +raw_charge（正价时为正，成本向）、`sale` 分量 = −raw_charge（正售电价时为负，收益以负值自动抵减成本）；负价自然按该方向反转（负购电价使 purchase 分量为负、负售电价使 sale 分量为正），零电价合法；规划表达式对已签名分量统一使用加法，不再书写额外负号。`time_series` 引用携带 `resolution`/`leap_year`/`point_count`/`unit` 等不可变元数据，装配时与项目基线核对，不一致阻断且不重采样（Profile 不固定唯一项目分辨率）。装配 `tariff_bindings` 以 `binding_id → {price, instance, interface, aggregation}` 把每个用到的 `price_id` 绑定到方向明确的计费点实例接口与聚合，校验价格 `carrier` 与接口载体相容、`direction` 与接口流向一致；同一价格可被多个绑定复用。时变价格按逐点计价：聚合先得到与价格同轴的能量序列 `e[t]`，再按 `Σ price[t]×e[t]` 计费（`constant` 每点同价，结果与 `price×Σe[t]` 一致但语义仍为逐点）；禁止先对时变价格总量聚合再与总能量相乘（聚合与计价衔接见[财务 YAML 契约](../formats/finance-yaml.md)）。

## 长期蓝图：`FinancialResult`

`FinancialResult` 蓝图至少表达：

- 逐期项目和资本金现金流；
- 投资、运行成本、收益和残值等组成；
- NPV、IRR、LCOE、回收期及单位；
- 每个无法正常计算指标的独立状态与原因；
- `EffectiveFinanceConfig`（含币种、`price_basis`、`base_year`）与计算版本。

IRR 必须与 `normal、no_solution、multiple、degenerate、out_of_domain、numeric_failure` 等状态一起消费；不能用 `null` 或零模糊这些情况。该蓝图需要的评价期、资金时间价值、折旧等输入不属于本版 Profile 文件契约；落地时按「增加财务指标」流程补入独立契约，不回流为财务文件契约的隐式字段。

## 蓝图设计思路

1. 把输入规范成时间点明确的现金流分量；
2. 再组合项目现金流和资本金现金流；
3. 对每个指标独立计算并分类失败状态；
4. 做内部一致性检查，例如分量之和、时间长度和币种一致；
5. 最后形成不可变财务结果，交给 analysis 和证据层。

财务指标函数应保持纯计算。application 负责选择哪份快照和基准，finance 只解释已明确传入的事实。

## 增加财务指标

1. 写清业务定义、单位、时间点和适用范围；
2. 明确输入不足、分母为零、多解或无解时的状态；
3. 用手算小样例和边界现金流建立测试；
4. 将指标加入 `FinancialResult` 的版本化 contract；
5. 更新 analysis 对指标的读取，但不让 finance 依赖 analysis；
6. 同步结果解释和更新日志中的用户可见变化。

## 失败语义

- 币种或口径（`base_year` / `price_basis`）不一致：拒绝计算；
- 非有限值、长度错误或缺少必需分量：输入诊断；
- `FinanceOverrides` 引用了未知 `profile id`、覆盖不存在的 `finance_type`/分量/`driver`/`price_id`、改变 driver 集合或分量类型、覆盖中改写 `carrier`/`direction` 或新增/删除 `price_id`、改变单位/币种/`price_basis`/`cost_method`、出现 `null` 或部分金额：阻断，不产生 Effective；
- 被引用的 `time_series` 未通过校验、不是完整年度序列、元数据（resolution/leap_year/point_count）与引用声明或项目基线不一致：阻断，不重采样；
- IRR 无解、多解或退化：返回对应业务状态，不抛普通内部异常（长期蓝图）；
- 数值算法未收敛：返回 numeric failure 和受控细节；
- 基准缺失：由 application 在调用前阻断，finance 不创建零基准。

## 必须遵循的规范

- 金额口径（`price_basis` 标签）、币种和时间点必须显式；`Profile.taxes` 只登记税目（税种、法定税率与适用对象），本版成本与价格计算不消费税率：分量金额按文件值直接使用，不含税金额不自动计税、含税金额不重复加税；本契约不定义计税/扣税/税后换算，不扩展成税务引擎；
- 百分比进入模块前已是规范比例；
- 不依赖 computation 内部实现、HTTP、ORM 和全局价格表；
- 不用浮点显示舍入值反推现金流；
- 每个指标的状态不能被综合分数覆盖；
- 同一 `EffectiveFinanceConfig` 必须同时进入规划和财务计算证据；
- 财务配置不得包含目标函数、规划约束、solver 选项或阶段性数据；
- 财务三件套声明式配置使用 YAML，经安全子集解析与规范化，不保留 `finance_config.json` 别名或 JSON/YAML 双格式兼容；
- 财务成本模型不得写入设备技术文件，不得依赖 `backend/iesplan/devices/catalog/prices.yaml` 与 `$price` 第二权威源；
- `device.id`（技术模型）、`finance_type`（财务类别）、`instance_id`（装配实例）与 `binding_id`（计量绑定）严格分离：装配实例通过显式 `finance_binding`（`costed`/`none`）声明设备成本，禁止猜测；能源计费走装配 `tariff_bindings`（`binding_id → {price, instance, interface, aggregation}`），`type: none` 的纯计量实例可以承担计费；
- `FinanceProfile` 不以内置形态充当全局默认；内置只作为样例或注册 provider 的输入。

## 完成标准

- 财务三件套解析、规范化与合并的契约测试以[财务 YAML 契约](../formats/finance-yaml.md)为字段权威；拒绝测试覆盖未知 `finance_type`、越权覆盖、`null`/部分金额、旧 JSON 别名，以及外部导入边界的对象身份不匹配；
- 手算基准、正常现金流及 IRR 各异常状态均有测试（IRR 属长期蓝图，独立验收）；
- 项目和资本金口径不会混用；
- 输入不完整时没有默认现金流或伪指标；
- 结果可序列化、可追溯且足以生成用户解释；
- 新指标不要求 finance 读取项目或任务内部状态。

代码阅读从 `FinanceProfile` / `FinanceOverrides` / `EffectiveFinanceConfig`（及其[文件契约](../formats/finance-yaml.md)）开始；合并器语义与分量时间口径在装配与财务契约测试中核对。对应测试以财务模块测试为入口。
