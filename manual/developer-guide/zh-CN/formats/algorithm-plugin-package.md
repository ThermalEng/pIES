# 算法插件包

> 契约标识：`ies.algorithm-plugin`；目标 schema：`1.0.0`；文件后缀：`*.algorithm.zip`；消费者：插件校验器与隔离运行器

## 作用

算法插件包把“规范装配到求解文件的加工程序”和“执行算法的程序”作为一个精确版本交付。它允许用户上传自己的计算实现，同时保证 API 和普通 Worker 不加载用户代码，任务仍能固定输入、依赖、资源限制和证据。

第一版仅支持 Python 源码和离线锁定的 Python 依赖。原生二进制、动态链接库、自带容器镜像、安装脚本、Git URL、在线包索引和运行时下载不在本契约范围内。

## 固定目录

ZIP 根目录不得再包一层任意文件夹，最小结构为：

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

只允许普通文件和目录。禁止符号链接、硬链接、设备文件、FIFO、绝对路径、`..`、重复规范路径、大小写碰撞和加密条目。归档条目数、单条目大小、总解压大小和压缩比必须受部署限额控制。

## Manifest

`algorithm.plugin.yaml` 使用 YAML 1.2 安全子集，至少包含：

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

字段语义：

| 字段 | 规则 |
|---|---|
| `plugin_id` | 稳定命名空间 ID，不含用户数据库 ID、路径或实现类名 |
| `version` | 插件语义版本；同 ID + 版本只能对应一个包摘要 |
| `runtime` | 只声明受支持语言版本和包内锁文件，不指定宿主机解释器路径 |
| `generator.entrypoint` | 包内受控 Python 模块与公开函数，只能由隔离运行器解析 |
| `algorithm.entrypoint` | 包内算法入口，禁止 shell 字符串和参数拼接 |
| `options_schema` | JSON Schema，未知字段默认拒绝 |
| `result` | 声明输出 schema 和系统受支持的统一适配器精确版本 |
| `resources` | 插件建议值；部署上限始终优先，不能由插件放宽 |
| `tests` | 至少一个无需网络的最小自检样例 |

## Python 入口

入口格式固定为 `<package.module>:<public_function>`。模块必须位于 `src/`，不能以相对导入逃逸、不能引用宿主机模块路径，也不能通过入口参数指定第二个模块。

逻辑协议为：

```python
def generate(request: GenerateRequest) -> GenerateResult: ...
def solve(request: SolveRequest) -> SolveResult: ...
```

具体 Python 类型由 SDK 生成，但跨进程事实是版本化 JSON 与文件 manifest：

- `GenerateRequest` 只含规范装配文本、固定资源清单、选项、种子和工作目录内逻辑路径；
- `GenerateResult` 只含生成文件清单、每个文件摘要、求解请求和输出声明；系统通用 GeneratorProvider 据此生成标准 Solver Bundle；
- `SolveRequest` 由 SolverRuntime 的通用 ExecutorProvider 从 Solver Bundle 建立，只含生成结果、规范选项、种子、停止条件和资源限制；
- `SolveResult` 只含技术状态、业务结局、声明输出、日志引用和资源统计。

入口不得返回进程句柄、打开的文件、Python 私有对象、数据库连接或宿主机路径。所有数值必须有限。

## 依赖锁

`requirements.lock` 必须是按部署支持格式生成的完整传递依赖锁，每个分发文件带 SHA-256。只允许部署离线仓库中存在且策略允许的纯 Python wheel；第一版拒绝源码包、VCS 依赖、本地绝对路径、可编辑安装、URL 依赖、未锁哈希依赖和原生扩展。

运行环境身份为：

```text
sha256(plugin_zip) + sha256(requirements.lock) + runner_contract_version + python_runtime_digest
```

环境可以缓存和重建，但不能成为任务的权威输入。任务快照保存上述全部身份字段。

## 加工阶段规则

加工入口只能消费 `ValidatedAssemblyArtifact` 与已固定资源。它负责：

- 校验插件声明支持的装配 schema、mode 和变量类型；
- 把业务单位转换到该插件明确声明的内部单位；
- 生成求解输入文件和确定性 manifest；
- 声明算法入口需要的参数、文件和预期输出；
- 对每个文件计算摘要。

禁止读取当前项目、数据库、对象服务、网络、进程环境和未声明文件。相同输入、包版本、依赖环境和种子必须产生相同加工结果；若声明非确定性，必须说明来源并仍固定随机种子和环境。

## 算法阶段规则

算法入口只读取加工 manifest 允许的相对路径。命令行、shell、子进程和额外可执行文件默认禁止；确需子进程能力必须等后续 schema 明确增加，不能放在 `extensions` 中绕过。

输出必须符合 `result.schema.json`，并包含：

- 技术状态和业务结局；
- 随机种子、停止条件和实际迭代信息；
- 声明的指标、变量和必要时序结果；
- 非成功状态下的结构化原因；
- 不含秘密和宿主机路径的日志摘要。

非有限数值、未声明输出、路径逃逸、输出超限和 schema 不匹配都使该 attempt 失败。

## 校验顺序

1. 上传媒体类型、大小、摘要和配额；
2. ZIP 中央目录、路径、条目类型、数量、大小和压缩比；
3. manifest 安全解析、schema、ID、版本和引用完整性；
4. 依赖锁格式、哈希、离线可用性和许可策略；
5. Python 语法、入口存在性和禁止能力静态扫描；
6. 在隔离运行器构建环境；
7. 对每个最小样例执行加工和算法自检；
8. 校验输出 schema、摘要、确定性和资源上限；
9. 固化包摘要、环境摘要和不可变校验报告。

任何阶段失败都不得发布“可选择”状态。静态扫描不能执行入口；动态自检只能在隔离运行器执行。

## 隔离要求

插件运行时必须：

- 位于独立容器化 runner，而非 API 或普通 Worker；
- 使用非 root 身份、只读包与依赖环境、独立临时工作目录；
- 默认无网络、无业务凭证、无数据库和宿主机目录；
- 强制 CPU、内存、进程数、文件大小、日志和时间限制；
- 只通过受控 manifest 取得输入和提交输出；
- 在取消、超时或租约失效时终止完整进程组；
- 生成带 runner 版本、环境摘要、实际限制、退出状态和输出摘要的执行回执。

## 版本与共享

- 改变入口、输入输出 schema、内部单位或算法语义必须提升 MAJOR；
- 增加向后兼容的可选能力提升 MINOR；
- 不改变 contract 的修复提升 PATCH；
- 同一精确版本的包字节不可替换；
- 共享园地审批绑定插件 ID、版本和 ZIP 摘要；批准后上传同名新包不能继承批准状态；
- 用户安装共享插件只建立目录引用，不复制 ZIP、校验报告或环境缓存；
- 历史任务始终引用原包和环境摘要，不自动迁移到新版本。

## 完成标准

- 合法最小包可以在无网络 Docker 隔离环境完成加工、求解和结果校验；
- 非法路径、压缩炸弹、缺哈希依赖、错误入口、越权文件、网络访问和资源超限均被稳定诊断阻断；
- API 和普通 Worker 测试证明不会 import 或执行包内源码；
- 相同输入和确定性插件产生相同加工文件摘要与结果基线；
- ZIP、环境、执行回执和结果都能追溯到任务快照；
- 共享安装人数增加不会增加底层包对象份数。
