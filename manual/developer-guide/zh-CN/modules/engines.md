# 计算生成与求解

> 文档状态：生效目标蓝图；目标代码边界：`backend/iesplan/computation/`；现有 `backend/iesplan/engines/` 按 Roadmap 迁移。

## 作用

计算模块把“一个已经证明合法的业务系统”交给具体求解器，并把原始求解输出恢复为统一结果。它不是一个同时理解项目、拼数学模型、运行进程和解释结果的“大引擎”，而是四个严格分开的环节：

```text
ValidatedAssemblyArtifact
          ↓
GeneratorProvider ─→ Solver Bundle
                         ↓
                   SolverRuntime
                         ↓
ExecutionReceipt + 原始输出
          ↓
ResultAdapter ─→ ComputeResult
```

这条边界使新增求解器不必修改项目、API 或 Worker 的业务逻辑，也使运行外部进程的安全规则不依赖某个算法实现。

## 子模块分工

| 子模块 | 输入 | 输出 | 不负责 |
|---|---|---|---|
| [生成器](generators.md) | 已校验装配、固定资源、生成选项 | Solver Bundle | 启动求解器、读取数据库、提交结果 |
| [求解运行时](solver-runtime.md) | Solver Bundle、取消与资源上下文 | ExecutionReceipt、原始输出 | 解释设备、构造数学问题、财务分析 |
| Result Adapter | Bundle、回执、声明输出 | `ComputeResult` | 重试、重新求解、读取当前项目 |
| 计算公共契约 | provider descriptor、状态、回执与结果类型 | 稳定跨模块 DTO | provider 发现、HTTP、ORM |

文件交接格式见 [Solver Bundle](../formats/solver-bundle.md)。

## 输入

计算入口只接受：

- 由装配模块签发的 `ValidatedAssemblyArtifact`；
- 与装配回执一致的内容寻址资源；
- 精确 generator、solver、executor 和 result adapter 版本；
- 固定生成选项、求解选项、随机种子和资源上限；
- Worker 提供的 attempt、取消、租约和证据写入上下文。

不接受项目草稿、前端表单、ORM 对象、未经校验的 YAML/CSV、宿主机路径和动态函数入口。

## 输出

对外统一 `ComputeResult` 至少区分：

- 技术执行状态：完成、超时、取消、资源不足、执行协议失败；
- 数学状态：有解、不可行、无界、数值失败或未求解；
- 业务候选：容量、运行轨迹、目标值和约束余量；
- 算法、生成器、求解器、结果适配器、版本、种子和容差；
- assembly、Bundle、ExecutionReceipt 和原始输出摘要；
- 可定位且不泄露内部路径的结构化诊断。

进程退出码为零不等于存在可推荐方案；不可行也不是内部异常。结果适配器必须完整映射这些语义。

## 开发思路

### 先冻结业务输入

装配模块只负责证明业务输入合法并产生规范装配。它不提前构造某个求解器的私有对象，也不提供命令字符串。

### 再由生成器形成可执行交接包

不同 GeneratorProvider 可以面向 HiGHS、Pyomo、专用仿真器或其他受支持求解器。每个生成器独立负责问题构造、内部单位和输入文件，但都产出同一 Solver Bundle 契约。

### 运行时只执行

SolverRuntime 根据 allowlist 解析结构化命令，在隔离目录执行，采集退出状态、日志、资源和输出摘要。它不根据求解器名称增加业务判断。

### 结果适配器只解释

与生成器配套的 ResultAdapter 将求解器状态、变量和指标映射回统一结果及业务单位。它不能重新读取最新设备目录，也不能对失败结果伪造空成功结果。

## Provider 关系

计算扩展至少包含以下可独立版本化的角色：

- `GeneratorProvider`：规范装配到 Solver Bundle；
- `ExecutorProvider`：受控 executable ID 到隔离运行方式；
- `ResultAdapterProvider`：声明输出到统一结果；
- 可选 `SolverCapabilityProvider`：求解器版本、能力与运行约束描述。

组合根负责发现、校验依赖并原子发布。装配阶段根据 descriptor 校验能力；Worker 按任务快照使用精确版本。任一角色缺失时在执行前失败，不回退到内置实现。

## 失败语义

| 阶段 | 示例 | 结果 |
|---|---|---|
| 生成前 | assembly 摘要不符、generator 能力不足 | 不生成 Bundle，任务阻断 |
| 生成中 | 输入构造失败、非有限值、写文件中断 | 不发布部分 Bundle |
| 执行前 | Bundle 摘要错误、命令越权、路径逃逸 | 不启动进程 |
| 执行中 | 超时、取消、OOM、求解器崩溃 | 保留失败回执与受控日志 |
| 解释时 | 缺输出、格式损坏、状态未知 | 结果协议失败，不发布业务结果 |
| 数学结局 | 不可行、无界、数值不稳定 | 发布明确数学状态与诊断 |

## 必须遵循的规范

- 业务单位到求解内部单位只在 GeneratorProvider 边界发生；反向换算只在 ResultAdapter；
- 生成器不访问网络、数据库、对象服务或环境变量，也不启动子进程；
- 运行时不读取装配语义，不补数据，不切换 solver；
- 命令使用参数数组和受信任 ID，禁止 shell 字符串；
- 所有输入、输出、manifest 和回执都内容寻址；
- 随机过程固定并回传种子；数值比较使用公开容差；
- 私有求解器对象、进程句柄和绝对路径不得跨公共边界；
- 重试由 Worker 按相同快照创建新 attempt，不在 generator/runtime 内隐式循环。

## 增加计算能力

1. 判断需求是新的建模命令、GeneratorProvider、ExecutorProvider 还是 ResultAdapter；
2. 先定义 descriptor、支持的 schema/mode、版本兼容和失败语义；
3. 准备最小规范装配与已知答案；
4. 实现纯生成并固定 Bundle 摘要；
5. 用通用 runtime 执行，补齐成功、不可行、超时和异常输出；
6. 通过 result adapter 映射统一状态和单位；
7. 在组合根原子注册并验证 readiness；
8. 证明 API、项目、装配和 GUI 无需增加具体求解器名称分支。

## 完成标准

- 生成器、运行时和结果适配器可以分别测试和替换；
- 仅凭 Bundle 可复现一次进程执行，凭装配和固定依赖可重建同一 Bundle；
- 非法装配、Bundle 和输出分别在所属边界失败；
- attempt 的输入、命令、日志、原始输出和统一结果形成连续证据链；
- 新生成器接入不修改通用 runtime，新 solver 接入不修改上游业务模块。

代码阅读顺序以目标结构为准：先读 computation 公共 contract，再读 [generators](generators.md)、[solver runtime](solver-runtime.md) 和 result adapters。迁移期间阅读现有 `engines` 时，要把“构造、执行、解释”分别标记并逐步移入对应边界，不能把现状当成长期 contract。
