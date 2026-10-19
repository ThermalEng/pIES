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
| `EffectiveFinanceConfig` | 唯一由确定性合并器从 Profile（+ Overrides）生成并完整校验的不可变快照；可导出/导入/进入快照，项目包导入在外部上传边界连同精确来源完成合并验证，确认阶段不重复执行；装配、规划与计算只消费同一引用（见[装配 YAML](../formats/assembly-yaml.md)） |
| 建设与替换计划 | 金额币种、发生期、`price_basis`（含税/不含税口径标签）和 `base_year` 明确 |
| 逐时或年度运行汇总 | 能量、成本、收益的单位和时间范围明确 |
| 规划与财务基准 | 与当前方案使用同一 `EffectiveFinanceConfig` |

金额与需要精确往返的费率使用十进制定点语义（`Decimal` 字符串，`{value, unit}` 原子，禁止 `null` 与部分金额）；物理量来自规范计算输出。模块不能自行读取“最新地区价格”补齐缺失输入，也不运行期继承 Profile 或覆盖。

### 成本语义与增量规则

- 成本函数为 `cost_method: fixed_plus_linear`：固定建设成本加多个独立线性分量，按明确 `driver` 计量；不做二次、分段、非线性或采购汇总。成本金额非负（允许 `0`，零费用类别合法）；
- 运维费用按业务分为固定运维费与使用运维费，不新增字段或费用载体。
  - 固定运维费包括常年固定运维 `annual_fixed_om`（按装机容量与年度单价计取，投产后逐年扣减，不使用费率模型 `fixed_om_rate`）和特定年份大修／换新 `maintenance_expenses`（显式指定发生年份并在该年扣减）。
  - 使用运维费即 `period_variable_om`：按实际运行量／发电量（如 kWh、Nm³）与明确单价计取，保留接口时序 driver，由同一次全周期仿真调度的对应年度实际运行量直接归集，天然具有年份，不按全期金额均摊。
- 不接受无年份、无时间维度的标量全期运维费用。按容量等存量每年收取的费用声明为 `annual_fixed_om`；一次性维护／换新使用评价确认中的显式发生年清单（公开字段 `maintenance_expenses`）。不得把标量 property 与无时间维度费率的乘积放入 `period_variable_om`，也不得自动指定发生年或均摊到运营年；该规则在装配公开输入边界阻断，不在求解后重复校验。
- `new` 计算建设期一次性与新增固定 O&M；`existing` 不计沉没历史建设成本与决策无关的固定 O&M；所有设备仍计算决策相关能源购售与可变 O&M，不做基准方案双重求解。
- 时间口径组合：`annual_fixed_om` 为按年值（currency/年），`period_*` 为窗口合计（currency）；两者相加前必须经显式时间跨度换算（示例：窗口为项目基线完整一年时，按年值 × `1 year`），窗口不是完整一年时不得默认相加。把一次性 `upfront_capex` 转换为可与运行期成本比较的显式规划分量属于规划配置契约的版本化规则，本模块与 Profile 不隐式定义或默认选择该转换（不做隐式年化，不隐含资本回收系数、利率或年限等值）。

### 能源价格

`energy_prices` 是 `price_id → 价格条目` 的开放映射（`price_id` 为稳定自定义名，`lower_snake_case`、不含 `__`；不存在固定白名单价格键，封闭的载体清单只在公共载体词汇中维护——见[设备模型 YAML](../formats/device-model-yaml.md)「interfaces」的 `carrier` 规则，本页不复制）。每个条目显式携带 `carrier`（来自公共载体词汇，与设备接口同一词汇源）与 `direction: purchase|sale`（会计方向，不靠 price_id/键名推断）；定价定义为 `constant {value, unit}` 或引用经校验、覆盖全期的连续序列的 `time_series`。价格为有限 Decimal，正、零、负均合法，禁止 NaN/Infinity。每个计量绑定产出的计费分量是**已带符号的记账贡献**：先算 `raw_charge = Σ price[t]×e[t]`，`purchase` 分量 = +raw_charge（正价时为正，成本向）、`sale` 分量 = −raw_charge（正售电价时为负，收益以负值自动抵减成本）；负价自然按该方向反转（负购电价使 purchase 分量为负、负售电价使 sale 分量为正），零电价合法；规划表达式对已签名分量统一使用加法，不再书写额外负号。`time_series` 引用携带 `resolution`/`point_count`/`unit` 等不可变元数据（无 `leap_year`），全期覆盖在任务提交边界核对，不一致阻断且不重采样（Profile 不固定唯一项目分辨率）。装配 `tariff_bindings` 以 `binding_id → {price, instance, interface, aggregation}` 把每个用到的 `price_id` 绑定到方向明确的计费点实例接口与聚合，校验价格 `carrier` 与接口载体相容、`direction` 与接口流向一致；同一价格可被多个绑定复用。时变价格按逐点计价：聚合先得到与价格同轴的能量序列 `e[t]`，再按 `Σ price[t]×e[t]` 计费（`constant` 每点同价，结果与 `price×Σe[t]` 一致但语义仍为逐点）；禁止先对时变价格总量聚合再与总能量相乘（聚合与计价衔接见[财务 YAML 契约](../formats/finance-yaml.md)）。

