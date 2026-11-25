# 算法插件包

> 契约标识：`ies.algorithm-plugin`；目标 `schema_version`：`"1.0.0"`；清单文件：`plugin.yaml`；文件后缀：`*.algorithm.zip`；目标产品版本：`1.1.5`（统一内置契约与临时计算容器底座）/ `1.2.0`（外部插件导入、镜像自动构建与市场分发）

算法插件包是 pIES 全部可计算扩展的统一打包与运行契约。**官方内置计算环节（预测器、组装器、求解器、结果适配与后期财务评价）与本地/市场第三方插件均遵循本同一契约**，由 `backend` 通过白名单容器代理 `job_runner` 按环节拉起断网临时计算容器执行，确保 Web/API 控制面永不加载或执行用户 Python 源码。

## 五类插件种类（`kind`）

| `kind` | 所属计算环节 | 典型职责与入口形态 |
|---|---|---|
| `predictor` | 预测环节 | 消费历史序列与参数，输出预测序列及回执（耦合型可配合内部循环接口） |
| `generator` | 组装/生成环节 | 消费冻结装配、序列、财务与计算配置，输出标准化 `Solver Bundle` |
| `solver` | 求解环节 | 消费 `Solver Bundle`，产出原始求解输出与执行日志 |
| `evaluator` | 结果适配与后期财务评价环节 | 消费 `Bundle`、原始解与冻结财务/评价基准，输出 `ComputeResult` 与财务评价事实 |
| `algorithm` | 极简一体化算法 | 跳过组装与求解拆分，单入口函数直接消费冻结计算输入包并输出标准结果 |

### 五类环节标准输入输出文件与组合规则

| `kind` | `/work/in` 标准输入 | `/work/out` 标准输出 | 组合兼容规则 |
|---|---|---|---|
| `predictor` | `/work/in/request.json` 与历史序列 | `/work/out/prediction.json` 与训练产物 | 可前置衔接 `generator` 或一体化 `algorithm` |
| `generator` | `/work/in/` 冻结装配、序列、财务与计算配置 | `/work/out/bundle/` 标准 `Solver Bundle` | 必须与选定 `solver` 契约及媒体类型兼容 |
| `solver` | `/work/in/bundle/`（只读 `Solver Bundle`） | `/work/out/solution.*` 声明输出 | 由选定 `generator` 声明兼容并在提交闸门核对 |
| `evaluator` | `/work/in/bundle/`（或 `algorithm` 标准解）、原始解、回执与冻结财务/基准 | `/work/out/result.json`（`ComputeResult` 与财务评价事实） | 消费配套上游声明与回执，按公共结果契约输出 |
| `algorithm` | `/work/in/` 标准化冻结计算输入包 | `/work/out/result.json`（标准规划/调度结果文件） | 严格等价替代 `generator + solver` 拆分，不跨越上游 `predictor` 与下游 `evaluator` 边界（按配置独立衔接解耦的 `evaluator` 或在 `evaluator: null` 时直接作为结果） |

## ZIP 目录布局（简单优先、兼容复杂包）

归档根目录不得嵌套额外顶层目录，只允许普通文件和目录（禁止符号链接、硬链接、绝对路径、`..` 路径逃逸、大小写碰撞与加密条目）。

### 最简合法结构（本地离线测试与极简算法）

```text
my-plugin/   # 打包为 ZIP 根目录内容，不含此顶层目录名
├── plugin.yaml
├── main.py
└── requirements.txt   # 可选；仅用基础环境内置库时可省略
```

### 完整结构（复杂包或向公共市场投稿）

```text
plugin.yaml
requirements.txt       # 可选；声明额外纯 Python 依赖
src/                   # 可选；多模块源码包
  __init__.py
  solver.py
tests/                 # 上架公共市场必填；至少包含一个无网络依赖的自检样例
  minimal/
    input.json
    expected.json
LICENSE                # 上架公共市场必填
README.md              # 上架公共市场必填
```

## `plugin.yaml` 清单

Manifest 采用 YAML 1.2 安全子集书写。普通一体化算法与预测插件只需声明核心字段与内联 `parameters_schema`；内置或高性能求解器可通过可选 `advanced` 与 `iteration` 扩展段声明高级能力。

### 极简插件示例

```yaml
schema: ies.algorithm-plugin
schema_version: "1.0.0"
plugin_id: acme.algorithm.capacity_search
version: "1.2.0"
kind: algorithm
name:
  zh_cn: 容量搜索算法
  en_us: Capacity search
description: 用于连续与整数容量变量的受限搜索。
license: MPL-2.0
compatibility:
  pies: ">=1.1.5,<2.0.0"
runtime:
  language: python
  python: "3.12"
  requirements: requirements.txt
entrypoint: main:solve
parameters_schema:
  type: object
  additionalProperties: false
  properties:
    max_steps:
      type: integer
      minimum: 1
      default: 100
resources:
  default:
    cpu: 1
    memory_mb: 512
    timeout_seconds: 300
    process_limit: 8
  maximum:
    cpu: 2
    memory_mb: 2048
    timeout_seconds: 3600
    process_limit: 32
tests:
  - id: minimal
    input: tests/minimal/input.json
    expected: tests/minimal/expected.json
extensions: {}
```

### 清单核心与扩展字段

