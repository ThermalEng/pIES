# 算法插件包

> 契约标识：`ies.algorithm-plugin`；目标 `schema_version`：`"1.0.0"`；清单文件：`plugin.yaml`；文件后缀：`*.algorithm.zip`；目标产品版本：`1.1.5`（统一内置契约与临时计算容器底座）/ `1.2.0`（框架零拷贝交接、底座自动工程解还原、并联自描述评价与开放市场分发）

算法插件包是 pIES 全部可计算扩展的统一打包与运行契约。**官方内置计算环节与本地/市场第三方插件均遵循本同一契约**，由 `backend` 经白名单代理 `job_runner` 按**严格单趟单向流水线**拉起断网临时计算容器执行，确保控制面永不加载或执行用户 Python 源码。

## 五类插件种类（`kind`）与框架单向交接协议

流水线拓扑固定为：
`冻结输入 → [predictions[]: 同层按插件分组执行] → (generator → solver → 平台底座自动还原工程解 | algorithm) → [evaluators[]: 0..N 个评价器并联执行] → 最终封存结果`。

**框架负责跨环节只读交接**：第三方插件**绝不承担搬运无关上游文件的职责**；跨环节所需的只读不可变上下文（如冻结装配、物化序列/价格、财务/评价确认及 `generator` 产出的 `variable_map`）统一由 `backend` 调度框架通过硬链接（`os.link`）零拷贝组装进目标环节的只读 `/work/in` 目录，既维持每一环只读自身 `/work/in` 的清晰单向逻辑，又彻底消除插件漏传、篡改或重复校验问题：

| `kind` | `/work/in` 框架组装的只读输入 | `/work/out` 插件自身唯一产物 | 组合与契约标识（`consumes` / `produces`） |
|---|---|---|---|
| `predictor` | 分配给本插件的全部关联 `targets[]`、历史序列与各目标 `parameters` | `/work/out/prediction.json` 与训练产物 | 同层按所选插件分组执行；同插件多目标单趟同源联合预测（保留负荷与风光共享潜变量相关性），框架合并全部预测序列后送入下一环 |
| `generator` | 冻结规范装配、完整物化序列、冻结财务/评价上下文与 `options.generator` | 求解模型文件（如 `problem.mps`）与声明式变量/单位映射 `variable_map`（内置为 `parameters.json` + `parameters.jsonl`） | 产出公开求解包契约（如 `ies.solver-bundle.mps@1.0.0`），执行唯一业务单位到求解内部单位的正向转换 |
| `solver` | `generator` 产出的求解模型文件与 `options.solver`（无需传入财务或装配文件） | 求解器无关标准原变量解（`primal_solution.json`：统一状态枚举、目标值、列值序列与回执元数据），**不搬运 `variable_map` 或财务文件** | 消费匹配 `generator` 契约并产出 `ies.primal-solution@1.0.0`；运行结束后**由平台底座自动结合 `variable_map` 还原出业务单位标准工程解**，第三方 `.mps` 求解器可直接无感替换内置 HiGHS |
| `algorithm` | 冻结规范装配、完整物化序列/价格、冻结财务/评价上下文与 `options.algorithm` | 业务单位标准工程解（`solution.json`：`ies.engineering-solution@1.0.0`） | 严格等价替代 `generator + solver`；产出与分步模式底座还原后**完全同构的标准工程解**（两种模式均支持 `evaluators: []`） |
| `evaluator` | 标准工程解（`ies.engineering-solution@1.0.0`）+ 框架硬链接的完整只读冻结上下文（规范装配 `ValidatedAssemblyArtifact`、物化 `prices`、`EffectiveFinanceConfig`、`EvaluationInput`）+ 本评价器 `parameters` | 官方财务评价器输出标准 `ies.financial-evaluation@1.0.0`；第三方评价器输出自描述报告块 `evaluation_report.json`（`metrics` / `charts` / `tables`） | 支持 `0..N` 个评价器并联独立运行；自描述展示协议由前端通用组件自动渲染并支持导出为 Excel Sheet，新增评价器无需修改前端代码 |

### 标准工程解（`ies.engineering-solution@1.0.0`）与自描述评价块契约

- **标准工程解必填项**（供前端工程页签展示及下游任意 `evaluator` 正向计算）：
  1. `construction_units`：装配中全部规划建设单元的 `built`（布尔值）与各 `planning_variable_id` 业务单位数值；
  2. `series` / `system_series`：覆盖所有电表计费绑定（`meter_bindings`）、变动运维及命名金额引用的设备接口/变量与系统辅助变量完整连续 `step` 业务单位序列；
  3. `solver_summary`（终止原因、`math_status`、可行解标记、目标值与单位等）及可选 `metrics`；缺失财务评价必需的建设单元或计费序列时明确报错，严禁静默补零。
