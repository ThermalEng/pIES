# 系统架构蓝图

> 文档状态：生效蓝图；规范版本：1.0.0；上位规范：[架构宪法](ARCHITECTURE_CONSTITUTION.md)；适用范围：pIES 应用、Worker、扩展与前端

## 系统目标

pIES 把用户的设备、连接、时序数据和经济约束转化为可校验、可计算、可追溯的规划结果。系统必须允许设备、建模命令、计算 provider 和存储能力经稳定协议扩展，同时保证错误不会被默认值、兼容分支或回退路径掩盖。

本章用于回答三个问题：一次业务请求怎样穿过系统；一项新能力应该落在哪个模块；模块之间应交换什么。进入具体实现时，再转到[模块开发手册](module-development.md)。

## 系统参与者与边界

```text
规划工程师 / 管理员
        │ 浏览器 GUI
        ↓
      Web 前端 ───────→ 静态帮助中心
        │ HTTPS/JSON/文件
        ↓
      API 适配层
        ↓
     应用用例层 ──────→ 权威数据库
        │       └─────→ 对象存储
        ├──→ 领域模块
        └──→ 任务与快照 ──→ Worker ──→ 生成器 ──→ Solver Bundle
                                  └──→ 求解运行时 ──→ 结果适配/分析
                                                └──→ 证据与结果
```

- 浏览器拥有展示状态和未提交表单，不拥有业务事实；
- API 负责传输，application 负责一次业务动作；
- 领域模块拥有各自规则和公开输入输出；
- 数据库保存事务事实，对象存储保存不可变大内容；
- Worker 只消费冻结快照，不能回读变化中的草稿；
- 帮助中心由正式文档静态生成，不依赖业务服务可用。

## 核心业务流

```text
设备模型/数据 → 建模命令 → 装配与检查 → 计算生成 → 受控求解 → 结果适配
                                │            │          │          │
                                └────不可变快照与内容摘要───────────┘
                                                             ↓
                                                    财务与结果分析
```

这条流程继承早期架构审查中“定义—建模—装配—计算—分析”的稳定意图，并把计算内部进一步拆成可独立替换的三个边界；以架构宪法为最终裁决：

1. **设备定义**发布参数、单位、端口、能力、状态与模型方法；
2. **建模命令**把设备能力映射为可解析、可版本化的标准命令；
3. **装配与检查**以边—端模型验证方向、载能、单位、输入完整性和整体可解性，签发规范装配产物；
4. **计算生成**只消费规范装配产物，由选定 GeneratorProvider 生成求解器输入文件和结构化命令；
5. **受控求解**只消费 Solver Bundle，在隔离环境按结构化命令执行，不再解释项目或设备；
6. **结果适配**把声明输出和执行回执转成统一 `ComputeResult`；
7. **财务与结果分析**把计算输出形成财务指标、敏感性、有效性评估和证据。

装配不是可选预检查。任何直接从旧项目形状拼接计算输入、缺端口时猜双向端口或装配失败后继续计算的路径都违反蓝图。格式边界详见[文件格式标准](file-formats.md)。

## 端到端流程的输入与输出

| 阶段 | 接收 | 产出 | 下游可以依赖的保证 |
|---|---|---|---|
| 设备定义 | provider 与设备规格 | `DeviceDescriptor` | 参数、端口、能力、单位和版本已校验 |
| 建模命令 | 设备描述与命令 provider | `ModelCommand` | 输入输出、状态、单位和执行版本明确 |
| 装配与检查 | 装配 YAML/项目图、绑定、配置、目录快照 | 诊断或 `ValidatedAssemblyArtifact` | 连接、数据和能力合法，业务单位明确，规范摘要与回执完整 |
| 计算生成 | 规范装配、固定资源、generator/solver 选择 | Solver Bundle | 求解器输入、结构化命令、输出和适配器声明完整 |
| 受控求解 | Solver Bundle、资源与取消上下文 | `ExecutionReceipt` 与原始输出 | 实际命令、限制、日志、退出和输出摘要完整 |
| 结果适配 | Bundle、执行回执、声明输出 | `ComputeResult` | 技术/数学状态、候选、流量、单位和依赖版本完整 |
| 财务与分析 | 计算结果、财务参数、分析命令 | 财务/分析结果与证据 | 指标口径、状态、来源和适用范围可追溯 |

每一阶段只消费上游公开输出。若一个阶段必须读取上游目录、ORM 或实现函数才能工作，说明公开 contract 尚未完成，而不是允许临时穿透边界。

## 三条主要业务链

### 编辑链：从 GUI 到项目草稿

```text
表单/画布 → 前端 mapper → HTTP DTO → application 用例
          → 领域校验 → revision 持久化 → 返回新 revision
```

前端可以即时提示，但后端仍执行权限、结构、单位和业务校验。并发冲突返回当前 revision，由用户决定重新加载或显式覆盖，不能最后写入静默获胜。

### 计算链：从提交到结果

```text
提交命令 → application 授权与完整校验 → assembly
        → 固化 CalcSnapshot → 创建 Task → Worker 领取 Attempt
        → generator → Solver Bundle → runtime → result adapter
        → finance/analysis → 提交 Evidence → Result View
```

任务创建之后，任何一步都不得重新读取“当前项目”替换快照输入。重试产生新 attempt，但逻辑输入保持不变。

### 历史链：从结果回到证据

```text
结果视图 → Evidence → Attempt → Task → CalcSnapshot
        → 项目版本 / 数据版本 / 配置版本 / provider 版本 / 对象摘要
```

结果页、评估和导出都沿这条链解释历史。当前草稿、最新数据或最新设备目录只能用于新任务，不能改变旧结果含义。

## 模块职责

