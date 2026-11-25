# 模型与算法扩展指南

> 文档状态：生效目标指南；目标产品版本：`1.2.0`；上位规范：[架构宪法](ARCHITECTURE_CONSTITUTION.md)；相关格式：[设备模型 YAML](formats/device-model-yaml.md)、[算法插件包](formats/algorithm-plugin-package.md)、[计算配置](formats/calculation-config.md)

本指南规定用户自定义设备模型、算法插件包与独立公共市场 **pIES Market**（规划于 `1.2.0` 交付）的长期交付标准、市场索引与无状态投稿契约、身份与多版本模型、正交运行模式与审批机制及安全隔离边界。

## 核心架构定位

模型与算法生态由**本地管理（`ThermalEng/pIES`）**与**独立公共市场（`ThermalEng/pIES-Market`）**共同构成：

- **本地模型与插件管理**：在 pIES 实例内管理设备模型（`ies.device-model` YAML 经基础信息/`properties`/`interfaces` 常用字段双向同步表单、在线 YAML 编辑器与 `.device.yaml` 文件导入/导出统一校验生成，复杂 `equations` 保持 YAML/表达式编辑）与算法插件（`ies.algorithm-plugin` ZIP 经受控镜像构建、断网自检与单链容器流水线执行），并依托零后端依赖的极简独立包 `pies-sdk` 提供运行时契约及通用 `validate`/`pack` 工具。
- **pIES Market（公共市场）**：与各 pIES 部署实例解耦，由静态 JSON 索引（`index.json`）、制品分发与同源纯静态浏览/下载页（免登录筛选、查看说明/许可证/`sha256` 并直接下载离线包），以及独立可自托管的极薄无状态投稿网关组成。公共市场**不得成为 pIES 本地计算的运行依赖**——市场关闭、不可用或断网时，本地已安装的模型、插件与离线导入导出功能均不受影响。
- **统一单页「模型与算法」视图与管理后台分工**：系统升级原自定义入口（`/custom`）为统一单页「模型与算法」，不设平行的“共享园地”页面，内部采用两个平级页签：
  - **「市场与已安装」**：统一呈现制品流，支持按制品/插件种类（设备模型及五类算法插件）分类筛选、切换同一稳定 ID 下的历史精确版本/修订，并为每个条目明确标注三类前端主状态之一：
    - **`远端`**：仅联网模式下可见的公共市场可用制品；
    - **`本地（未安装）`**：本地目录已就绪（状态为 `ready`），但当前成员尚未安装启用的制品；
    - **`本地（已安装）`**：当前成员已安装且可用于建模或计算的制品。
  - **「我的创作/草稿」**：承载个人自定义设备模型编辑（表单/YAML/文件导入导出、草稿保存与发布）以及个人向本地提交设备模型或算法插件的后台状态（`pending_approval` 待审批 / `building` 构建自检中 / `ready` 可用 / `build_failed` 构建或自检失败 / `disabled` 已停用）与诊断记录。
  - **管理后台（`/admin`）**：集中承载两个正交实例开关（运行模式 `market_mode` 与本地上传审批 `local_upload_requires_approval`）、市场索引与投稿网关地址、可选 `pip_index_url` 镜像/内网源、待审批队列，以及本地目录条目的启停与卸载清理。

## 身份、命名空间归属与多版本并存模型

### 稳定命名空间与所有权规则

| 来源类别 | 稳定标识格式 | 所有权、投稿转换与版本固定规则 |
|---|---|---|
| 官方内置与官方市场制品 | `ies.<type>.<slug>` | 保留给官方维护与官方发布；按精确版本或发布 `revision` 不可变固定 |
| 单实例本地个人自建草稿 | `user.<public_namespace>.<type>.<slug>` | `public_namespace` 为系统分配的 12 位随机 Base32 字符串（60 bit 熵），终身不变，用于单实例自建与跨实例离线导包防撞名及账号名脱敏 |
| 公共市场社区/厂家制品 | `<publisher>.<type>.<slug>` | 投稿公共市场时统一收敛为可读 `<publisher>` 前缀；首次审核通过时在市场仓库静态文件登记 `<publisher>` 与验证邮箱加密哈希绑定（零明文邮箱），后续仅同一验证邮箱可发布该命名空间新版本/新修订 |