- **评价器自描述报告块（`ies.evaluation-report@1.0.0`）**：第三方 `evaluator`（如碳排放、供电可靠性、㶲效率或海外 PPA 评价）输出自包含的标题、摘要指标列表 `metrics`（`{id, label, value, unit}`）、序列图表列表 `charts`（横纵轴标签、单位与序列数组）及二维明细表列表 `tables`（列定义、单位与行数组），与前端固定财务页签彻底解耦。

## ZIP 目录布局与双形态自检样例

归档根目录不得嵌套额外顶层目录，只允许普通文件和目录（禁止符号链接、硬链接、绝对路径、`..` 路径逃逸、大小写碰撞、加密条目及自带 `Dockerfile`）。向公共市场投稿必填 `README.md`、`LICENSE` 及至少一个无网络依赖的 `tests` 样例，同时支持**单文件 JSON 简写**与**完整 `/work/in → /work/out` 目录样例**（适配 `generator` / `solver` 多文件交接）：

```text
plugin.yaml
requirements.txt       # 可选；仅用基础内置库时省略（强制 --network=none 构建）
src/
  __init__.py
  solver.py
tests/
  minimal_dir/         # 目录形态：in/ 映射为 /work/in/，expected/ 核对 /work/out/
    in/
      problem.mps
    expected/
      primal_solution.json
  simple_json/         # 单文件简写：input.json 映射为 /work/in/request.json
    input.json
    expected.json
LICENSE
README.md
```

## `plugin.yaml` 清单

Manifest 采用 YAML 1.2 安全子集书写，通过 `consumes` 与 `produces` 声明所消费与产出的公开数据契约标识：

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
| `consumes` / `produces` | 公开输入/输出数据契约标识列表，作为提交闸门校验上下游可组合性的权威依据 |
| `runtime` | Python 版本与可选 `requirements` 相对路径；未声明额外依赖时直接以 `--network=none` 复用 `pies-plugin-base` |
| `entrypoint` | 固定 `<module.path>:<public_function>` 格式 |
| `parameters_schema` | 内联 Draft 2020-12 JSON Schema（`additionalProperties: false`），对应本插件在 `predictions[].parameters`、`options.<kind>` 或 `evaluators[].parameters` 中的专属参数槽 |
| `resources` | 默认与上限资源建议，最终受部署策略上限强制裁决 |
| `advanced` | **可选高级扩展段**：流式分块声明、受信任二进制求解器参数模板及**可选** `companions` 限制（常规插件不得硬编码锁定对方 ID） |
| `iteration` | **可选内部循环专用接口**：单容器内自闭环迭代进度上报（追加 NDJSON 至 `/work/out/iterations.jsonl`） |
| `tests` | 无网络自检样例列表（支持单文件 JSON 或 `in/` + `expected/` 目录形态；公共市场投稿必填） |

## 构建信任边界、断网自检与执行生命周期

- **安装期构建信任边界与优先源回退策略**：
  - 无 `requirements` 插件强制以 `--network=none` 直接基于 `pies-plugin-base` 构建；含 `requirements` 插件在 `job_runner` 内存受控上下文中构建（零宿主机业务目录挂载、零业务凭证注入、带硬构建超时）；若管理员配置了 `pip_index_url`，作为**优先源并允许回退默认公网 PyPI**（`--index-url <pip_index_url> --extra-index-url https://pypi.org/simple`，留空直接走默认 PyPI）；离线环境拉包失败时标记 `build_failed` 并弹窗提示所缺插件/依赖及联系管理员处理；
  - 构建完成后，声明的 `tests` 强制在 `--network=none` 容器内全部通过才转入 `ready`。`pies-sdk validate` 与 `pies-sdk pack` 统一覆盖全部五类插件。
- **运行期隔离、同源财务评价与无状态并发安全卸载**：
  - 所有计算环节均以 `--network=none --read-only --cap-drop=ALL` 非 root 临时容器运行；内置财务评价器复用同源财务领域规则对标准工程解做正向全量评价（与内置生成器优化目标数学口径严格一致），**平台与自检门禁仅校验公开契约合法性，不复核插件物理最优性，也不为任何第三方插件的计算正确性背书**；
  - 外部摘要遵循三分校验规则，入库后内部永不存储或重算 `sha256`；
  - 插件停用/卸载与新任务提交共用数据库事务行级锁核对，防止并发竞态；仅在零未终结任务引用该精确版本时经 `delete_plugin_image` 物理清理 `pies-plugin/*` 镜像。
