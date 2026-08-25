# 模型与算法

> 文档状态：生效蓝图；目标产品版本：`1.1.0`；上位规范：[架构宪法](ARCHITECTURE_CONSTITUTION.md)；文件契约：[设备模型 YAML](formats/device-model-yaml.md)、[算法插件包](formats/algorithm-plugin-package.md)

## 产品目标

一级导航“模型与算法”面向所有已登录用户，固定包含三个入口：

1. **模型库**：创建、编辑、上传、校验、版本化和管理当前用户可用的设备模型；
2. **算法插件**：上传、校验、测试和管理当前用户可用的加工与求解插件；
3. **共享园地**：浏览管理员已批准共享的精确版本，并以引用方式安装到自己的目录。

模型和算法最终都必须成为版本化文件对象。页面不是另一套模型格式，数据库也不是 YAML/ZIP 的替代事实源。项目和任务只保存稳定 ID、精确版本、内容摘要和依赖锁。

## 明确不做

- 不在 API、普通 Worker 或 Web 进程中 `import`、`exec`、`eval` 用户代码；
- 不把上传目录加入 `PYTHONPATH`，不修改业务模块注册表，不做运行期 provider 热加载；
- 不允许项目直接引用草稿、文件名、`latest` 或共享园地中尚未安装的条目；
- 不因“安装到我的目录”复制 YAML、ZIP、依赖锁、样例或环境缓存；
- 不允许用户在设备模型 YAML 中写 Python 路径、命令、shell、凭证或宿主机路径；
- 不把管理员批准共享解释为管理员担保计算正确性；批准只确认共享政策、风险报告和可公开性。

## 核心对象

| 对象 | 含义 | 可变性 |
|---|---|---|
| `CustomEntry` | 用户目录中的逻辑条目，含类型、所有者、名称和草稿 revision | 可编辑 |
| `CustomVersion` | 一个规范模型 YAML 或算法插件 ZIP 的精确版本 | 不可变 |
| `ValidationReport` | 静态校验、隔离自检、兼容性和诊断结果 | 不可变 |
| `CatalogInstallation` | 用户目录到某个精确版本的逻辑引用 | 可安装/卸载 |
| `ShareRequest` | 所有者申请把精确版本加入共享园地的审批记录 | 状态转换，不覆盖历史 |
| `PluginEnvironment` | 由插件摘要和依赖锁构建的可重建运行环境缓存 | 可重建 |

`CustomVersion` 的底层内容由 storage 内容寻址保存。创建者默认拥有一条安装引用；其他用户从共享园地安装时只新增 `CatalogInstallation` 和对象 owner 引用。相同摘要只保存一份字节对象。

## 模型库

### 三种作者入口，一个权威结果

模型库必须同时提供：

- 参数配置：用 schema 驱动表单填写设备 ID、名称、参数、单位、端口、数据字段、状态和命令引用；
- 在线编辑 YAML：编辑完整 `ies.device-model` 文本，提供语法、schema 和领域诊断；
- 上传 YAML：上传 `*.device.yaml`，读取后进入同一个编辑和校验流程。

三条路径共享同一草稿 revision。表单切换到 YAML 时必须由同一 serializer 生成文本；YAML 切换到表单时必须经安全解析和 mapper 还原。无法无损表示的字段必须阻止切换并显示诊断，禁止静默丢弃。发布时统一执行安全解析、完整 schema 校验、领域校验、规范化和 SHA-256 计算，形成不可变 `CustomVersion`。

### 模型身份与版本

- 每个用户首次创建内容时由系统分配不含个人信息的公开随机命名空间；模型稳定 ID 采用 `user.<public_namespace>.device.<slug>`，不包含数据库 ID、用户名、路径或显示名，并在全系统唯一；
- 创建后稳定 ID 不可修改，显示名称可随新版本变化；
- 版本使用明确的 `MAJOR.MINOR.PATCH`，同一稳定 ID + 版本唯一；
- 同版本不同摘要冲突必须拒绝；相同摘要重复提交应返回已有版本；
- 端口、单位、参数语义或命令引用发生破坏性变化时必须提升 MAJOR；
- 发布版本不可原地编辑，修改必须从该版本派生新草稿。

### 可选择条件

一个模型版本只有同时满足以下条件才返回 `selectable=true`：

1. 已安装到当前用户目录；
2. YAML schema、端口、单位、参数和命令引用校验无阻断；
3. 所需建模命令及精确版本当前可解析；
4. 未被所有者停用，且不存在对象损坏；
5. 对当前项目用途具备所需能力。