- **跨实例保留原始身份与修订**：从公共市场下载或通过离线包导入制品时，**原样保留其稳定 ID 与发布方分配的精确版本或整数 `revision`**（纯技术 `.device.yaml` 正文不内嵌版本字段，发布 `revision` 由市场索引或导出元数据携带；无元数据的裸单文件离线导入默认记为 `revision = 1` 或按导入参数指定），绝不重新编号。
- **幂等与冲突拒绝**：同一 `(id, version/revision)` 若规范化内容完全一致则幂等成功；若内容不一致则直接按 `409` 冲突拒绝覆盖，**严禁自动改名、自动递增修订号或静默覆盖**。

### 实例级共享缓存、多版本并存与快照冻结

- **禁止运行期热加载**：Web、API 与调度控制面进程永不加载或 `import` 用户插件源码；组合根仅在进程启动时静态注册受信任的执行桥接。
- **实例级去重缓存 + 成员级多版本并存安装**：底层制品对象与构建好的插件镜像在实例内按精确 `(id, version/revision)` 去重共享；安装启用状态按**当前成员**独立维护。同一稳定 ID 允许多个精确版本/修订在本地目录与成员已安装列表中并存（卡片默认展示最新版，详情可切换历史版本），不同项目与计算配置各自引用精确版本互不干扰。
- **安装即对后续计算可选与提交冻结**：仅后台状态为 `ready` 且被当前成员安装启用的精确版本/修订可被项目与计算配置引用（禁止草稿或 `latest`）。任务提交闸门校验通过后，一次性将精确引用、阶段分槽参数与不可变输入冻结进 `CalcSnapshot` 并建立对象引用；后续安装、停用或卸载**仅影响新提交计算**，绝不干扰在途任务或历史快照追溯。

### 外部完整性校验边界（三分规则）

