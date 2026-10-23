# 计算生成器

> 文档状态：生效目标蓝图；目标代码边界：`backend/iesplan/computation/generators/`。

## 作用

生成器负责把通过校验的装配产物转换成某类求解器能够消费的计算输入文件和运行命令。每个生成器聚焦一种稳定的问题表达与求解器组合；系统通过多个 GeneratorProvider 并列扩展，而不是在一个函数中按算法名称堆叠分支。

生成器是“编译器”，不是“执行器”。它完成语义降级、单位换算和文件生成，但不运行命令。

## 边界

模块负责：

- GeneratorDescriptor、Provider protocol、能力查询和独立注册状态；
- 直接消费装配产物、设备数学贡献、物化序列、有效财务快照、计算配置及冻结评价确认，不设外层包装；
- 按规范设备方程贡献收集变量、约束、状态、接口流和索引；
- 按规划配置形成目标、变量边界和规划约束，并从 `EffectiveFinanceConfig` 取得经济参数；价格或成本不得写回设备技术语义；
- 将税务关系纳入同一次模型：预合并系数、建立当期关系与跨期余额约束，目标与结果评价使用同一规则。业务口径及公式以[财务计算](finance.md#税务参与规划与现金流)为准；
- 将规范业务单位显式转换为求解器内部单位；
- 生成求解器输入文件、结构化命令、输出声明和 ResultAdapter ID；
- 规范化 Bundle。

模块不负责数据库查询、对象下载、网络访问、provider 发现、任务状态、进程启动、重试、日志采集和业务结果发布。

用户算法插件把加工程序与算法程序放在同一个不可变包内时，系统注册的是通用 `SandboxedAlgorithmGeneratorProvider`，而不是每个用户包。该 provider 基于上述直接输入构造包内加工请求，交独立 runner 只执行包内加工入口，再把生成文件、结构化命令和输出声明封装成标准 Solver Bundle；算法入口仍由 SolverRuntime 通过通用 ExecutorProvider 执行。不得因二者同包而合并生成与执行边界，或允许加工程序读取项目数据库、网络和当前草稿。

## 输入

| 输入 | 进入条件 |
|---|---|
| `ValidatedAssemblyArtifact` | 已由装配模块签发，零阻断诊断；实例、参数覆盖、连接拓扑、规划、财务/计费绑定、项目基线 |
| `models` | 装配实例 ID 到设备数学贡献的映射（键为实例，不是设备类型） |
| `materialized` | 已物化的运营期输入序列集合及物化元数据（`MaterializedSet`；`constant`/`data_repeat`/`data_full`/`data_predict` 统一为 `MaterializedSeries` 只读序列） |
| `finance` | 不可变 `EffectiveFinanceConfig`（地区及项目财务规则、价格和成本参数来源；评价确认另行直传） |
| `CalculationConfig` | 在计算阶段选择，包含预测算法与参数、generator、solver、精度、求解选项、收敛条件、种子和输出请求 |
| `evaluation` | 财务模块公开的不可变评价确认，来自同一任务冻结块；年限、折现率、付款与维护字段见[公共契约](../contracts.md#快照与异步契约)，不混入其余输入 |
| GeneratorDescriptor | 支持装配 schema、所选计算能力和 solver，且与 `CalculationConfig` 一致；属 provider 静态事实，不作为每次生成参数 |

各项输入均来自可信上游，生成器不重复验证已签发的装配、设备贡献、财务快照、序列或评价确认，不做内容摘要。Generator 只接收已完成物化的集合（`pending_predict` 为空，由上游在预测产物合并后构成，此处信任类型化交接，不复核）；完整运营时域由装配基线起始年/分辨率与评价确认的 `operation_years` 推导，`series` 为空的项目仍得完整时域，不回退单年。付款计划与维护费用按财务时点消费，不放进物化集合，不建立外层输入包装。

### 税务提交能力

需量计费能力使用同一描述符机制：`finance.monthly_demand` 表示能构造真实自然月峰值、需量费用及其同次解事实。任务实际绑定需量费时必须同时具备该能力和 `finance.financial_facts`；没有需量绑定时不因目录内闲置费项拒绝。现有税务能力覆盖新增的显式需量费税务范围，不另建注册表；具体数学实例所需有限界仍由生成器构模报告，不复制到提交预检。

税务支持复用 `GeneratorDescriptor.capabilities`，不另建生成器白名单或注册表。`finance.vat`、`finance.construction_input_vat`、`finance.income_tax` 分别声明对应已定义税则种类，`finance.credit_pools` 声明抵扣池，`finance.financial_facts` 声明同次解财务事实输出。采用税则或抵扣池的任务还要求财务事实输出能力；空税则且空池不因缺少税务能力标记而拒绝。

任务提交用已解析的所选生成器描述符核对所需能力，缺失时返回 `TASK-REQ-007` 并标明缺失项，不静默按税前执行，也不按provider名称或版本字符串猜测支持。维护引用、评价期间及财务规则自身约束仍在原公开入口核对一次。类别支持不承诺每个数学实例均可降级；具体表达式、有限界和正残值等建模能力限制仍由生成器实际构模时报告，不在API生成计算包或复制模型预校验。

## 输出

成功输出 [Solver Bundle](../formats/solver-bundle.md)，包含：

- `bundle.yaml`；
- 一个或多个只读输入文件及媒体类型；
- 结构化 command；
- 允许写入的输出清单；
- 配套 ResultAdapter 的精确 ID 与版本；
- assembly、`CalculationConfig`、generator、solver 和依赖版本追溯信息。

失败输出结构化诊断，不发布临时目录或半份 manifest。

步序列的求解引用由生成器在建立实际变量时显式声明，形状以 [Bundle 步序列结果映射](../formats/solver-bundle.md#步序列结果映射) 为唯一正文。业务单位恢复和统一结果发布归 ResultAdapter；不得把生成器内部列命名规则交给 application 或前端反解。

## 内部开发分层

一个生成器建议按以下纯步骤组织：

```text
接收已签发输入
  ↓
方程展开：设备实例 → 规范数学贡献
  ↓
系统合并：变量/索引/平衡/目标/约束
  ↓
单位与数值规范化
  ↓
求解器格式 writer
  ↓
command/output manifest builder
  ↓
Bundle 规范化与原子发布
```

各步骤使用不可变中间 contract。求解器格式 writer 不应重新读取设备模型；command builder 不应重新解释装配业务。

## 项目级线性模型与 MPS writer

显式财务目标入口按[装配规划契约](../formats/assembly-yaml.md#规划表达式中的财务分量标识符)识别。生成器以同一组建设、运行及财务关系构建逐年现金流表达式，再按冻结折现率组合 NPV 目标，不先求解另一税前方案。现金流保留建设付款时点、年度固定运维、实际运行量对应年度的可变运维与能源收支及维护清单，不把全期合计均摊为年值；税额关系按[财务模块](finance.md#税务参与规划与现金流)接入。

`build_project_model` 把各项直接输入装配成项目级 `LinearModel`（变量声明、约束行、目标表达式与 min/max 方向）、单位映射与目标业务单位，不设包装层：按基线展开全部连续 `step`，实例与时步确定命名；技术关系经现有 lowerer 逐降（`t`/`t-1`、Big-M，有限界来自值域数值或实例额定引用）；公开原语 `step_duration` 以基线导出的 SI 秒注入；连接只定义连通性，按 carrier + 无向连通分量逐 step 生成一条母线守恒式（out/bidirectional 为正，in 为负），无边载体不建平衡式；负荷需求经可连接输入口与 serve 方程进入母线；规划目标/变量/约束与财务/计费分量按绑定展开；物理量归 SI、金额守币种；计费支持 constant 与 time_series（时变价格由第三项价格区逐点 `price[t]×e[t]` 计价，`constant` 同一逐点语义；未知价格类型明确拒绝）。`write_mps` 只序列化已完成的 `LinearModel`（稳定 ROWS/COLUMNS/RHS/BOUNDS/整数标记/OBJSENSE，目标常数以偏移量单独报告），同一模型逐字节相同。

## 首个内置 provider 与完整 Bundle 聚合

首个内置 provider 为 `ies.generator.highs_milp@4.0.0`（见“首个内置链”裁定目标）：mode 只要 `planning`、精度只要 `medium`、solver 只要 `ies.solver.highs@1.7.2`；options 只接受空对象；输出请求支持 `solution` 与 `metrics`（`metrics` 为 ResultAdapter 后处理请求，不对应求解器第二个原始文件，未知输出明确拒绝）；只消费装配实际绑定的 constant/time_series 计费价格（快照中未绑定的价格条目不触发拒绝），未知价格类型明确拒绝；`seed` 为 null 时使用固定种子 `0`，种子同时固定进求解命令与元数据，不读取随机全局状态。

`generate` 返回唯一完整聚合：manifest（即 `bundle.yaml` 内容）加按规范相对路径索引的只读输入文件字节映射（`input/problem.mps` 与 `input/parameters.json`）。`input/parameters.json` 为最小规范 JSON 元数据，只保存 ResultAdapter 后续必需的 `unit_map`、`objective_offset`、`objective_unit`（目标业务单位，按编译期根量纲确定：金额取财务币种，无量纲取 `"1"`，单一物理量纲取 SI 基准单位）、财务分量求值声明 `components`（分量 ident → `{unit, caliber, constant, terms}`，与目标同一分量展开路径，不依赖目标写法）、请求 outputs、固定 seed、precision 与装配/引用追溯，不复制完整装配、模型与财务，不写 hash。结构化命令按真实 HiGHS CLI 语法逐项存储（非 shell），`time_limit_seconds` 同时进入硬资源声明与求解器选项且语义一致；同一组直接输入与 provider 版本逐字节相同，不写临时目录，不启动 solver。

注册表注册真实 provider 实例：`ref` 与描述符引用单一来源；`resolve` 完成通用门禁后返回可调用 provider，快照与目录只公开描述符数据；任一候选不符即整体构建失败。组合根显式注册内置 provider，不动态扫描。

## GeneratorDescriptor

descriptor 至少声明：

- 稳定 generator ID 和版本；
- 支持的 assembly schema MAJOR/MINOR、计算 mode 和问题能力；
- 兼容 solver ID/版本及所需 executor 能力；
- 支持的方程 contract、AST 和数学贡献能力；
- options schema（完整 Draft 2020-12 对象，根描述 object 且 `additionalProperties` 显式 false；无选项用显式空对象 schema，不用空 `{}`）、默认语义和资源上界；
- 内部单位体系、确定性承诺和种子语义；
- 生成的媒体类型、ResultAdapter ID 和版本关系。

这些信息必须在生成 Solver Bundle 前可查询并完成兼容校验，不能等 Worker 启动求解后才发现不兼容。装配阶段不选择或锁定 generator 与 solver。生成前用 descriptor 的 options schema 校验计算配置选项：非法选项是用户配置错误，直接拒绝，不回退其他生成器，不删字段、不补默认；schema 内 `default` 只作描述。

## 设备方程如何参与

设备 descriptor 内的声明式方程是设备到数学贡献的公开边界。生成器消费 modeling 已解析、校验和规范化的贡献，并接收：

- 实例 ID、已校验 properties 和状态初值；
- 已由当前计算阶段按项目基线物化为连续 `step` 的预定义接口数据和连接接口；
- 存量/新增身份、规划配置以及不可变 `EffectiveFinanceConfig`；
- 由 generator 提供的能力受限 builder contract。

方程贡献提供声明式变量、技术约束、状态关系、接口流和结果映射元数据，不直接操作具体 solver 私有对象，不写文件，不运行进程。目标及规划边界来自规划配置，成本分量、O&M 与能源价格等共用经济参数来自 `EffectiveFinanceConfig`；预测算法、计算精度及求解选项由 `CalculationConfig` 决定。预测训练与序列生成由上游物化完成；生成器每次消费本轮完整运营期序列并构建技术／财务模型。耦合预测的反馈与重求解流程由 Worker 经 application 阶段命令编排。

## 单位与数值

装配阶段证明单位和量纲兼容，生成器执行唯一实际换算：

- 每个输入字段有来源业务单位、目标内部单位和换算记录；
- 禁止根据字段名猜单位；
- 同一 quantity 的内部单位由 GeneratorDescriptor 固定；
- 比例统一为 `0..1`；财务金额带币种和基准年语义；
- 非有限数、溢出和病态缩放在写求解器文件前失败；
- 结果映射元数据保留反向换算所需信息。

单位换算不得散落在设备方程解析和格式 writer 的常量中。

## 纯度与确定性

生成器必须满足：

- 不读取进程环境、当前时间、随机全局状态和机器 locale；
- 不访问网络、数据库、对象存储和用户目录；
- 不启动进程或动态下载 solver；
- 文件顺序、变量名、约束名、数字格式和归档元数据稳定；
- 随机性只能来自显式种子；
- 同一输入产生逐字节相同的规范 Bundle。

Bundle 以规范 manifest 与全部输入文件作为身份，不另放随机 ID 破坏确定性。attempt ID 属于执行回执，不参与 Bundle 内容。

## 增加一个 GeneratorProvider

1. 选择新的稳定 ID，列出 mode、solver、方程 contract 能力和输入媒体类型；
2. 定义 options schema、内部单位和 ResultAdapter；
3. 建立最小合法装配、边界装配和已知答案；
4. 先实现数学中间表示与确定性命名，再实现 solver writer；
5. 生成结构化 command，不接受用户 shell 片段；
6. 完成 manifest 和原子发布；
7. 用 fake runtime 验证 Bundle contract，用真实 solver 容器做独立集成验证；
8. 注册 provider 并验证缺依赖、重复 ID 和半失败不会发布。

## 首个内置链（已裁定目标）

首个内置 provider 为 `ies.generator.highs_milp@4.0.0`，兼容求解器 `ies.solver.highs@1.7.2`，结构化命令执行器前向引用 `ies.executor.local_process@1.0.0`，结果适配器前向引用 `ies.result-adapter.highs@4.0.0`。技术模型支持可证明的仿射关系，以及有限非负上下界下 `x*y=0` 的确定性二进制 Big-M 转换；设备状态仅允许 `t` 与 `t-1`；公开规划变量为连续变量。生成器可为受支持的互斥关系及[财务税务关系](finance.md#求解能力边界)引入必要的内部离散变量，所需界由业务参数和变量边界推导。电池“规划容量参与 SOC 分母”一类双线性明确不支持。

生成器 `3.0.0` 将独立评价确认列为必需生成输入，不保留旧调用签名或默认评价值。通用 Bundle manifest 未变；结果适配协议按实际输出变化独立版本化，不因生成器输入变化而同步升版。

新增显式全周期 NPV 目标的能力版本为 `ies.generator.highs_milp@3.1.0`；直接输入形状不变，既有目标语义不变。该目标的数学定义不表示尚未实现的税额关系已获支持；未支持关系仍明确拒绝。仅增加目标能力不改变通用 Bundle 或结果适配协议。

增值税关系的能力版本为 `ies.generator.highs_milp@3.2.0`：按财务文件声明构建经营进销项、建设进项及共享抵扣池的连续月度状态，将实缴与退款到账纳入同次 NPV 目标；进项确认、财务年度映射及终期余额以财务契约为准。金额系数预合并，余额和分段税额由精确约束确定，不依赖目标函数压紧。该能力不包含所得税，不改变通用 Bundle 或结果适配协议；业务税务输出由配套结果协议另行声明。

所得税关系的目标能力版本为 `ies.generator.highs_milp@3.3.0`：依财务配置将计税基础、直线折旧及先到期先用的亏损结转纳入同次求解，所得税按对应财务年度进入现金流。金额与期间语义以财务契约为准；折旧与亏损使用不作为现金支出重复扣除，税额与抵扣顺序不依赖规划目标压紧。直接输入与通用 Bundle 协议不变，税务结果发布按实际输出另行版本化。

同次解财务事实的目标能力版本为 `ies.generator.highs_milp@4.0.0`，配套 `ies.result-adapter.highs@4.0.0`。生成器声明与模型同源的实际现金及税务事实，使适配器恢复完整年度现金流；已有财务分量和求解序列保持原有用途，不以新增通用分量口径或解析命名替代结构化财务映射。字段以[财务事实映射](../formats/solver-bundle.md#同次解财务事实映射)为准。

## 失败语义

| 问题 | 结果 |
|---|---|
| 非法装配输入 | 装配/application 边界阻断，不进入生成 |
| 必需直接输入不完整 | 调用方输入不完整，不生成 Bundle |
| 方程 contract 或 solver 能力不匹配 | 兼容诊断，执行前阻断 |
| 超出已声明精确降级能力的非仿射关系（变量×变量、变量作除数、非常数幂等） | 明确拒绝，不近似、不回退 |
| 互斥项非负不可证或缺有限上界 | 明确拒绝，不猜测 Big-M |
| `[t-k]`（k>1）时间引用 | 设备发布输入边界拒绝 |
| 单位无法换算或出现非有限数 | 数值输入诊断，定位字段/实例 |
| writer 中途失败 | 临时产物废弃，不发布 Bundle |
| 相同输入不稳定 | 确定性契约失败，provider 不 ready |

禁止捕获异常后改用另一生成器、删去约束、零填数据或生成“尽力而为”Bundle。

## 必须遵循的规范

- 只消费装配、规范方程贡献和 core 的公开 contract；
- 一个 provider 拥有自己的注册候选，组合根原子发布；
- 所有相对路径由 Bundle builder 生成并校验；
- command 只能引用受信任 executor/executable ID；
- `CalculationConfig` 的 generator 版本、options、seed、依赖锁和输出进入任务证据；
- 日志不输出完整业务数据、凭证或宿主机路径。

## 完成标准

- 不启动 solver 即可完成全部单元与契约测试；
- 最小装配的变量、约束、单位和输入文件可人工复核；
- 相同输入逐字节可重复；
- 非法装配、资源、能力和数值均在生成边界阻断；
- 通用 SolverRuntime 不包含该 generator 的专用分支。

代码阅读从 GeneratorDescriptor/Protocol 开始，再看规范中间表示、单位转换、具体 writer 和 Bundle builder；测试按“纯生成 → Bundle contract → 容器内 solver 集成”分层。
