# 计算生成器

> 文档状态：生效目标蓝图；目标代码边界：`backend/iesplan/computation/generators/`。

## 作用

生成器负责把通过校验的装配产物转换成某类求解器能够消费的计算输入文件和运行命令。每个生成器聚焦一种稳定的问题表达与求解器组合；系统通过多个 GeneratorProvider 并列扩展，而不是在一个函数中按算法名称堆叠分支。

生成器是“编译器”，不是“执行器”。它完成语义降级、单位换算和文件生成，但不运行命令。

## 边界

模块负责：

- GeneratorDescriptor、Provider protocol、能力查询和独立注册状态；
- 核验 `ValidatedAssemblyArtifact`、依赖锁和资源摘要；
- 按规范设备方程贡献收集变量、约束、状态、接口流和索引；
- 按规划配置形成目标、变量边界和规划约束，并从公共财务配置取得经济参数；价格或成本不得写回设备技术语义；
- 将规范业务单位显式转换为求解器内部单位；
- 生成求解器输入文件、结构化命令、输出声明和 ResultAdapter ID；
- 规范化 Bundle 并计算内容摘要。

模块不负责数据库查询、对象下载、网络访问、provider 发现、任务状态、进程启动、重试、日志采集和业务结果发布。

用户算法插件把加工程序与算法程序放在同一个不可变包内时，系统注册的是通用 `SandboxedAlgorithmGeneratorProvider`，而不是每个用户包。该 provider 把规范装配、固定资源和包摘要交给独立 runner，只执行包内加工入口，再把生成文件、结构化命令和输出声明封装成标准 Solver Bundle；算法入口仍由 SolverRuntime 通过通用 ExecutorProvider 执行。不得因二者同包而合并生成与执行边界，或允许加工程序读取项目数据库、网络和当前草稿。

## 输入

| 输入 | 进入条件 |
|---|---|
| `ValidatedAssemblyArtifact` | schema、摘要、回执一致，零阻断诊断 |
| 资源映射 | 每项由内容 ID 映射到只读字节，摘要与回执一致 |
| 方程贡献与设备内容锁 | 方程 contract、设备内容摘要和装配依赖锁一致 |
| `CalculationConfig` | 在生成 Solver Bundle 时选择，包含 generator、solver、精度、算法选项、种子和输出请求 |
| GeneratorDescriptor | 支持装配 schema、所选计算能力和 solver，且与 `CalculationConfig` 一致 |

资源必须由调用方显式传入。生成器看到的是只读内容和逻辑 ID，不是 BlobStore、URL 或宿主机路径。

## 输出

成功输出 [Solver Bundle](../formats/solver-bundle.md)，包含：

- `bundle.yaml` 及其规范摘要；
- 一个或多个只读输入文件及媒体类型、摘要；
- 结构化 command；
- 允许写入的输出清单；
- 配套 ResultAdapter 的精确 ID 与版本；
- assembly、`CalculationConfig`、generator、solver 和依赖版本追溯信息。

失败输出结构化诊断，不发布临时目录、半份 manifest 或缺摘要文件。

## 内部开发分层

一个生成器建议按以下纯步骤组织：

```text
验证入口
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
Bundle 规范化、摘要、原子发布
```

各步骤使用不可变中间 contract。求解器格式 writer 不应重新读取设备模型；command builder 不应重新解释装配业务。

## GeneratorDescriptor

descriptor 至少声明：

- 稳定 generator ID 和版本；
- 支持的 assembly schema MAJOR/MINOR、计算 mode 和问题能力；
- 兼容 solver ID/版本及所需 executor 能力；
- 支持的方程 contract、AST 和数学贡献能力；
- options schema、默认语义和资源上界；
- 内部单位体系、确定性承诺和种子语义；
- 生成的媒体类型、ResultAdapter ID 和版本关系。

这些信息必须在生成 Solver Bundle 前可查询并完成兼容校验，不能等 Worker 启动求解后才发现不兼容。装配阶段不选择或锁定 generator 与 solver。

## 设备方程如何参与

设备 descriptor 内的声明式方程是设备到数学贡献的公开边界。生成器消费 modeling 已解析、校验和规范化的贡献，并接收：

- 实例 ID、已校验 properties 和状态初值；
- 已按项目计算基线规范为连续 `step` 的预定义接口数据和连接接口；
- 存量/新增身份、规划配置以及独立的公共财务配置；
- 由 generator 提供的能力受限 builder contract。

方程贡献提供声明式变量、技术约束、状态关系、接口流和结果映射元数据，不直接操作具体 solver 私有对象，不写文件，不运行进程。目标及规划边界来自规划配置，投资、运维、能源价格、税率和资金时间成本等共用经济参数来自公共财务配置；计算精度及算法选项由 `CalculationConfig` 决定。

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

Bundle 以规范 manifest 与全部输入文件的内容摘要作为身份，不另放随机 ID 破坏确定性。attempt ID 属于执行回执，不参与 Bundle 内容。

## 增加一个 GeneratorProvider

1. 选择新的稳定 ID，列出 mode、solver、方程 contract 能力和输入媒体类型；
2. 定义 options schema、内部单位和 ResultAdapter；
3. 建立最小合法装配、边界装配和已知答案；
4. 先实现数学中间表示与确定性命名，再实现 solver writer；
5. 生成结构化 command，不接受用户 shell 片段；
6. 完成 manifest、输入摘要和原子发布；
7. 用 fake runtime 验证 Bundle contract，用真实 solver 容器做独立集成验证；
8. 注册 provider 并验证缺依赖、重复 ID 和半失败不会发布。

## 失败语义

| 问题 | 结果 |
|---|---|
| 原始或过期装配 YAML | 入口拒绝，要求新的校验产物 |
| 资源摘要不符 | 完整性失败，不生成输入 |
| 方程 contract 或 solver 能力不匹配 | 兼容诊断，执行前阻断 |
| 单位无法换算或出现非有限数 | 数值输入诊断，定位字段/实例 |
| writer 中途失败 | 临时产物废弃，不发布 Bundle |
| 相同输入摘要不稳定 | 确定性契约失败，provider 不 ready |

禁止捕获异常后改用另一生成器、删去约束、零填数据或生成“尽力而为”Bundle。

## 必须遵循的规范

- 只消费装配、规范方程贡献和 core 的公开 contract；
- 一个 provider 拥有自己的注册候选，组合根原子发布；
- 所有相对路径由 Bundle builder 生成并校验；
- command 只能引用受信任 executor/executable ID；
- `CalculationConfig` 摘要、generator 版本、options、seed、依赖锁和输出摘要进入任务证据；
- 日志不输出完整业务数据、凭证或宿主机路径。

## 完成标准

- 不启动 solver 即可完成全部单元与契约测试；
- 最小装配的变量、约束、单位和输入文件可人工复核；
- 相同输入逐字节可重复，变化可由摘要定位；
- 非法装配、资源、能力和数值均在生成边界阻断；
- 通用 SolverRuntime 不包含该 generator 的专用分支。

代码阅读从 GeneratorDescriptor/Protocol 开始，再看规范中间表示、单位转换、具体 writer 和 Bundle builder；测试按“纯生成 → Bundle contract → 容器内 solver 集成”分层。