严格遵守[架构宪法](ARCHITECTURE_CONSTITUTION.md#26-校验只设在真实边界)，按外部导入来源区分边界校验：

| 外部来源 | 边界校验规则 |
|---|---|
| 公共市场远端下载 | **强制**比对下载字节与 `index.json` 声明的权威 `sha256`，并执行归档安全与契约校验 |
| 附带预期摘要的离线导入 | 比对文件字节与用户提供的预期 `sha256`，并执行归档安全与契约校验 |
| 未附带摘要的普通离线包 | 仅执行归档安全与公开契约校验，不强制要求手工提供摘要，保障离线分享便利性 |

完成上述外部入口校验并写入受管对象存储获得不可变 `ObjectId` 后，pIES 数据库、快照与运行期交接**一律不存储、不重算、不比对 SHA-256**，也不以内容摘要作为业务身份。

## 公共市场索引契约与无状态投稿协议

### 静态市场索引契约（`ies.market-index@1.0.0`）

`ThermalEng/pIES-Market` 构建生成的 `index.json` 是同源纯静态浏览/下载页与各 pIES 客户端共同消费的**唯一公开制品清单契约**（不维护第二套列表）：

```json
{
  "schema": "ies.market-index",
  "schema_version": "1.0.0",
  "generated_at": "2026-11-25T12:00:00Z",
  "artifacts": [
    {
      "id": "acme.device.chp",
      "kind": "device_model",
      "revision": 1,
      "name": { "zh_cn": "燃气热电联产机组", "en_us": "Gas CHP Unit" },
      "description": "纯技术声明式热电联产设备模型。",
      "publisher": "acme",
      "license": "MPL-2.0",
      "compatibility": { "pies": ">=1.2.0,<2.0.0" },
      "url": "artifacts/acme.device.chp-r1.device.yaml",
      "readme_url": "artifacts/acme.device.chp-r1.README.md",
      "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
      "size_bytes": 4096
    },
    {
      "id": "acme.solver.cbc_mps",
      "kind": "solver",
      "version": "1.0.0",
      "name": { "zh_cn": "CBC MPS 求解器", "en_us": "CBC MPS Solver" },
      "description": "消费标准 MPS 求解包并输出求解器无关原变量解。",
      "publisher": "acme",
      "license": "MPL-2.0",
      "compatibility": { "pies": ">=1.2.0,<2.0.0" },
      "consumes": ["ies.solver-bundle.mps@1.0.0"],
      "produces": ["ies.primal-solution@1.0.0"],
      "url": "artifacts/acme.solver.cbc_mps-1.0.0.algorithm.zip",
      "readme_url": "artifacts/acme.solver.cbc_mps-1.0.0.README.md",
      "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
      "size_bytes": 102400
    }
  ]
}
```

- **版本标识**：`kind: "device_model"` 使用正整数 `revision`；五类算法插件（`predictor` / `generator` / `solver` / `evaluator` / `algorithm`）使用三段式语义化 `version`；同一 `id` 的多个历史版本/修订并列保存在 `artifacts` 中。
- **下架隔离**：公共市场下架仅将对应条目从远端可分发索引移除（或标记停止分发），**绝不远程自动撤销或破坏各 pIES 实例本地已安装的制品**。

### 极薄无状态投稿网关协议

投稿网关不引入数据库或账号系统，以 GitHub PR 本身作为持久化审核记录：

1. **请求验证码**：提交邮箱地址（受发送频率与滑动窗口限流保护），经 SMTP 发送短期一次性验证码；
2. **无状态验权**：核验验证码后签发带服务端 HMAC 签名、用途绑定与短期过期时间的无状态投稿凭证；
3. **上传与前置校验**：携带短期投稿凭证上传 `.device.yaml` 或 `.algorithm.zip` 及投稿说明，网关执行归档安全、公开契约静态校验及 `publishers.yaml` 邮箱 HMAC 摘要归属核对（新 `<publisher>` 校验未被占用，既有 `<publisher>` 校验邮箱摘要一致）；
4. **幂等创建 PR**：网关使用最小权限 GitHub App / Token 检查同一 `(id, version/revision)` 无重复未决 PR 后自动创建分支与 PR；**投稿邮箱仅用于验权与生成非明文绑定摘要，默认绝不写入公开 PR 正文或 `index.json`**；
5. **只读隔离自检与人工审核合并**：PR 触发只读 `pull_request` 工作流（禁用 `pull_request_target`，执行环境零 GitHub 写权限、零 SMTP/发布密钥，自检容器全程 `--network=none`）；人工审核合并后，由独立受信任发布工作流打包计算 `sha256` 并重生成 `index.json` 与纯静态目录页。

## 设备模型库规范

1. **纯技术语义与双模编辑**：遵循[设备模型 YAML](formats/device-model-yaml.md)，仅含稳定标识、非时变 `properties`、五类 `interfaces`、可选模板 `inputs` 与声明式 `equations`，严禁夹带价格、成本、精度或代码路径。「我的创作/草稿」提供常用字段表单与在线 YAML 双向同步及 `.device.yaml` 导入/导出，统一经后端权威校验。
2. **校验门禁、面板可见性与版本隔离**：未通过完整校验的候选仅留在草稿区；已安装且启用的模型出现在项目新建模型选择器中，而历史项目版本恢复后仅展示该版本自身声明的模型引用。

## 算法插件单链契约、构建信任边界与运行约束

1. **严格单趟单向单链与可替换兼容**：遵循[算法插件包](formats/algorithm-plugin-package.md)（`plugin.yaml`），五类插件按 `冻结输入 → [predictor] → (generator → solver | algorithm) → [evaluator] → 结果` 单趟单向执行：
   - **上一环出口即下一环唯一进口**：禁止跨级挂载上游目录，禁止跨容器往返循环；跨链映射与冻结财务/评价上下文通过显式 `passthrough` 字段由中间环节**原样透传**；
   - **多目标单趟同源联合预测**：分配给同一 `predictor` 插件的多个预测目标一次性送入同一容器联合预测（保留共享气温/日照等潜变量相关性），含内部迭代寻优的算法统一在单容器内部经 `iteration` 完成；
   - **内置通用正向财务评价与契约匹配**：内置正向工程财务 `evaluator` 基于标准工程解（或由透传 `variable_map` 归一化的原变量解）与透传财务配置确定性计算完整财评，无差别通吃任意兼容 `solver` 与 `algorithm`（`evaluator: null` 时直接交付上游结果）；插件组合兼容以公开输入/输出契约标识（`consumes` / `produces`）判定，`CalculationConfig.options` 按环节分槽独立校验，不互相写死插件 ID。
2. **构建阶段信任边界与依赖源规则**：
   - 无 `requirements.txt` 的插件强制以 `--network=none` 直接基于本地 `pies-plugin-base` 构建；
   - 含 `requirements.txt` 的插件在 `job_runner` 内存受控上下文中构建，**严禁挂载宿主机业务目录、严禁注入数据库/Redis/密钥等业务凭证，并受硬性构建超时约束**；构建优先使用管理员配置的 `pip_index_url`（留空走默认 PyPI，本地模式仅关闭公共市场而不人为封锁 `pip`）；若离线环境拉取依赖失败，后台标记 `build_failed` 并在界面明确弹窗提示具体缺失依赖/插件名称及“请联系管理员安装或配置内网依赖源”。
3. **安装期断网自检门禁与状态区分**：镜像构建成功后，若 `plugin.yaml` 声明了 `tests`（同时支持单 JSON 简写与 `in/` → `expected/` 完整目录多文件样例），强制经 `job_runner` 在 `--network=none` 容器内全部跑通后才将后台状态由 `building` 置为 `ready`（失败置为 `build_failed`）。**自检通过仅代表通过作者样例与公开契约格式检查，平台不对任何第三方插件的工程最优性或财务正确性背书**。
4. **极简 `pies-sdk`**：保持零后端依赖定位，仅提供运行时契约/入口及覆盖全部五类插件的通用 `validate`（清单、归档安全及单文件/目录级离线自检）与 `pack`（打标准 `*.algorithm.zip` 并输出 `sha256`）命令。

## 正交运行模式、审批开关与无状态卸载保护

管理员在 `/admin` 维护两项**相互正交**的实例级策略：

| 管理设置 | 控制范围与生效规则 |
|---|---|
| `market_mode`（`local` / `online`） | 控制是否连接远端 `pIES-Market`：本地模式零市场出网请求、隐藏远端条目并禁用外网投稿；联网模式开放远端浏览、下载与邮箱验证投稿 |
| `local_upload_requires_approval`（`true` / `false`） | **在本地模式和联网模式下统一生效**：联网模式下从已审核公共市场直接下载的制品免本地审批；但**任何非管理员成员自行上传或离线导入**的本地制品均受本开关约束（开启时仅做静态安全与契约校验并进入 `pending_approval` 队列、不触发构建，批准后才转入 `building → ready`；关闭时直接进入构建与上架；管理员自身操作免审），彻底杜绝联网模式下借离线导入绕过企业审批 |

**两步无状态卸载与在途任务保护**：

- **停用与下架（立即生效）**：成员取消安装或管理员停用/卸载本地条目时，立即解除目录可用引用，阻止后续新项目选择与新任务提交；
- **物理清理镜像（无状态在途检查）**：管理员请求清理插件镜像时，系统实时检查是否存在未终结任务（`queued` / `running` / `cancelling`）的冻结快照引用该 `(plugin_id, version)`——无在途引用时立即经 `job_runner` 白名单操作 `delete_plugin_image` 清理带 `pies.managed="true"` 标签的 `pies-plugin/*` 镜像（严禁触碰 `pies-builtin-stages` 与 `pies-plugin-base`）；若仍有 $N$ 个在途任务引用，则**无状态拒绝当场删镜像**并明确提示“已下架阻止新任务，当前仍有 $N$ 个在途任务使用中，请待任务结束后再执行镜像清理”，不维护后台待删状态机；
- **对象引用保护**：仍被历史项目版本、任务快照或证据引用的对象存储包受 `ObjectOwner` 严格保护不得物理删除，卸载前明确提示引用影响。

## 外部交付自检清单

- **提交设备模型前**：确认通过 `1.0.0` Schema 校验、不含财务/精度/代码字段、具备可读 `<publisher>`（投稿市场时）、许可证与合法样例。
- **提交算法插件前**：运行 `pies-sdk validate` 与 `pies-sdk pack` 确认 ZIP 目录安全、清单与 `consumes`/`produces` 声明合规及离线自检样例确定性通过；向公共市场投稿时附带清晰 `README.md`、明确许可证与至少一个无网络依赖的 `tests` 样例。