| 能力 | 权威职责 | 明确不负责 |
|---|---|---|
| [core](modules/core.md) | 无状态诊断、错误、单位、时间、ID 和纯契约 | 设备/计算 provider 注册、业务默认、持久化 |
| [devices](modules/devices.md) | 设备描述、端口、能力、状态和设备 provider | 项目实例、画布布局、建模执行 |
| [modeling](modules/modeling.md) | 标准建模命令及命令 provider | 读取设备目录内部文件、项目编排 |
| [assembly](modules/assembly.md) | 边—端装配、完整校验和规范装配产物 | 求解器输入、命令和执行 |
| [generators](modules/generators.md) | 规范装配到求解器输入、命令与 Bundle | 启动进程、读取数据库、提交任务 |
| [solver runtime](modules/solver-runtime.md) | 受控执行、资源限制、日志与执行回执 | 设备语义、问题构造、结果解释 |
| [computation](modules/engines.md) | 生成、执行、结果适配的公共契约与统一结果 | HTTP、会话、ORM 和项目草稿 |
| [finance](modules/finance.md) | 现金流、NPV、IRR、LCOE 和回收期 | 页面状态和跨用例事务 |
| [analysis](modules/analysis.md) | 敏感性、批量扫描、指标和评估 | 任务租约、HTTP 传输 |
| [storage](modules/storage.md) | 内容寻址、完整性、引用、保留和存储 provider | 理解项目或结果内部业务表 |
| [application](modules/application.md) | 权限后的用例、事务和跨模块编排 | 穿透模块私有实现 |
| [api](modules/api.md) | HTTP DTO、认证依赖、状态码和错误适配 | ORM 查询和领域计算 |
| [worker](modules/worker.md) | 领取、租约、执行、重试和提交结果 | 缺失命令时降级运行 |

用户模型和算法插件不是新的全局 provider 注册表。各领域模块仍分别拥有设备模型、生成器、执行器和结果 contract；`application/customizations` 只编排用户目录、修订、共享申请、引用安装和可用性查询。组合根只注册通用的沙箱 generator、executor 和 result adapter；用户算法代码作为不可变任务载荷交给其隔离 runner，不能被 API 或普通 Worker import。完整边界见[模型与算法](customization-center.md)。

## 依赖方向

```text
外部请求 / Worker 触发
          ↓
      api / worker
          ↓
      application
          ↓
devices · modeling · assembly · computation · finance · analysis · storage
          ↓
         core
```

业务模块只能调用对方公开门面、实现公开 provider、传递不可变契约或发布版本化事件。跨模块不得读取私有符号、内部注册表、文件路径 helper、持久化实现或对方 ORM。

组合根是唯一选择具体 provider、完成注入和启动校验的位置。各业务模块拥有自己的注册状态，组合根不能把它们合并成新的全局权威表。

## 一项需求怎样落位

按下面顺序判断，不以现有文件“放得下”为依据：

1. 新增的是基础值规则吗？只有完全不知道业务仍成立时才进入 core；
2. 新增的是某类设备的公开能力吗？进入 devices；
3. 新增的是设备执行协议吗？进入 modeling；
4. 新增的是项目能否形成计算问题的规则吗？进入 assembly；
5. 新增的是装配到求解器输入的转换吗？进入 generators；
6. 新增的是进程/容器执行方式吗？进入 solver runtime 的 executor provider；
7. 新增的是求解器输出解释吗？进入 result adapter；
8. 新增的是财务口径或结果比较吗？分别进入 finance 或 analysis；
9. 新增的是一次跨模块用户动作吗？由 application 编排；
10. HTTP、后台执行和页面只分别适配已有用例、任务 contract 和公开 DTO。

网页参数配置、在线 YAML 编辑和 YAML 上传必须汇合为同一个规范设备模型版本。算法插件 ZIP 可以在运行期进入用户目录，但只能作为内容寻址的任务载荷由通用隔离运行器执行；这不是把用户模块加入 API/Worker 的 Python 环境或模块注册表。

若同一条业务规则需要在多个模块复制，先确定唯一所有者，其他模块通过 contract 消费；不要维护同步清单。

## 前后端边界

```text
GUI 表单状态
   ↓ 前端 mapper：字符串、百分比、简单展示单位
公开 HTTP DTO
   ↓ 后端：认证、权限、结构、范围、量纲与领域规则
应用用例 → 公开领域能力 → 持久化/任务
```

前端预检查用于即时反馈，后端校验始终是提交闸门。后端不返回只服务某个组件的临时形状，前端也不通过多种响应猜测或业务缓存弥补不稳定契约。

## 故障原则

- 必需 provider 或命令注册失败：实例不进入 ready；
- 项目或装配输入非法：返回完整诊断并阻断任务；
- 数据库、存储或队列不可用：明确反映在就绪或任务状态；
- 对象缺失或摘要损坏：返回损坏诊断，不回退旧副本；
- 内部异常：受控记录追踪信息，客户端只得到标准错误。

## 开发一个跨模块功能

以“新增一种可规划设备”为例，开发顺序是：

1. devices 发布完整 descriptor；
2. modeling 发布与 descriptor 匹配的命令；
3. assembly 证明真实端口、数据、命令、generator 和 solver 可以组成规范装配产物；
4. 现有 generator 能表达该设备时不增加分支；确有新数学表达时新增独立 GeneratorProvider；
5. runtime 不因设备类型改变，ResultAdapter 只按公开映射解释结果；
6. application 增加创建/校验/提交用例；
7. API 和前端分别适配同一公开 contract；
8. 通过模块协议、HTTP、前端和人工画布验收。

每一步都应能单独测试其输入输出。只有全部链路完成，功能才进入使用者指南；中间目标属于 Roadmap 或开发分支说明。

实现迁移顺序见 [Roadmap](../../changelog/roadmap.md)。
