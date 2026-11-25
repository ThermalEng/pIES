# 算法插件包

> 契约标识：`ies.algorithm-plugin`；目标 `schema_version`：`"1.0.0"`；清单文件：`plugin.yaml`；文件后缀：`*.algorithm.zip`；目标产品版本：`1.1.5`（统一内置契约与临时计算容器底座）/ `1.2.0`（单链可替换交接、受控构建自检与开放市场分发）

算法插件包是 pIES 全部可计算扩展的统一打包与运行契约。**官方内置计算环节与本地/市场第三方插件均遵循本同一契约**，由 `backend` 经白名单容器代理 `job_runner` 按**严格单趟单向单链**（`冻结输入 → [predictor] → (generator → solver | algorithm) → [evaluator] → 结果`）拉起断网临时计算容器执行，确保 Web/API 控制面永不加载或执行用户 Python 源码。

## 五类插件种类（`kind`）与单链交接契约

每一环节的出口目录（`/work/out`）直接作为紧邻下一环节的唯一进口目录（`/work/in`），**禁止跨级旁路挂载上游目录，禁止跨容器往返循环**；需要跨环节传递的映射与冻结上下文统一装入显式 `passthrough` 字段由中间环节**原样透传**（`pies-sdk` 提供标准透传辅助）：

| `kind` | `/work/in` 单链输入 | `/work/out` 单链输出 | 可替换组合与契约标识（`consumes` / `produces`） |
|---|---|---|---|
| `predictor` | `/work/in/request.json`（含分配给本插件的全部关联 `targets[]`、历史序列与单目标参数）及上游 `passthrough` | `/work/out/prediction.json`、训练产物及合并后的单链下游输入包 | 原生支持单趟多目标同源联合预测（保留负荷与风光间共享气温/日照潜变量相关性），不再设依赖求解结果的跨容器外循环 |
| `generator` | 上一环交付的冻结装配、物化序列、计算配置与冻结财务/评价上下文 | 求解输入文件（如 `problem.mps`）、声明式列号—业务变量/单位映射 `variable_map` 及 `passthrough`（含冻结 `finance` 与 `evaluation`） | 产出公开求解包契约（如 `ies.solver-bundle.mps@1.0.0`），供任意声明消费该契约的 `solver` 接入 |
| `solver` | **仅读取 `generator` 出口目录**（模型文件、`variable_map`、`passthrough` 与本环节参数） | 求解器无关标准原变量解（`primal_solution.json`：统一状态枚举、目标值、列值序列与回执元数据）+ **原样透传**的 `variable_map` 与 `passthrough` | 消费匹配 `generator` 契约并产出 `ies.primal-solution@1.0.0`；不执行业务单位换算，第三方 `.mps` 求解器可直接无感替换内置 HiGHS |
| `algorithm` | 上一环交付的标准化冻结计算输入包（含本环节参数与 `passthrough`） | 业务单位标准工程解（`solution.json`：装机容量、选建状态、逐时序列与求解摘要）+ **原样透传**的 `passthrough` | 严格等价替代 `generator + solver` 拆分，产出 `ies.engineering-solution@1.0.0`，不跨越上游 `predictor` 与下游 `evaluator` 边界 |
| `evaluator` | **仅读取紧邻上一环（`solver` 或 `algorithm`）出口目录**（绝不跨级挂载 `generator` 目录）及本环节参数 | `/work/out/result.json`（`ComputeResult`、逐时序列及同口径 `ies.financial-evaluation@1.0.0` 财务评价事实） | 经 `pies-sdk` 统一归一化 `{primal_solution + variable_map}` 与 `{engineering_solution}`，结合透传 `passthrough` 正向算出完整评价；当 `CalculationConfig.evaluator` 显式置 `null` 时跳过本环节直接交付上游结果 |

## ZIP 目录布局与多形态自检样例

归档根目录不得嵌套额外顶层目录，只允许普通文件和目录（禁止符号链接、硬链接、绝对路径、`..` 路径逃逸、大小写碰撞、加密条目及自带 `Dockerfile`）。

### 最简合法结构（本地开发与极简算法）

```text
my-plugin/   # 打包为 ZIP 根目录内容，不含此顶层目录名
├── plugin.yaml
├── main.py
└── requirements.txt   # 可选；仅用基础环境内置库时省略（零联网构建）
```

### 完整结构（支持单 JSON 简写与多文件目录级 `tests`）

向公共市场投稿必填 `README.md`、`LICENSE` 及至少一个无网络依赖的 `tests` 样例。为同时适配轻量插件与需要多文件输入输出的 `generator` / `solver`，`tests` 支持**单文件 JSON 简写**与**完整 `/work/in → /work/out` 目录样例**两种形态：

```text
plugin.yaml
requirements.txt       # 可选；声明额外纯 Python 依赖
src/
  __init__.py
  solver.py
tests/
  minimal_dir/         # 目录形态：in/ 原样映射为 /work/in/，expected/ 核对 /work/out/
    in/
      problem.mps
      variable_map.json
      passthrough.json
    expected/
      primal_solution.json
  simple_json/         # 单文件简写：input.json 映射为 /work/in/request.json
    input.json
    expected.json
LICENSE
README.md
```