| 字段 | 说明与约束 |
|---|---|
| `plugin_id` / `version` | 稳定命名空间 ID（`ies.*` / `<publisher>.*`）与三段式语义版本 |
| `kind` | 封闭枚举：`predictor` / `generator` / `solver` / `evaluator` / `algorithm` |
| `compatibility` | 兼容的 pIES 或 SDK 版本范围 |
| `runtime` | 声明 Python 版本与可选 `requirements` 相对路径；未声明额外依赖时直接复用官方基础环境 |
| `entrypoint` | 固定 `<module.path>:<public_function>` 格式，支持单文件 `main:solve` 或包路径 `src.solver:solve` |
| `parameters_schema` | 内联 Draft 2020-12 JSON Schema（禁止未知字段），用于动态渲染表单与提交边界校验 |
| `resources` | 默认与上限资源建议（`cpu`、`memory_mb`、`timeout_seconds`、`process_limit`），最终受部署策略上限强制裁决 |
| `advanced` | **可选高级扩展段**：用于流式分块 `Solver Bundle` 声明、外部受信任求解器二进制参数约定及配套上下游环节精确引用（官方内置 HiGHS 与未来外部高性能求解器共用本段） |
| `iteration` | **可选内部循环专用接口**：用于声明含内部迭代循环的算法配置，详见下方说明 |
| `tests` | 最小无网络自检样例列表（向公共市场投稿时必填） |

## 内部循环算法专用接口（`iteration`）

凡包含内部迭代循环的算法（如耦合预测-求解闭环或迭代启发式搜索），**在单个临时计算容器内部自闭环运行**，不拆成跨容器每轮冷启动循环。插件通过在 `plugin.yaml` 声明可选 `iteration` 段接入平台统一进度与收敛观测协议：

```yaml
iteration:
  enabled: true
  max_iterations_default: 20
  convergence_metric: max_relative_delta
  progress_channel: /work/out/iterations.jsonl
```

- **输入约定**：容器从 `/work/in/request.json` 接收冻结的 `tolerance`、`max_iterations` 与统一总超时预算；
- **轮次上报**：容器每完成一轮迭代向 `/work/out/iterations.jsonl` 追加一行 NDJSON 记录（含 `iteration`、`metric`、`value`、`unit: "1"` 与状态标记）；
- **预算与取消**：内部循环严格受同一容器总超时与外部停止信号控制，达到收敛阈值或上限后输出最终产物并正常退出。

## 容器执行契约（`/work/in → /work/out`）与镜像构建

- **安装期自动构建计算镜像与断网自检门禁**：导入或安装第三方插件时，由 `job_runner` 套用官方标准模板基于 `pies-plugin-base` 自动构建专属插件计算镜像 `pies-plugin/<plugin_id>:<version>`（支持管理员配置可选 pip 镜像/内网源，留空走默认源；不要求作者手写 Dockerfile；无额外依赖的插件直接零联网复用基础层）。若 `plugin.yaml` 声明了 `tests`，构建完成后强制在 `--network=none` 容器内通过全部自检样例后才标记插件可用。依赖安装阶段与运行阶段彻底分离；不可变约束针对冻结生成的 `/work/in` 输入文件、任务快照与持久化证据（用于可追溯），不强行要求一次即毁的临时容器运行环境或镜像不可变；管理员彻底卸载某插件版本时经 `job_runner` 白名单操作 `delete_plugin_image` 仅清理受管 `pies-plugin/*` 镜像。
- **极简 `pies-sdk` 校验与打包**：五类插件（`predictor` / `generator` / `solver` / `evaluator` / `algorithm`）统一复用 `pies-sdk validate`（校验 `plugin.yaml`、目录安全与离线自检）与 `pies-sdk pack`（打包标准 `*.algorithm.zip` 并输出 `sha256`）。
- **运行期断网隔离交接**：每次执行某一计算环节时，`backend` 将该环节的冻结只读输入写入独立作业目录并由 `job_runner` 拉起临时计算容器（`--network=none --read-only --cap-drop=ALL`，带硬资源限额）：
  - `/work/in`（只读挂载）：承载标准化冻结输入包（JSON/分块文件流）及参数/种子配置；
  - `/work/out`（可写挂载）：承载声明输出产物、结构化日志与可选 `/work/out/iterations.jsonl`。
- **结果边界与外部校验**：
  - 遵循[部署与运行](../deployment.md#临时计算容器隔离与资源观测)统一容器生命周期（自然退出/取消/超时 → 收集退出状态与声明输出 → 持久化有效产物 → `cleanup_stage` 清理容器与临时文件）；声明输出进入 pIES 结果模型前必须严格通过公开结果契约校验（结构、字段类型、有限数值与引用匹配），**平台不对任何插件的数学最优性或财务计算正确性背书**（计算正确性责任归插件作者与选用者）；
  - 外部市场下载与离线导入入口一次性校验压缩安全与内容 `sha256`；入库后 pIES 内部不存储、不重算、不比对 SHA-256；
  - 修改入口、Schema、单位或算法语义须提升 `MAJOR`；向后兼容新增提升 `MINOR`；缺陷修复提升 `PATCH`；已发布同版本不可静默覆盖。
  - `2.0.0` 正式发布前遵循单一当前契约，不建设多版本兼容矩阵；若插件声明的 `compatibility` 与当前 pIES 版本不兼容，安装与任务提交闸门必须明确报错提示，绝不静默失效。