## 长期蓝图：`FinancialResult`

`FinancialResult` 蓝图至少表达：

- 逐期项目和资本金现金流；
- 投资、运行成本、收益和残值等组成；
- NPV、IRR、LCOE、回收期及单位；
- 每个无法正常计算指标的独立状态与原因；
- `EffectiveFinanceConfig`（含币种、`price_basis`、`base_year`）与计算版本。

IRR 必须与 `normal、no_solution、multiple、degenerate、out_of_domain、numeric_failure` 等状态一起消费；不能用 `null` 或零模糊这些情况。该蓝图需要的评价期、资金时间价值、折旧等输入不属于本版 Profile 文件契约；落地时按「增加财务指标」流程补入独立契约，不回流为财务文件契约的隐式字段。

### 项目投资口径评价规则

项目投资口径评价以覆盖建设和运营的完整现金流 `CF[0..evaluation_years]` 与用户显式确认的评价参数为输入；`t=0` 为开工时点，后续时点按整数年排列，序列长度为 `evaluation_years + 1`。不由现金流或运行序列长度反推评价年限，不把单年结果复制为多年。逐年明细行含原始净流、贴现流（`CF[t]/(1+r)^t`）与两种累计，NPV、IRR、静态/动态回收期与明细同源复算。动态回收期对贴现流沿用静态回收期同一规则（累计首次非负、年内线性插值），未回收为无值并附明确状态，`r=0` 时与静态回收期一致。IRR 状态采用本页公开语义（`normal/no_solution/multiple/degenerate/out_of_domain/numeric_failure`）。

### 封存评价块 `financial_evaluation`

Worker 只对最终轮已求解结果计算一次评价，随结果载荷同一证据单元封存（`status` 为 `"evaluated"`）；未求解或缺解输出不写财务块。读取与导出只消费该封存事实，不重算、不读最新配置。金额均为十进制字符串（不舍入），单位独立字段：

- `evaluation_years`／`construction_period`／`operation_years`：整数（K/N 回显冻结输入，供时轴映射展示）；
- `currency`、`price_basis`、`base_year`、`profile_id`、`discount_rate`：冻结值原样；
- `construction_total` + `construction_total_unit`、`annual_fixed_om`：十进制字符串；
- `payments_applied`：`[{t, ratio, amount}]` 数组；`t` 为整数财务时点，`ratio`／`amount` 为十进制字符串；同一 `t` 可多笔，不设时点唯一；
- `annual_operation`：`[{financial_year, energy_net, variable_om, fixed_om, maintenance, net}]` 数组；金额十进制字符串，`maintenance` 为该年逐项维护合计；
- `evaluation`：逐年明细与指标：`rows[{year, net, discounted, cumulative, cumulative_discounted}]`（金额字符串）；`npv`（字符串）+ `npv_unit`；`irr`（数或 null）+ `irr_status`（本页 IRR 状态枚举）；`static/dynamic_payback`（年数或 null）+ `_unit`（有值为 `"year"`）+ `_status`（`recovered`／`not_recovered`）。

### 评价确认与快照冻结

评价年限与折现率是财务输入的显式评价确认，不属于 `FinanceProfile`/`Overrides`/`Effective`（本页已明令不回流），不属于封闭 `CalculationConfig`，不设默认值（沿用 `0.08` 或由序列长度推导均属阻断行为）：

确认字段（均无业务默认值）：
- 时间关系：`construction_period` 为非负整数年，`operation_years` 为正整数运营年限，`evaluation_years = construction_period + operation_years`，包含建设期，不能再作为运行序列的年限。明确确认这些时间参数及折现率，不以缺省建设期为零代替用户确认；
- `discount_rate` 为无量纲比例（API 规范值 `0..1`，无默认，`r=0` 合法）；确认同时包含明确的建设付款比例计划和维护／换新支出清单，公开字段见[公共契约](../contracts.md)；`confirmed_by`/`confirmed_at` 由服务端记录（追加审计不可覆盖）；
- 运行起始年归项目计算基线（创建时固定），与 `base_year`（金额口径基年）不得互相推断；
- 自由字典式 `baseline-confirm` 的审计记录不能充当已确认的计算参数：提交时须有关联到本次计算的显式确认记录，无确认即阻断提交；
- 以上确认随提交冻结进 `CalcSnapshot`（破坏性修改提升快照主版本）；运行年度边界 step 下标只由基线运行起始年、分辨率与 `operation_years` 推导并随快照冻结。参数关系与输入范围在公开提交边界校验，内部快照读取与交接不重复校核。