## `plugin.yaml` 清单

Manifest 采用 YAML 1.2 安全子集书写。插件通过 `consumes` 与 `produces` 声明所消费与产出的公开数据契约标识，实现跨作者插件自由替换组合。

### 插件清单示例

```yaml
schema: ies.algorithm-plugin
schema_version: "1.0.0"
plugin_id: acme.algorithm.capacity_search
version: "1.2.0"
kind: algorithm
name:
  zh_cn: 容量搜索算法
  en_us: Capacity search
description: 用于连续与整数容量变量的受限启发式搜索。
license: MPL-2.0
compatibility:
  pies: ">=1.2.0,<2.0.0"
consumes:
  - ies.stage-input@1.0.0
produces:
  - ies.engineering-solution@1.0.0
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
  - id: simple_json
    input: tests/simple_json/input.json
    expected: tests/simple_json/expected.json
extensions: {}
```

### 清单核心与扩展字段

| 字段 | 说明与约束 |
|---|---|
| `plugin_id` / `version` | 稳定命名空间 ID（`ies.*` / `<publisher>.*` / 本地 `user.<public_namespace>.*`）与三段式语义版本 |
| `kind` | 封闭枚举：`predictor` / `generator` / `solver` / `evaluator` / `algorithm` |
| `compatibility` | 兼容的 pIES 或 SDK 版本范围，不满足时在安装与任务提交闸门明确报错拒绝 |
| `consumes` / `produces` | 公开输入/输出数据契约标识列表，作为提交闸门校验上下游可组合性的权威依据（不兼容组合在提交闸门直接拒绝，严禁运行期猜测转换） |
| `runtime` | 声明 Python 版本与可选 `requirements` 相对路径；未声明额外依赖时直接零联网复用 `pies-plugin-base` |
| `entrypoint` | 固定 `<module.path>:<public_function>` 格式 |
| `parameters_schema` | 内联 Draft 2020-12 JSON Schema（`additionalProperties: false`），对应 `CalculationConfig` 中本环节专属参数槽（`predictions[].parameters` 或 `options.<kind>`），各环节参数隔离不冲突 |
| `resources` | 默认与上限资源建议（`cpu`、`memory_mb`、`timeout_seconds`、`process_limit`），受部署策略上限强制裁决 |
| `advanced` | **可选高级扩展段**：用于流式分块声明、受信任二进制求解器参数模板及**可选** `companions` 限定（仅当确实依赖特定专用伙伴版本时声明，常规插件不得硬编码锁定对方 ID） |
| `iteration` | **可选内部循环专用接口**：用于单容器内自闭环迭代进度上报，详见下方说明 |
| `tests` | 无网络自检样例列表（支持单文件 JSON 或 `in/` + `expected/` 目录形态；公共市场投稿必填） |

## 内部循环算法专用接口（`iteration`）

凡包含迭代寻优循环的算法（如启发式搜索、仿真优化或单求解器内部多轮恢复），**一律在单个临时计算容器内部自闭环运行**，不拆成跨容器往返循环。插件声明 `iteration` 段接入统一进度观测：

```yaml
iteration:
  enabled: true
  max_iterations_default: 20
  convergence_metric: max_relative_delta
  progress_channel: /work/out/iterations.jsonl
```

- 容器每完成一轮内部迭代向 `/work/out/iterations.jsonl` 追加一行 NDJSON 记录（含 `iteration`、`metric`、`value`、`unit: "1"` 与状态标记），严格受单容器总超时与外部停止信号约束。

## 构建信任边界、断网自检与执行生命周期

- **安装期构建信任边界与断网自检门禁**：
  - 无 `requirements` 插件强制以 `--network=none` 直接基于 `pies-plugin-base` 构建；含 `requirements` 插件在 `job_runner` 内存受控上下文中构建（零宿主机业务目录挂载、零业务凭证注入、带硬超时限制），使用可选 `pip_index_url` 或默认 PyPI（离线拉包失败时标记 `build_failed` 并弹窗提示联系管理员处理）；
  - 构建完成后，若声明了 `tests`，强制在 `--network=none` 容器内跑通全部单文件/目录级自检样例后才置为 `ready`。`pies-sdk validate` 与 `pies-sdk pack` 统一覆盖全部五类插件。
- **运行期断网单链交接与免背书边界**：
  - 每次执行单链一环时，以 `--network=none --read-only --cap-drop=ALL` 非 root 身份拉起临时容器（只读 `/work/in`、可写 `/work/out`），持久化有效产物后立即 `cleanup_stage` 清理；
  - 声明输出进入结果模型前严格通过公开结果契约校验，**平台与自检门禁仅验证契约合规性，不对任何插件的工程最优性或财务正确性背书**；
  - 外部摘要遵循三分校验规则（市场下载强制核对 `index.json` `sha256`、带摘要离线包核对所给 `sha256`、普通裸离线包仅做归档安全与契约校验），入库后内部永不存储或重算 `sha256`；
  - 管理员卸载清理 `pies-plugin/*` 镜像时执行无状态在途任务检查：仅在无未终结任务引用该精确版本时调用 `delete_plugin_image`，有在途引用时仅下架阻止新任务并提示待在途任务结束后清理。