系统建模的设备选择器从“系统内置 + 当前用户已安装模型”的统一只读目录取得 descriptor，并显示来源、稳定 ID 和精确版本。共享园地条目未安装前不能直接进入项目。

## 算法插件

### 插件职责

算法插件包同时交付两个受控阶段：

1. **加工程序**：消费 `ValidatedAssemblyArtifact` 与已固定资源，确定性生成求解所需文件和输出声明；
2. **算法程序**：消费加工结果与明确选项，执行求解并产生声明输出。

第一版只支持 [算法插件包](formats/algorithm-plugin-package.md)规定的 Python ZIP。插件必须包含 manifest、加工入口、算法入口、依赖锁、输入输出 schema、最小合法样例、预期结果和许可证。原生二进制、自带容器镜像、在线依赖解析和安装脚本不属于第一版。

加工程序仍受 GeneratorProvider 语义约束：只能消费规范装配和固定资源，不能读取数据库、当前项目、网络、对象服务或宿主机路径。算法程序只能访问隔离工作目录和 manifest 声明的输入，不能获得业务凭证。

### 校验与可用性

上传后按下列顺序处理：

```text
流式上传与大小门禁
  → ZIP 结构/路径/压缩比/条目数检查
  → manifest 与依赖锁静态校验
  → 源码和入口静态风险扫描
  → 隔离环境构建
  → 最小样例动态自检
  → 固化 ValidationReport
  → 所有者目录显示为 private_ready 或 invalid
```

静态扫描不是安全边界，不能替代运行隔离。依赖必须来自部署允许的离线 wheel 仓库或已批准镜像层，并以哈希锁定；运行时禁止访问公共包仓库。

插件只有在当前用户已安装、最新校验报告通过、环境可构建、运行器兼容且未停用时才 `selectable=true`。计算配置选择后固定插件稳定 ID、精确版本、包摘要、环境摘要、加工入口版本、算法入口版本、选项、种子和资源上限。

## 隔离运行架构

普通计算 Worker 仍按 `GeneratorProvider → Solver Bundle → SolverRuntime/ExecutorProvider → ResultAdapter` 编排任务、租约、重试和证据，但不执行用户模块。组合根启动时注册受信任的通用 `SandboxedAlgorithmGeneratorProvider`、`SandboxedAlgorithmExecutorProvider` 和 `UserAlgorithmResultAdapterProvider`；用户包只是它们按摘要消费的任务载荷，不是运行期注册项。

Docker 部署新增独立 `plugin_runner` 服务，供通用 generator 和 executor 通过版本化内部协议分阶段调用。加工阶段在 runner 内执行包的 generator 入口，通用 GeneratorProvider 把生成文件、结构化命令与输出声明封装成标准 Solver Bundle；执行阶段由 SolverRuntime 调用通用 ExecutorProvider，再在 runner 内执行算法入口：

```text
普通 Worker
  → SandboxedAlgorithmGeneratorProvider
  → plugin_runner: 加工入口 → 求解文件
  → 标准 Solver Bundle
  → SolverRuntime / SandboxedAlgorithmExecutorProvider
  → plugin_runner: 算法入口 → 声明输出 + ExecutionReceipt
  → UserAlgorithmResultAdapterProvider → ComputeResult
  → Worker 校验租约与摘要后提交证据
```

默认隔离基线：

- `plugin_runner` 是独立容器，不向普通 Worker 或 runner 挂载 Docker Socket；
- 容器以非 root 用户运行，根文件系统只读，不挂载仓库、`/data`、数据库 Socket 或宿主机目录；
- 用户子进程使用空白环境、独立临时目录、文件 allowlist、CPU/内存/进程数/文件大小/时间限制；
- 默认禁止外网和业务内网访问，只允许 runner 的受控输入输出通道；
- runner 不持有数据库、对象存储或用户会话凭证，输入由可信编排方按摘要暂存，输出经摘要回收；
- 一个插件版本的依赖环境按包摘要 + 锁文件摘要构建，只作为可删除缓存复用；
- 超时、取消、OOM、非法系统调用、输出越界和非零退出必须形成明确回执并回收完整进程组；
- stdout/stderr 有长度上限并脱敏，不允许成为业务结果或秘密通道。

若部署平台提供更强的 sandbox adapter，可以替换默认 runner；不能因此改变插件包、任务快照和执行回执 contract。

## 共享园地

### 申请与审批

共享申请只针对一个不可变精确版本。创建者填写公开名称、摘要、分类、许可证、适用范围、版本说明和风险说明。后端附上内容摘要、完整校验报告和依赖清单，申请后这些字段不可替换。

