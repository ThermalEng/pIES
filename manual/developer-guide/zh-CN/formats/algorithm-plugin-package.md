# 算法插件包

> 契约标识：`ies.algorithm-plugin`；目标 `schema_version`：`"1.0.0"`；文件后缀：`*.algorithm.zip`；目标产品版本：`1.1.0`

算法插件包用于将用户自定义的“规范装配加工程序”和“求解算法程序”以单一不可变版本打包交付，由独立容器化隔离运行器校验与执行，确保 Web/API 与普通 Worker 进程永不加载用户 Python 代码。

第一版仅支持纯 Python 源码与离线依赖锁，拒绝原生二进制、动态链接库、自带容器镜像、安装脚本、Git/URL 依赖与运行期在线下载。

## 固定 ZIP 目录布局

归档根目录不得嵌套额外顶层目录，只允许普通文件和目录（禁止符号链接、硬链接、绝对路径、`..` 路径逃逸、大小写碰撞与加密条目）：

```text
algorithm.plugin.yaml
requirements.lock
src/
  generator.py
  algorithm.py
schemas/
  options.schema.json
  result.schema.json
tests/
  minimal/
    assembly.json
    resources/
    expected.json
LICENSE
README.md
```

## `algorithm.plugin.yaml` 清单

Manifest 采用 YAML 1.2 安全子集书写：

```yaml
schema: ies.algorithm-plugin
schema_version: "1.0.0"
plugin_id: acme.algorithm.capacity_search
version: "1.2.0"
name:
  zh_cn: 容量搜索算法
  en_us: Capacity search
description: 用于连续与整数容量变量的受限搜索。
license: MPL-2.0
runtime:
  language: python
  python: "3.12"
  dependency_lock: requirements.lock
generator:
  entrypoint: src.generator:generate
  deterministic: true
  supported_assembly_schema:
    - "1.0.0"
algorithm:
  entrypoint: src.algorithm:solve
  modes:
    - planning
  variable_types:
    - continuous
    - integer
  supports_seed: true
  options_schema: schemas/options.schema.json
result:
  schema: schemas/result.schema.json
  adapter: ies.result.user_algorithm@1.0.0
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
    input: tests/minimal/assembly.json
    expected: tests/minimal/expected.json
extensions: {}
```

| 字段 | 说明与约束 |
|---|---|
| `plugin_id` / `version` | 稳定命名空间 ID 与三段式语义版本 |
| `runtime` | 声明支持的 Python 版本与包内 `requirements.lock` 路径（仅限部署离线仓库许可的纯 Python wheel） |
| `generator.entrypoint` / `algorithm.entrypoint` | 固定 `<package.module>:<public_function>` 格式，模块必须位于 `src/` 下 |
| `options_schema` / `result.schema` | 严格 JSON Schema，未知字段默认拒绝；`result.adapter` 指定系统统一适配器版本 |
| `resources` | 插件建议值，最终受部署策略上限强制约束 |
| `tests` | 至少包含一个无网络依赖的最小自检样例 |

## 入口契约与隔离运行约束

- **加工入口（`generate(request) -> GenerateResult`）**：接收包内规范装配、固定资源清单、选项、种子与工作目录逻辑路径；负责验证支持的能力与执行业务单位转换，产出确定性求解输入文件清单与结构化求解请求，由通用沙箱生成器封装标准 `Solver Bundle`。
- **算法入口（`solve(request) -> SolveResult`）**：仅读取加工清单声明的相对路径输入，禁止 Shell/子进程调用、网络访问或读取环境/数据库；输出必须严格匹配 `result.schema.json`，仅含有限数值与受控日志。
- **沙箱校验门禁**：上传按 `媒体类型与大小 → 归档安全与压缩比 → Manifest 与引用完整性 → 离线依赖锁 → 静态语法扫描 → 隔离容器环境构建 → 最小样例自检 → 输出与确定性验证` 依次执行，全链通过后方可登记为可选择版本。
- **版本规则**：修改入口、Schema、单位或算法语义须提升 `MAJOR`；向后兼容新增提升 `MINOR`；缺陷修复提升 `PATCH`。共享园地审批与任务快照均绑定精确 `(plugin_id, version)`，安装仅建立引用而不复制底层归档。
