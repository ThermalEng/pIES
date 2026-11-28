# 模块开发手册

> 文档状态：生效蓝图；适用范围：后端各业务模块边界与开发协作

本手册提供进入具体后端模块开发的索引与协作方法。各模块章节详述其公开契约、核心边界、失败语义与验证标准，不在此重复各模块的私有实现代码。

## 阅读与开发入口

接手单模块开发任务时，推荐按以下顺序建立上下文：

1. 阅读[架构宪法](ARCHITECTURE_CONSTITUTION.md)了解不可突破的跨模块边界；
2. 阅读对应模块手册；
3. 查看该模块的公开门面（`__init__.py` / `contracts.py`）与对应测试文件；
4. 进入模块内部实现。

涉及跨模块数据结构调整时补充阅读[公共契约](contracts.md)，涉及文件规范时阅读[文件格式标准](file-formats.md)，涉及快照与对象生命周期时阅读[领域模型与追溯链](domain-model.md)。

## 后端模块地图

```text
设备技术定义 ─→ 方程解析 ─→ 装配与检查 ─┐
全期序列、财务规则与参数 ──────────────┼→ 计算生成器 ─→ Solver Bundle
计算配置 ────────────────────────────┘                       ↓
                                                      求解运行时 ─→ 结果适配
                                                                      ↓
                                                      同次解的财务评价 / 结果分析

HTTP API ──→ 应用用例 ──→ 上述领域模块 / 对象存储
                 │
                 └──→ 不可变快照 ──→ Worker ──→ 证据与结果

组合根 ──→ 发现并注入各模块 Provider，校验启动依赖
所有模块 ──→ Core（纯类型、诊断、单位与时间轴）
各领域 Repository ──→ 持久化适配 ──→ PostgreSQL
```

| 模块 | 核心职责 | 主要输入 | 主要输出 |
|---|---|---|---|
| [bootstrap](modules/bootstrap.md) | 读取配置、装配 Provider 并发布就绪状态 | 部署配置与候选 Provider | 完整 `ApplicationContext` |
| [core](modules/core.md) | 提供无业务状态的共同语言 | 原始值与基础参数 | 不可变值对象、诊断与规范 JSON |
| [devices](modules/devices.md) | 维护设备技术规格与目录发布 | 设备 YAML 与 Provider | 不可变 `DeviceDescriptor` |
| [modeling](modules/modeling.md) | 解析受限技术方程并生成数学贡献 | 设备描述、方程 AST | 变量、约束、状态迁移与结果映射 |
| [assembly](modules/assembly.md) | 校验项目拓扑与配置并签发规范装配 | 项目图、基线、绑定、配置 | 诊断或 `ValidatedAssemblyArtifact` |
| [generators](modules/generators.md) | 数学模型转换、单位换算与生成计算包 | 装配产物、贡献、序列、财务、计算与评价输入 | Solver Bundle |
| [solver runtime](modules/solver-runtime.md) | 隔离执行受控求解命令 | Solver Bundle、Attempt 上下文 | `ExecutionReceipt` 与原始输出 |
| [computation](modules/engines.md) | 编排序列物化、预测调度与结果适配 | 装配产物、Provider 与回执 | `ComputeResult` |
| [finance](modules/finance.md) | 财务三件套合并、规划财务关系与项目评价 | 财务配置、评价确认、同次解 | 财务关系、现金流与评价指标 |
| [analysis](modules/analysis.md) | 指标计算、参数扫描与四维系统评估 | 计算结果、评价事实、分析命令 | 分析结果与评估报告 |
| [storage](modules/storage.md) | 不可变大对象读写、Owner 引用与孤儿对象清理 | 字节流、媒体类型、Owner | `ObjectHandle` 与引用状态 |
| [application](modules/application.md) | 控制事务边界与跨模块用例编排 | `ActorContext`、用例命令 | 应用结果、事件或后台任务 |
| [api](modules/api.md) | HTTP 路由、认证适配与 DTO 映射 | HTTP 请求 | 标准化 JSON 响应或错误信封 |
| [worker](modules/worker.md) | 控制面内嵌调度、唯一任务所有权与阶段检查点 | 任务队列、`CalcSnapshot` | Attempt 进度、阶段资产与终态 |
| [persistence](modules/persistence.md) | 隔离领域事实与数据库表实现 | Repository 命令 | 受数据库约束保护的事务状态 |

## 模块边界检查原则

设计模块功能前，应先理清接口契约链：

```text
上游所有者 → 本模块公开输入 → 本模块处理 → 本模块公开输出 → 下游消费者
```

- 如果当前模块必须导入上游模块的私有符号、内部文件路径或 ORM 模型才能运行，说明上游公开门面缺失，应先补齐公开契约而非绕过边界。
- 如果下游必须根据字段名称拼接规则或捕获宽泛异常猜测状态，说明本模块输出契约不够显式。

## 跨模块变更协作顺序

当需求同时跨越多个模块时，契约所有权保留在各领域模块，业务编排由 [application](modules/application.md) 承担，推荐推进顺序为：

1. 明确业务场景与可观察的最终输出；
2. 划清各领域模块分别拥有的事实与不变量；
3. 先定义或调整模块间的不可变契约；
4. 补齐各领域的单元测试与协议测试；
5. 在 `application` 层完成事务、引用与用例编排；
6. 最后接入 `api`、Worker 与前端，并在 Docker 环境中执行真实浏览器或端到端验收。