管理员可以批准或驳回，并必须填写审核说明。批准前至少核对：

- 内容仍与申请摘要一致；
- 模型或插件校验通过；
- 许可证允许共享；
- 描述不含秘密、个人信息或误导性声明；
- 算法插件依赖、资源上限和动态自检结果可接受。

管理员批准的是共享可见性，不把条目复制到所有用户目录。共享园地只显示 `shared` 精确版本。

### 浏览与安装

所有已登录用户可以按类型、载能、能力、作者、许可证、版本和兼容性浏览共享园地。安装操作必须幂等，只新增：

- 当前用户到 `CustomVersion` 的 `CatalogInstallation`；
- storage 中当前用户目录用途的 owner 引用；
- 安装时间、来源和可选备注。

不得复制内容对象、校验报告或依赖环境。用户可以同时安装同一稳定 ID 的多个精确版本；业务选择器默认展示兼容版本，但不能静默升级。

### 停止共享与卸载

- 创建者可申请停止共享，管理员也可因安全或许可问题立即停止新的浏览/安装；
- 停止共享不自动卸载其他用户已安装的版本；严重安全问题可以把版本标为 `blocked`，此时禁止新任务，但历史证据仍可读取；
- 卸载只解除当前用户目录引用，已被项目草稿、项目版本、快照或证据引用时应提示影响并保留必要对象引用；
- 新版本不会替换旧安装，用户必须显式安装并在项目中选择新版本。

## 状态模型

版本状态使用明确枚举，不把多个维度挤进一个模糊状态：

| 维度 | 状态 |
|---|---|
| 内容 | `draft`、`validating`、`invalid`、`private_ready`、`deprecated`、`blocked` |
| 共享申请 | `not_requested`、`pending`、`approved`、`rejected`、`withdrawn`、`unshared` |
| 用户安装 | `installed`、`uninstalled` |
| 运行环境 | `not_applicable`、`queued`、`building`、`ready`、`failed`、`stale` |

`selectable` 是后端根据版本、安装、依赖、环境、兼容性和阻断状态计算的只读结论，不作为独立可写事实。

## 权限

- 已登录用户可以创建、修改自己的草稿，发布私有版本，上传插件，安装共享版本和卸载自己的目录引用；
- 只有条目所有者可以申请共享、发布新版本、停用自己的版本或撤回申请；
- 管理员可以查看待审材料，批准/驳回共享、停止共享和因安全原因阻断版本；
- 普通用户不能读取他人未共享的条目、版本、校验日志或对象；
- 项目仍遵循现有项目权限，安装目录不能成为读取他人项目的旁路；
- 管理员审批、阻断、停止共享以及用户发布、安装、卸载均写入最小化审计。

## 模块归属

| 能力 | 权威所有者 |
|---|---|
| 设备模型 YAML 解析、规范化和 descriptor | `devices` 公开门面 |
| 算法插件 manifest、加工/算法输入输出 contract，以及通用 generator/executor/result adapter | `computation` 公开 contract |
| 用户目录、版本、安装、共享申请与审批用例 | `application/customizations` |
| 内容字节、摘要、owner 引用和清理 | `storage` |
| HTTP、multipart、状态码和 DTO | `api` |
| 页面、表单/YAML mapper、上传和共享浏览 | `features/customizations` |
| 任务租约、快照、runner 调用和证据提交 | 普通 `worker` |
| 用户代码环境、自检和受限执行 | 独立 `plugin_runner` |

统一页面目录不意味着建立跨模块全局 registry。`application/customizations` 通过各模块公开校验和解析能力形成聚合 read model，不读取内部 registry、ORM 或实现路径。

## 完成标准

- 三个一级入口及模型的三种作者方式行为一致，发布结果可逐字节复现；
- 模型和插件的稳定 ID、精确版本、摘要、依赖和来源贯穿目录、项目、快照和证据；
- 共享安装只增加引用，相同内容的存储字节数不随安装用户数增长；
- 未安装、校验失败、不兼容、停用或阻断版本不会进入业务选择器；
- API 和普通 Worker 无任何用户代码 import/执行路径；
- 插件在 Docker 内的独立 runner 完成无网络、无业务凭证、限资源的自检和计算；
- 共享审批、停止共享、阻断、安装和卸载的权限、冲突和审计可验证；
- 历史项目与任务不因共享状态、卸载或新版本发布而改变解释；
- 全部编译、测试和浏览器验收在 Docker 中完成，画布拖放仍由人工验收。

具体实施切片、端点草案、数据表、测试矩阵和禁止事项见[AI 开发功能要求](ai-development/customization-center-requirements.md)。