### 项目建设安排与全周期现金流

本计算课题在项目首年启动建设，建设期与建设费用支付安排由项目明确，不将何年启动建设或中期新增容量作为求解变量。首年启动不等于首年年初立即建成投产，也不等于投资一次付清。建设选择与容量仍由规划求解决定：选建实例收取一次固定建设费，并按选定容量计算容量建设费；未选建实例不收固定建设费。总建设费用与付款时点分开处理，不因费用固定或随容量变化就推断支付年份，不自动均摊。中期扩建另建计算课题。

令 `K = construction_period`、`N = operation_years`。项目在 `t=0` 开工，`t=K` 建设期末投产，第 `j` 个完整运营年末位于 `t=K+j`（`j=1..N`）；评价期为 `K+N` 年。运行仿真只覆盖投产后的连续 `N` 年，不生成建设阶段的虚假运行 step。基线 `start_year` 是运行起始公历年，而不是开工年；运行年界仍按实际公历推导。

- 付款计划是用户明确给出的比例计划，总和精确为 `1.0`；求解前不要求确定选建方案的总金额。求解得到建设总额 `C` 后，按各时点比例计入 `−ratio_t × C`，可在建设期末 `t=K` 付款；不一律强放 `t=0`，不默认均摊；
- 第 `j` 年运行净额来自同一次全期解的对应运营年切片，计入 `CF[K+j]`；无运行的建设阶段不能复制运行期收益，投产时点 `t=K` 没有前序完整运营年收益；
- 各年运行净额＝能源净收益 − 可变 O&M − 固定 O&M − 当年未包含在前述 O&M 中的维护／换新支出。能源及可变 O&M 按该年真实运行结果归集，固定 O&M 按本课题选定容量及明确费率计算；不能以平均年费替代逐年实际支出。

手算时轴样例：建设 `1` 年、运营 `2` 年、评价 `3` 年，付款计划为 `[{t: 0, ratio: 0.6}, {t: 1, ratio: 0.4}]`。开工 `t=0` 付 `0.6C`，投产 `t=1` 付 `0.4C` 且无前序运行收益，`t=2`、`t=3` 分别计入第 1、2 个完整运营年的净收益 `R1`、`R2`。因此 `CF = [−0.6C, −0.4C, R1, R2]`，长度 `4 = evaluation_years + 1`，运行序列仅覆盖两年。

零建设期样例（仅本例用户明确 `construction_period=0`、`operation_years=2`、`evaluation_years=2`，全部费用 `t=0` 支付，不是默认）：选建 50 kW；固定 900000 CNY、容量建设费 5000 CNY/kW、固定 O&M 100 CNY/kW/a；扣除可变 O&M 后能源净收益为 80000、100000 CNY，无额外换新支出。`CF = [−1150000, 75000, 95000]`。

### 运行期换新与指标口径

维持既有运行能力的维护、部件更换与电池换新支出，按汇报口径归入运行期维护费用，在实际发生年份扣减年末运行净额，不回填初始建设投资；已包含在 O&M 分量中的支出不得重复扣减。新增或扩建设备容量属于另一个建设计算课题，不能仅因发生在运行期就归为本课题维护。维护费用的报告分类不等于将一次性换新支出写入 `annual_fixed_om` 年费率；用户在评价确认中逐项显式给出发生年、金额与币种（见公共契约），不能猜测换新周期或费用，明确无额外支出时给空清单。

以初始建设投资为分母的简单回报率，分母不因运行期换新而扩大，收益仍须扣除维护与换新支出，并明确评价期间及计算定义；NPV、IRR、静态／动态回收期必须使用包含全部建设投入和运行期维护／换新支出的逐年现金流，不能只计算初始投入与毛收益。

## 蓝图设计思路

1. 把输入规范成时间点明确的现金流分量；
2. 再组合项目现金流和资本金现金流；
3. 对每个指标独立计算并分类失败状态；
4. 在公开输入边界确认必要的时间、金额与币种约束；内部计算、快照读取与阶段交接信任已确认的事实，不重复校验分量之和、长度或币种；
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
- 被引用的 `time_series` 未通过校验、未覆盖全期、元数据（resolution/point_count）与引用声明或全周期推导值不一致（任务提交边界核对）：阻断，不重采样；
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
- 财务模型实际消费的每项参数必须在求解前有明确权威来源与值，缺失即提交检查阻断；绝不默认、外推或事后反推（建设期及支付安排为明确项目输入，建设选择与容量仍为规划决策，不为参数确定而预先固定选建方案与容量）；
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
