# 模块开发手册

> 文档状态：生效蓝图；适用范围：后端各业务边界及其公开协作

本章是从总体架构进入具体代码的路由图。架构宪法定义全局不可突破的边界；下面的模块章节把这些边界转化为可以直接用于设计和开发的上下文。

## 怎样使用模块手册

一个只修改单个模块的任务，最低阅读集合是：

1. [架构宪法](ARCHITECTURE_CONSTITUTION.md)；
2. 对应模块章节；
3. 对应模块的公开门面、契约类型和测试；
4. 本次修改涉及的实现代码。

只有在改变跨模块数据结构时才需要继续阅读[公共契约](contracts.md)，改变公共文件时阅读[文件格式标准](file-formats.md)，改变业务证据或生命周期时阅读[领域模型](domain-model.md)，增加 provider 时阅读[扩展体系](extensions.md)。历史 Review 和 archive 不属于开发必读输入。

## 模块地图

```text
设备技术定义/序列数据 ─→ 方程解析 ─→ 装配与检查 ─→ 计算生成器 ─→ Solver Bundle
                                    │                         │
                                    └─规范装配产物             ↓
                                                     求解运行时 ─→ 结果适配
                                                                    │
                                                     财务计算/结果分析

HTTP API ──→ 应用用例 ──→ 上述领域模块 / 对象存储
                 │
                 └──→ 不可变快照 ──→ Worker ──→ 证据与结果

组合根 ──→ 选择并注入各模块 provider，校验启动依赖
所有模块 ──→ Core 的纯类型、诊断、单位与时间
各领域 repository ──→ 持久化适配 ──→ 权威数据库
```

| 模块 | 解决的问题 | 主要输入 | 主要输出 |
|---|---|---|---|
| [bootstrap](modules/bootstrap.md) | 选择实现并验证实例能否服务 | 配置与 provider 候选 | 完整应用装配与 readiness |
| [core](modules/core.md) | 提供无业务状态的共同语言 | 原始值和纯配置 | 类型、诊断、规范值 |
| [devices](modules/devices.md) | 描述系统中有哪些设备能力 | 设备 provider 与规格 | `DeviceDescriptor` |
| [modeling](modules/modeling.md) | 把受限设备方程变成公共数学贡献 | 设备 descriptor、方程 AST | 变量、关系、状态和结果映射 |
| [assembly](modules/assembly.md) | 把装配 YAML/项目图变成可信规范产物 | 项目图、绑定、配置、各目录快照 | 诊断或 `ValidatedAssemblyArtifact` |
| [generators](modules/generators.md) | 生成求解器输入文件和结构化命令 | 规范装配、固定资源、生成器选择 | Solver Bundle |
| [solver runtime](modules/solver-runtime.md) | 在受控环境执行 Bundle 命令 | Solver Bundle、attempt 上下文 | `ExecutionReceipt` 与原始输出 |
| [computation](modules/engines.md) | 组织生成、执行和结果适配的公共语义 | 装配产物、provider 与执行上下文 | `ComputeResult` 与连续证据 |
| [finance](modules/finance.md) | 解释方案经济性 | 运行结果、财务参数 | `FinancialResult` |
| [analysis](modules/analysis.md) | 比较方案并评估证据 | 结果、场景、评估命令 | 分析与评估结果 |
| [storage](modules/storage.md) | 管理不可变大对象及引用 | 字节、媒体类型、owner | `ObjectHandle` 与完整性状态 |
| [application](modules/application.md) | 编排一个完整业务用例 | 已认证主体、用例命令 | 业务结果、事件或任务 |
| [api](modules/api.md) | 把 HTTP 转成应用命令 | HTTP 请求 | 标准响应或错误信封 |
| [worker](modules/worker.md) | 可靠执行长任务 | 任务、租约、不可变快照 | attempt、进度、证据与终态 |
| [persistence](modules/persistence.md) | 隔离领域事实与数据库实现 | 领域 repository 命令 | 约束保护的事务事实 |

## 每个模块章节回答什么

各章统一回答八个问题：

1. **作用**：模块为系统解决什么问题；
2. **边界**：负责什么、不负责什么；
3. **输入**：谁提供、进入前必须满足什么条件；
4. **输出**：交给谁、必须保证什么；
5. **开发思路**：主流程和内部职责怎样分层；
6. **失败语义**：哪些失败必须暴露，调用方怎样处理；
7. **扩展步骤**：增加能力时从哪里开始、按什么顺序完成；
8. **完成标准**：需要哪些测试和文档证据。

章节描述公开语义，不复制私有函数、ORM 表或临时实现。具体字段以模块公开 contract 和生成的 schema 为准；章节说明这些字段为什么存在以及跨边界时必须保持的含义。

## 局部开发的判断方法

开始编码前先画出一行输入输出：

```text
上游所有者 → 本模块公开输入 → 本模块处理 → 本模块公开输出 → 下游消费者
```

如果实现需要读取上游私有文件、ORM 或注册表，说明输入契约不完整，应先补公开能力；如果下游需要猜测输出形状、单位或失败状态，说明输出契约不完整。不要以跨目录导入或兼容分支临时补洞。

## 跨模块变更

一个变更同时影响两个以上模块时，由 [application](modules/application.md) 负责业务编排，但契约所有权仍留在各领域模块。推荐顺序是：

1. 明确场景和最终用户可见结果；
2. 确认每个模块拥有的事实；
3. 定义或修改不可变输入输出；
4. 先完成各模块协议测试；
5. 再完成 application 编排；
6. 最后接入 API、Worker 或前端，并做端到端验收。

任何一步需要改变宪法强制条款时停止实现，先走 ADR 和修宪流程。
