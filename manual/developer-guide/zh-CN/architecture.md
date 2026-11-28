# 系统架构蓝图

> 文档状态：生效蓝图；规范版本：1.3.0；上位规范：[架构宪法](ARCHITECTURE_CONSTITUTION.md)

## 系统概述

pIES 将设备技术模型、拓扑连接、时序数据、财务规则与规划目标转化为可校验、可求解、可追溯的工程规划结果。架构设计围绕两个核心要求展开：**业务语义由后端与声明式文档统一拥有**，以及**设备、方程、生成器、执行器和存储之间通过稳定边界解耦**。

### 模型文档与后端为中心

设备技术定义、系统装配、财务配置、规划目标和计算配置均采用独立的声明式文档表达，由后端统一解析、校验并构建数学问题。前端作为操作入口，其结构化表单只是文档的便捷编辑视图，不决定系统能表达哪些关系；对于表单未展开的高级约束，用户可直接通过完整文档编辑保留和提交。

新增工程建模需求时，应优先通过声明式设备方程、系统方程和约束表达，在通用链路存在缺口时扩展公共能力，避免按特定设备或案例名称在核心流程中硬编码分支。

### 领域语义与数学表达

领域层负责维护设备、连接、费用、资产、支付与税务的业务含义；计算生成层负责将其等价转换为具体求解器的数学模型；结果适配层再把求解输出还原为带业务单位和身份的结果。

