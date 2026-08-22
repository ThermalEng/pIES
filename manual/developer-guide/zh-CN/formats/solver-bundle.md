# Solver Bundle

> 契约标识：`ies.solver-bundle`；目标 schema：`1.0.0`；生产者：GeneratorProvider；消费者：SolverRuntime。

Solver Bundle 是一次求解的可执行、不可变交接包。生成器把已校验装配转成求解器输入文件和结构化命令；通用运行时只核验并执行该命令。这样“如何构造数学问题”和“如何安全运行进程”成为两个可以独立开发、测试和替换的部分。

Solver Bundle 不由最终用户手写，也不能从未经校验的装配 YAML 直接拼接。

## 目录结构

```text
solver-bundle/
├── bundle.yaml
├── input/
│   ├── problem.mps
│   └── parameters.json
└── output/
```

`output/` 在生成时为空。求解器只能写入 manifest 声明的相对输出路径。Bundle 可以存为内容寻址目录或不可变归档，但解包后必须保持相同规范路径和摘要。

## `bundle.yaml` 示例

```yaml
schema: ies.solver-bundle
schema_version: "1.0.0"

bundle:
  assembly_sha256: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
  generator: acme.generator.highs_milp@1.0.0
  solver: ies.solver.highs@1.7.2

inputs:
  - path: input/problem.mps
    media_type: application/mps
    sha256: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
  - path: input/parameters.json
    media_type: application/json
    sha256: cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc

command:
  executor: ies.executor.local_process@1.0.0
  executable: ies.solver.highs@1.7.2
  arguments:
    - input/problem.mps
    - --solution_file
    - output/solution.json
  working_directory: .
  environment:
    OMP_NUM_THREADS: "4"
  timeout_seconds: 600
  memory_mib: 4096
  cpu_cores: 4
  network: false
  success_exit_codes:
    - 0

outputs:
  - path: output/solution.json
    media_type: application/json
    required: true

result_adapter: acme.result-adapter.highs@1.0.0
extensions: {}
```

## 生成器职责

一个 GeneratorProvider 接收 `ValidatedAssemblyArtifact` 和已固定的资源内容，负责：

1. 验证装配摘要、校验回执和自身能力匹配；
2. 调用版本化建模命令，建立变量、目标、约束和索引；
3. 在唯一边界把业务单位转换为求解器内部单位；
4. 生成 MPS、LP、JSON 或特定求解器需要的输入文件；
5. 声明受控命令、资源限制、预期输出和结果适配器；
6. 对 manifest 与全部输入计算摘要并原子发布 Bundle。

生成器应是确定性的纯转换：不得连接数据库、对象服务或网络，不得启动求解器，不得读环境变量，不得修改项目或任务状态。资源由调用方按内容摘要解析后显式传入。

同一规范装配、资源摘要、生成器版本和固定种子必须产生相同输入字节及 Bundle 摘要。若底层文件格式本身含非确定字段，生成器必须移除或固定它们。

## 结构化命令

`command` 是数据结构，不是 shell 字符串：

- `executor`：受信任运行方式的稳定 ID 与精确版本；
- `executable`：由运行时根据 allowlist 解析的求解器 ID，不是文件路径；
- `arguments`：一个参数一个字符串，禁止拼成命令行；
- `working_directory`：只能是 Bundle 内的规范相对目录；
- `environment`：仅包含 executor contract 允许的变量；
- 资源与网络字段：声明硬限制，`network` 默认并通常固定为 `false`；
- `success_exit_codes`：只判断进程是否按协议结束，不能替代求解状态解析。

禁止 `sh -c`、`bash -c`、管道、重定向、命令替换、通配符展开和由参数触发的任意插件加载。命令参数中的路径同样必须在 Bundle 内，不能含绝对路径或 `..`。

## 运行时职责

SolverRuntime 是求解器无关的受控执行层，只负责：

1. 校验 Bundle schema、整体摘要、输入摘要和规范路径；
2. 确认 executor、executable、参数、环境和资源策略在部署 allowlist 内；
3. 建立隔离工作目录，以最小权限运行一个命令；
4. 执行超时、取消、CPU、内存、文件和网络限制；
5. 分别捕获 stdout、stderr、退出码、信号、开始/结束时间和资源统计；
6. 核对声明输出，生成不可变 `ExecutionReceipt`。

运行时不读取 AssemblySpec、不构造数学问题、不解释设备类型、不修正输入文件，也不把非零退出码自动重试成另一个求解器。部署对可执行文件 ID 的解析属于组合根和 executor provider，不进入 Bundle。

## 输出和结果适配

每个输出都必须预先声明相对路径、媒体类型和是否必需。求解后：

- 缺少必需输出、输出越界或非预期文件视为执行协议失败；
- 原始输出按内容寻址保存并写入回执；
- stdout/stderr 单独保存，不能混入业务结果；
- `success_exit_codes` 只表示进程协议成功；不可行、无界等业务状态由结果适配器读取求解器输出后映射。

`result_adapter` 接收只读 Bundle、ExecutionReceipt 和声明输出，产出统一 `ComputeResult`。它不得重新运行求解器、访问项目草稿或根据当前最新 provider 改写历史结果。

## `ExecutionReceipt` 最小内容

回执至少固定：

- Bundle 摘要、assembly 摘要、attempt ID；
- executor、solver、generator、result adapter 的 ID 与版本；
- 实际开始/结束时间、退出码或终止信号；
- 资源限制和可用的使用统计；
- stdout、stderr 和每个输出的内容摘要；
- 取消、超时、容量故障和完整性故障的结构化状态。

回执是证据，不是可变运行日志索引。迟到 attempt 的回执可以保留审计，但不能覆盖当前有效任务结果。

## 失败边界

| 失败 | 所属边界 | 处理 |
|---|---|---|
| 装配未通过或回执不匹配 | generator 入口 | 拒绝生成，不产生部分 Bundle |
| 建模命令或生成器能力缺失 | generator | 任务运行前阻断 |
| 输入文件生成中断 | generator | 临时产物不发布 |
| 摘要、路径或 allowlist 不合法 | runtime 入口 | 不启动进程 |
| 超时、取消、OOM、非零退出 | runtime | 形成失败 ExecutionReceipt |
| 不可行、无界、数值异常 | result adapter | 映射为明确 ComputeResult 状态 |
| 结果格式损坏或非有限值 | result adapter | 结果协议失败，不发布伪成功 |

## 增加一个生成器

1. 声明稳定 generator ID、版本、支持的装配 schema、mode、设备/命令能力和 solver；
2. 定义生成器选项 schema、内部单位、确定性和资源上界；
3. 实现纯转换和配套 result adapter；
4. 提供最小装配、生成后 Bundle、输入摘要和已知求解结果；
5. 测试非法装配回执、能力不匹配、路径、非确定性和中途失败；
6. 通过 provider 原子注册，由组合根显式选择；
7. 证明通用 runtime 无需为该生成器或求解器增加业务分支。

## 完成标准

- 生成、执行、结果适配可分别进行契约测试；
- runtime 测试只靠 Bundle 即可执行，不需要项目数据库和设备目录；
- generator 测试不启动外部进程；
- 相同输入的 Bundle 摘要稳定，任一输入变化都能被追溯；
- 任意命令注入、路径逃逸、未声明输出和网络访问默认被拒绝；
- 新求解器主要新增 generator/result-adapter/executor provider，不修改装配、API 或 GUI 类型映射。