| 语义主题 | 权威正文 |
|---|---|
| 端口连接、设备内部边界与封闭端口语义 | [端口级网络语义](modules/assembly.md#端口级网络语义) |
| 模型有效域、项目边界与派生界分析 | [统一上下界](modules/generators.md#统一上下界) |
| 建设单元激活与规模变量约束 | [建设激活目标语义](formats/assembly-yaml.md#建设激活目标语义) |
| 命名金额定义、收付发生与引用身份 | [通用金额引用目标语义](formats/finance-yaml.md#通用金额引用目标语义) |
| 评价范围、纳税边界与冻结基准 | [评价边界与基准](modules/finance.md#评价边界与基准) |
| 运行步、结算期、财务年度与等价优化 | [时间结构与等价优化](modules/generators.md#时间结构与等价优化) |

2.0 正式发布前，自有文件契约标记统一固定为 `1.0.0`，变更时同步更新生产者、消费者和样例；已冻结的历史快照与结果证据保持原貌，不用当前草稿改写历史。

### 规模与可解性边界

- **装配有效与可求解分离**：装配校验确认输入完整、引用合法且量纲兼容；具体模型能否转换为某类求解器输入、问题是否可行或有界，由生成器能力声明、转换诊断和求解器状态分别说明。
- **可控的等价优化**：性能优化应优先定位生成与 I/O 热点，在严格保持原数学约束与目标等价的前提下实施（例如稀疏矩阵复用、结构模板化），不擅自改变问题时域或业务语义。

## 系统上下文与参与者

```text
规划工程师 / 管理员
        │ 浏览器 GUI
        ↓
      Web 前端 ───────→ 静态帮助中心（由 manual/ 构建期生成）
        │ HTTPS / JSON / 文件
        ↓
      API 适配层
        ↓
     应用用例层 ──────→ PostgreSQL（权威事务事实）
        │       └─────→ 对象存储（不可变大对象）
        ├──→ 领域模块
        └──→ 任务与冻结快照 ──→ 内嵌调度层（backend 控制面）
                                      └──→ application.worker 阶段命令
                                                ├──→ GeneratorProvider ──→ Solver Bundle
                                                ├──→ SolverRuntime ──→ ResultAdapter
                                                └──→ analysis / 任务处理 / 证据与结果
```

- **Web 前端**：管理展示状态与未提交表单，不持有权威业务状态。
- **API 适配层**：负责路由、认证、DTO 校验与用例调用。
- **应用用例层**：协调跨模块业务动作并控制数据库事务边界。
- **领域模块**：各自拥有独立的业务规则、内部持久化与公开门面。
- **内嵌调度层**：消费不可变计算快照，通过 `application.worker` 的短事务阶段命令与 `job_runner` 容器代理推进长时后台任务。

## 端到端业务流

```text
设备技术定义 → 方程解析与装配检查 ─┐
全期序列、财务规则与参数 ──────────┼→ 计算生成 → 受控求解 → 结果适配
独立计算配置 ────────────────────┘                          ↓
                                               同次解的财务评价与结果分析
```

| 阶段 | 输入 | 输出 | 向下游提供的保证 |
|---|---|---|---|
| 设备定义 | 设备 YAML | `DeviceDescriptor` | `properties/interfaces/equations` 与单位合法，发布内容身份固定 |
| 方程解析 | 设备描述符与受限方程 | 公共 AST 与数学贡献 | 变量、关系、单位和接口引用明确 |
| 装配与检查 | 装配 YAML/项目图、基线、数据绑定、规划配置、`EffectiveFinanceConfig` | 诊断或 `ValidatedAssemblyArtifact` | 连接拓扑、数据来源、财务绑定和规划输入完整合法 |
| 序列物化 | 规范装配、项目基线与计算配置 | 全周期连续 `MaterializedSet` | `constant`/`data_repeat`/`data_full`/`data_predict` 对齐到同一全周期 `step` |
| 计算生成 | 装配产物、设备贡献、物化序列、有效财务快照、`CalculationConfig`、评价确认 | Solver Bundle | 能力匹配，求解输入文件、结构化命令、输出与适配器声明完整 |
| 受控求解 | Solver Bundle、资源与取消上下文 | `ExecutionReceipt` 与原始输出 | 命令在隔离环境执行，退出状态、资源统计和输出完整 |
| 结果适配 | Bundle、执行回执、原始输出 | `ComputeResult` | 数学状态、业务单位、设备与系统序列、财务事实及版本锁完整 |
| 财务与分析 | 计算结果、冻结财务与评价参数、分析命令 | 现金流评价、评估与分析结果 | 指标口径、状态及证据溯源链完整 |

## 三条核心业务链

1. **编辑链（草稿演进）**：`表单/画布 → 前端 mapper → HTTP DTO → application 用例 → 领域校验 → 新草稿 revision`（单主最后写入生效）。
2. **计算链（快照冻结与求解）**：`提交命令 → 授权与装配 → 固化 CalcSnapshot → 创建 Task → Worker 领取 Attempt → 序列物化 → 生成 Bundle → 隔离求解 → 结果适配（耦合预测时按需迭代）→ 财务评价 → 提交 Evidence`。任务创建后只消费冻结快照，与后续草稿编辑隔离。
3. **追溯链（历史解释）**：`结果视图 → Evidence → Attempt → Task → CalcSnapshot → 项目/数据/配置/Provider 版本与对象引用`。任何展示、评估、方案应用与导出均沿此链读取冻结事实。

## 模块职责与依赖方向

| 模块 | 核心职责 | 不负责的内容 |
|---|---|---|
| [core](modules/core.md) | 无状态基础类型、诊断、单位、时间轴与规范 JSON | 业务默认值、Provider 注册、持久化 |
| [devices](modules/devices.md) | 设备身份、技术属性、接口声明、方程文本及目录发布 | 项目实例、经济成本、求解精度 |
| [modeling](modules/modeling.md) | 受限技术方程解析、语义校验与公共数学贡献 | 设备目录路径、价格目标、求解器执行 |
| [assembly](modules/assembly.md) | 接口网络装配、四阶段校验与 `ValidatedAssemblyArtifact` 签发 | 求解器文件、命令与算法选择 |
| [generators](modules/generators.md) | 数学降级、单位换算与 Solver Bundle 生成 | 进程启动、数据库访问、任务状态 |
| [solver runtime](modules/solver-runtime.md) | 受控进程/容器执行、资源隔离、取消与 `ExecutionReceipt` | 设备语义、问题构建、结果解释 |
| [computation](modules/engines.md) | 计算阶段公共契约、预测调度、结果适配与 `ComputeResult` | HTTP 会话、ORM 与草稿管理 |
| [finance](modules/finance.md) | 财务三件套合并、规划财务关系与项目投资现金流评价 | 规划求解调度、界面状态 |
| [analysis](modules/analysis.md) | 敏感性扫描、批量比较与四维系统评估 | HTTP 传输、阶段调度 |
| [storage](modules/storage.md) | 不可变对象读写、Owner 引用生命周期与孤儿清理 | 理解项目版本或数据集业务含义 |
| [application](modules/application.md) | 权限控制、数据库事务与跨模块用例编排 | 穿透领域模块私有实现 |
| [api](modules/api.md) | HTTP 路由、认证上下文、DTO 序列化与错误映射 | ORM 查询、领域规则计算 |
| [worker](modules/worker.md) | 队列领取、临时容器执行编排与阶段检查点恢复 | 解释装配语义、拼接 Shell 命令 |

依赖方向严格单向：

```text
外部请求 → api → application 用例 → 领域公开门面
任务调度 → worker 调度层 → application.worker 阶段命令 → 领域/计算公开门面
api / worker / application / 领域模块 → core
```

各业务模块之间只通过 `__init__.py`、`public.py` 或公开契约类型交互，禁止跨模块导入私有符号、Repository 或 ORM 模型。具体 Provider 的选择与依赖注入统一由组合根（`bootstrap`）在进程启动时完成。

## 前后端边界

```text
GUI 表单状态 → 前端 mapper（文本、百分比、展示单位）→ 公开 HTTP DTO
  → 后端（认证、权限、结构、量纲、领域约束）→ 应用用例 → 领域模块
```

前端预校验仅用于提供即时交互反馈，后端始终是业务正确性与权限的唯一权威。

## 跨模块功能设计落点与开发顺序

新增能力应按职责归属落位，不以“哪个文件方便”为准：基础无关值规则进 `core`，设备技术描述进 `devices`，方程语法进 `modeling`，装配与网络规则进 `assembly`，求解转换进 `generators`，隔离执行进 `solver runtime`，输出映射进 `result adapter`，经济口径与评估分析分别进 `finance` 和 `analysis`，跨模块流程由 `application` 编排并适配至 `api`/前端。

以新增一种可规划设备为例，推荐按以下顺序推进并独立验证：

1. 在 `devices` 定义包含 `properties`、`interfaces` 和 `equations` 的设备模型 YAML；
2. 确认 `modeling` 可解析其方程并生成无求解器依赖的数学贡献；
3. 验证 `assembly` 能完成实例绑定、端口连接与规划/财务配置检查；
4. 确认现有 `GeneratorProvider`（或新增独立 Generator）能降级并生成 Bundle；
5. 验证 `SolverRuntime` 与 `ResultAdapter` 能正常执行并还原业务单位；
6. 在 `application`、`api` 与前端完成端到端接入与 Docker Playwright 浏览器验收。
