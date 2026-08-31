# 已归档：模型与算法 AI 开发功能要求

> 状态：已归档
> 归档日期：2026-08-31
> 归档原因：本文曾把长期产品/架构设计、版本实施顺序和 AI 交接要求混为同一权威来源，存在与正式手册和 Roadmap 分叉的风险。
> 替代来源：长期模型与算法规则见 [模型与算法扩展指南](../../../manual/developer-guide/zh-CN/customization-center.md) 及对应格式/模块手册；版本顺序见 [Roadmap](../../../manual/changelog/roadmap.md)；AI/人工实施交接见 [`docs/development/model-and-algorithm-ai-requirements.md`](../../development/model-and-algorithm-ai-requirements.md)。
>
> 以下原文仅保存当时的设计与交接证据，不再作为现行开发输入或规范。

## 1. AI 执行指令

本文件用于把“模型库、算法插件、共享园地”交给 AI 分切片实现。开始任何切片前必须依次阅读：

1. [架构宪法](../../manual/developer-guide/zh-CN/ARCHITECTURE_CONSTITUTION.md)；
2. [模型与算法](../../manual/developer-guide/zh-CN/customization-center.md)；
3. [设备模型 YAML](../../manual/developer-guide/zh-CN/formats/device-model-yaml.md)与[算法插件包](../../manual/developer-guide/zh-CN/formats/algorithm-plugin-package.md)；
4. 本切片涉及的模块手册；
5. Roadmap 当前顺序与 Git 工作区状态。

本功能位于 `1.1.0`。除非项目所有者明确调整 Roadmap，不得在 `0.x` 主线未完成时提前实现。每次只实现本文件定义的一个切片；通过 review 后立即单独 Git 提交。使用子 agent 时必须使用独立工作树，禁止多个 agent 在同一工作树交叉修改。

所有依赖安装、代码生成、migration 验证、编译、测试、格式化和浏览器验收只能在 Docker 中运行。测试产生的非基础镜像在验收完成后清理。画布拖放只由人工验收，画布目录加载、参数设置和其他交互可用 Playwright。

## 2. 已确认产品决策

以下事项已经由项目所有者确认，实现中不得再次改写：

- 一级导航名称为“自定义”；
- 页面固定有“模型库”“算法插件”“共享园地”三个入口；
- 模型最终必须落为 `ies.device-model` YAML；
- 模型可从网页结构化表单、在线 YAML 编辑或上传 YAML 创建，三者必须汇合为同一草稿与规范内容；
- 所有完成设备模型使用 `schema/schema_version/device/properties/interfaces/equations` 统一纯技术格式，设备文件不声明独立语义版本；未实例化编辑状态可按文件格式规范暂含 `inputs`，实例化后必须移除再执行完整模型校验；
- properties 只保存非时变技术常量；interfaces 只保存序列交互并使用 `in/out/bidirectional/predefined/blind` 五种类型；
- predefined 只允许 `constant/data_repeat/data_predict`，blind 不连接且不接收预定义数据；
- 用户公开命名空间首次使用时由系统生成 12 位小写 Crockford Base32 标识（60 bit CSPRNG），全局唯一、终身不变、不可转让且不复用；用户只选择 slug，客户端不能指定命名空间；
- 序列重采样按公共物理量语义固定为三类：瞬时/强度量使用时间加权平均或线性插值，区间累计量使用求和或按时长比例分配，状态/离散量使用区间末值或前向保持；语义不明确时阻断；
- `data_predict` 唯一固定算法为 `ies.predict.ridge@1.0.0`：显式训练输入、训练目标和预测输入，特征按训练集均值/总体标准差标准化，零方差映射为 0，`alpha=1.0` 且不惩罚截距，每个目标独立训练；
- 设备文件不得包含计算精度、价格、成本或财务假设，技术关系只通过受限声明式 equations 表达；
- 模板实例化、结构化表单和直接 YAML 编辑产生的都是候选模型；候选必须在进入项目正式模型目录前通过后端完整校验，失败不保存、不进入装配且不占用 `_N` 正式编号；
- 算法不在网页中编写，通过 ZIP 插件包上传；
- 第一版插件包同时包含规范装配加工程序、算法程序、依赖锁、输入输出 schema 和测试样例；
- 第一版只支持 Python ZIP，不支持原生二进制、自带镜像或在线依赖；
- 用户自己的模型和算法不需要管理员批准即可进入自己的目录；
- 所有者可以申请共享某个设备不可变内容 revision 或算法插件精确版本，只有管理员批准后才进入共享园地；
- 其他用户从共享园地安装时只创建逻辑引用，不能复制底层对象；
- 已安装且后端判定可用的模型和算法，分别进入系统建模和计算配置的选择器；
- 用户插件只能由 Docker 部署中的独立隔离 runner 执行，API 和普通 Worker 不得加载用户代码；
- 禁止把 Docker Socket 挂给普通 Worker 或插件 runner。

## 3. 交付范围

### 3.1 必须交付

- 新一级路由与三个入口的桌面、移动和键盘体验；
- 模型草稿、三种作者方式、校验、不可变内容发布、停用与安装目录；
- 算法插件上传、静态校验、环境构建、隔离自检、不可变版本与停用；
- 用户目录、共享申请、管理员审批、共享浏览、引用安装与卸载；
- 统一可选择目录，以及系统建模/计算配置接入；
- 插件任务快照、独立 runner、资源限制、取消/超时和证据；
- 对象去重、引用生命周期、审计、诊断、配额和运维状态；
- OpenAPI、前端 contract、migration、Docker 测试、帮助中心和更新日志同步。

### 3.2 明确排除

- 用户间私下分享、项目成员共享、付费市场、评分、评论和推荐算法；
- 管理员代用户修改私有内容；
- 网页编写算法源码；
- 原生二进制、C 扩展、系统包、自带容器、Git 安装和公网依赖下载；
- 用户插件直接启动 shell、任意子进程或嵌套容器；
- 自动升级已安装版本；
- 对共享内容做实质性复制；
- 用 Python `venv` 单独充当安全边界；
- 修改架构宪法。若实现证明必须修改，停止并向用户申请，不得静默修改。

## 4. 信息架构与路由

目标前端路由：

| 路由 | 页面 | 主要动作 |
|---|---|---|
| `/customizations/models` | 模型库 | 新建、表单/YAML 编辑、上传、校验、发布不可变内容、申请共享、停用 |
| `/customizations/plugins` | 算法插件 | 上传 ZIP、查看校验/环境/自检、申请共享、停用 |
| `/customizations/shared` | 共享园地 | 搜索、筛选、查看精确发布、安装/已安装状态 |

一级导航点击“自定义”默认进入 `/customizations/models`。三个入口在桌面端使用可见页签或侧栏，在移动端使用可键盘操作的折叠导航；URL 必须可深链接和刷新恢复。

目标 feature：

```text
frontend/src/features/customizations/
├── api.ts
├── contracts.ts
├── model.ts
├── form.ts
├── mappers.ts
├── hooks/
└── components/
```

页面只组合 feature 对外接口。不得继续把 DTO 拼接、YAML mapper、上传状态或共享缓存堆入 `App.tsx`、通用 `api/client.ts` 或页面组件。

## 5. 后端边界

目标用例目录为 `backend/iesplan/application/customizations/`。它负责编排目录、草稿、不可变发布、共享、安装、审计和对象引用，但不自行实现设备 YAML 语义或插件运行。

职责分配：

| 代码边界 | 必须负责 | 禁止负责 |
|---|---|---|
| `devices` | YAML 安全解析、schema、properties/interfaces/equations 校验、规范化和 descriptor | 用户权限、共享审批、价格与计算精度 |
| `computation` | 插件 manifest、跨进程 contract、通用沙箱 generator/executor/result adapter | HTTP、用户目录 ORM |
| `storage` | ZIP/YAML/报告对象、摘要、owner 引用和去重 | 理解共享状态 |
| `application/customizations` | 授权、revision、事务、安装/共享生命周期 | import 插件、复制领域校验 |
| `api` | DTO、multipart、限流、状态码、错误适配 | 解压或执行插件 |
| `worker` | 插件快照、标准计算链编排、租约和证据提交 | import 插件、绕过 provider 或降级本地执行 |
| `plugin_runner` | 环境构建、静态扫描后的动态自检和受限运行 | 数据库、项目权限、共享审批 |

用户算法包不是 provider 注册项。组合根只注册通用 `SandboxedAlgorithmGeneratorProvider`、`SandboxedAlgorithmExecutorProvider`、`UserAlgorithmResultAdapterProvider` 和 runner client/sandbox adapter。前者通过 runner 执行加工入口并生成标准 Solver Bundle，后者由 SolverRuntime 调用 runner 执行算法入口，再由结果适配器形成 `ComputeResult`。这是避免运行期热加载并保持标准计算链的强制设计，不得用“更方便”为由让用户包进入模块注册表或让 Worker 绕过 provider。

## 6. 持久化要求

建议表名可以按现有命名规范调整，但语义必须保持：

### `custom_entries`

- `id`、`owner_user_id`、`kind(model|algorithm_plugin)`；
- `stable_id`、显示名称、描述、分类；
- 当前草稿 revision、创建/更新时间、停用时间；
- 首次创建时按已确认规则分配 12 位公开随机命名空间，稳定 ID 使用 `user.<public_namespace>.<kind>.<slug>`；
- `stable_id` 全系统唯一，所有者不能声明内置或其他用户的命名空间；
- 不保存底层文件副本。

### `custom_draft_revisions`

- `entry_id`、严格递增 revision；
- 模型草稿保存 YAML 对象 ID/摘要；插件不允许在线源码草稿；
- 创建来源 `form|yaml_editor|upload|derived`；
- 不覆盖旧 revision。

### `custom_publications`

- `entry_id`、`stable_id`、严格递增 publication revision、`schema`、`schema_version`；
- 设备 publication 不含设备语义版本；算法插件 publication 必须含精确 `semantic_version`；
- 内容对象 ID、内容 SHA-256、规范摘要或 ZIP 摘要；
- 依赖锁、descriptor/manifest 摘要、创建来源和创建者；
- immutable 标记与数据库禁止更新约束；
- `(entry_id, publication_revision)` 唯一；算法插件另保证 `(stable_id, semantic_version)` 全局唯一，内容冲突返回 409；
- 相同设备规范内容重复发布幂等返回已有 publication。

### `custom_validation_reports`

- 绑定精确 publication 或 draft revision；
- 校验器/runner contract/Python runtime 精确版本；
- 阶段、结论、诊断、资源统计和报告对象摘要；
- 报告不可变，不把完整源码、秘密或无限日志存入普通字段。

### `catalog_installations`

- `user_id`、`custom_publication_id`、来源 `owned|shared_garden`；
- 安装/卸载时间和状态；
- 活跃 `(user_id, custom_publication_id)` 唯一；
- 安装事务中 attach storage owner，卸载事务中 detach；
- 禁止保存 YAML、ZIP、报告或依赖副本。

### `share_requests`

- `custom_publication_id`、申请者、申请摘要、许可证、公开说明；
- 包/模型摘要快照；
- `pending|approved|rejected|withdrawn|unshared`；
- 审核者、审核说明和时间；
- 同一 publication 最多一个活跃 pending；
- 审批后申请内容不可替换，重新申请创建新记录。

运行环境缓存只记录可重建索引，权威身份由插件包摘要、依赖锁摘要、runner contract 和 Python runtime digest 计算。清理缓存不能改变 `selectable` 之外的历史事实；需要时可以重建。

## 7. 状态与业务规则

### 7.1 模型发布

```text
draft(revision N)
  → validate
  → invalid（保留诊断和草稿）
  → private_ready publication（不可变，所有者自动安装）
```

校验和发布不能分成“校验旧 revision、发布新 revision”的竞态。发布命令携带 `expected_revision`，同一事务核对 revision 和校验输入摘要。

项目模型保存同样使用单一校验并提交用例：候选 YAML 与临时配套文件先完整校验，成功后才在项目范围内分配只递增、不复用的 `_N` ID，并原子提交规范 YAML、配套文件、摘要、回执和项目模型清单引用。blocking 诊断、写入失败或并发冲突均不得留下正式文件、孤立引用或被消耗的用户可见编号。

### 7.2 插件发布

```text
uploaded
  → static_validating
  → environment_building
  → sandbox_testing
  → private_ready（所有者自动安装）
  ↘ invalid / environment_failed / test_failed
```

上传返回 202 与任务 ID。只有最终不可变报告成功才自动建立所有者安装引用。失败包保留在配额和保留策略内供查看诊断，不能出现在选择器。

### 7.3 共享

```text
private_ready
  → pending
  → approved(shared) / rejected
shared
  → unshared（停止新浏览和安装，既有安装保留）
任意可运行版本
  → blocked（安全阻断，新任务禁止，历史只读）
```

批准绑定精确 publication + content hash。设备新 revision 或插件新版本重新申请。管理员不能批准校验失败或许可证缺失的 publication。

### 7.4 安装与选择

- 所有者发布成功后自动得到 `owned` 安装引用；
- 共享安装幂等，重复 POST 返回原安装而不增加引用；
- 共享园地取消共享后，既有 `shared_garden` 安装仍存在；
- 用户显式卸载后不再进入新项目选择器；
- 已被项目草稿引用时，卸载需返回影响预览并要求显式确认；
- 已被项目版本/快照/证据引用时保留历史 owner 引用；
- `selectable` 由后端计算，至少检查安装、校验、依赖、runner、兼容性、停用/阻断和对象完整性。

## 8. HTTP API 草案

具体 Pydantic/OpenAPI 是字段事实源。实现前可以细化字段，但不得改变资源语义。

### 模型库

| 方法与路径 | 语义 | 成功 |
|---|---|---|
| `GET /api/customizations/models` | 当前用户模型目录 | `{items, next_cursor}` |
| `POST /api/customizations/models` | 创建模型条目与初始草稿 | 201 `{model, draft}` |
| `GET /api/customizations/models/{id}` | 条目、草稿、publications、安装与共享状态 | `{model, draft, publications}` |
| `PUT /api/customizations/models/{id}/draft` | 用 expected_revision 完整替换 YAML 草稿 | `{draft, diagnostics}` |
| `POST /api/customizations/models/import` | multipart 上传 YAML 并创建/更新草稿 | 201 `{model, draft, diagnostics}` |
| `POST /api/customizations/models/{id}/validate` | 校验指定 revision，不发布 | `{ok, report}` |
| `POST /api/customizations/models/{id}/publications` | 校验并发布不可变内容 revision | 201 `{publication, installation, report}` |

表单模式不需要独立后端模型格式；前端把 form 映射为 YAML 草稿再调用同一 PUT。

### 算法插件

| 方法与路径 | 语义 | 成功 |
|---|---|---|
| `GET /api/customizations/plugins` | 当前用户插件目录 | `{items, next_cursor}` |
| `POST /api/customizations/plugins` | multipart 上传 ZIP、固定对象并创建校验任务 | 202 `{plugin, task}` |
| `GET /api/customizations/plugins/{id}` | 插件 publications、报告、环境、安装与共享状态 | `{plugin, publications, reports}` |
| `POST /api/customizations/plugins/{id}/revalidate` | 对同一精确包重跑兼容性/自检 | 202 `{task}` |

### 共享园地与管理

| 方法与路径 | 语义 | 成功 |
|---|---|---|
| `POST /api/customizations/publications/{id}/share-requests` | 所有者申请共享设备 publication 或插件精确版本 | 201 `{share_request}` |
| `POST /api/customizations/share-requests/{id}/withdraw` | 所有者撤回待审申请 | `{share_request}` |
| `GET /api/shared-garden` | 浏览已批准精确 publication | `{items, next_cursor}` |
| `GET /api/shared-garden/{publication_id}` | 公开详情、报告摘要和安装状态 | `{item, installation}` |
| `POST /api/shared-garden/{publication_id}/install` | 幂等建立当前用户目录引用 | 201/200 `{installation}` |
| `GET /api/customizations/installations/{id}/uninstall-preview` | 返回草稿/历史引用影响与短期确认令牌 | `{preview}` |
| `DELETE /api/customizations/installations/{id}` | 携带确认令牌卸载目录引用 | 204 |
| `GET /api/admin/share-requests` | 管理员待审列表 | `{items, next_cursor}` |
| `POST /api/admin/share-requests/{id}/approve` | 批准精确 publication 共享 | `{share_request}` |
| `POST /api/admin/share-requests/{id}/reject` | 驳回并记录说明 | `{share_request}` |
| `POST /api/admin/shared-publications/{id}/unshare` | 停止新浏览/安装 | `{publication, share_status}` |
| `POST /api/admin/custom-publications/{id}/block` | 安全阻断新任务 | `{publication, blocked}` |

### 选择目录

| 方法与路径 | 语义 |
|---|---|
| `GET /api/catalog/device-models` | 系统内置 + 当前用户已安装且可选择的模型 |
| `GET /api/catalog/algorithm-plugins` | 系统内置 + 当前用户已安装且可选择的算法能力 |

选择目录响应必须包含 `source(system|owned|shared_garden)`、稳定 ID、设备 publication revision/插件精确版本、摘要、兼容性和 `selectable`/不可选诊断。不得要求前端分别请求注册表、用户目录和共享园地后自行合并。

所有端点先登记允许的一级包装键。上传大小/配额用 413，业务内容校验用 400，请求 schema 用 422，revision/插件同版本不同内容/状态冲突用 409，权限用 403。卸载确认令牌必须绑定 installation、影响清单、操作者和短期过期时间，清单变化后拒绝执行。当前架构宪法没有 `CUSTOM` 诊断域；未经用户批准修改宪法前，应使用现有 `API`、`CONFIG`、`TASK`、`OBJ`、`PERM` 等权威域，不得私自发明不合规前缀。

## 9. 模型编辑器要求

- 表单字段完全来自设备模型 schema，不硬编码设备类型；
- 表单、YAML 编辑器和上传预览共享一个 draft query key；
- 每次切换编辑方式前检查未提交更改和可逆映射；
- YAML 编辑器支持行列诊断、查找和格式化，但不执行模板或任意表达式；
- 上传前显示文件名、大小和本地摘要，服务端摘要为权威；
- 校验错误定位到字段路径或 YAML 行列，保留完整诊断总览；
- 项目模型校验失败时保留编辑内容和临时上传状态，但不显示已保存、不预分配 `_N` ID，也不允许进入装配；
- 设备发布对话框要求变更说明并显示 publication revision、摘要和校验回执，不要求设备语义版本；插件发布仍要求精确版本；
- 发布成功后草稿不被静默清空，可选择从该 publication 派生下一草稿；
- 同名显示名允许，设备以稳定 ID + 内容摘要唯一，插件以稳定 ID + 精确版本唯一；
- 系统内置模型只读显示，不允许在用户库中伪装覆盖同 ID。

## 10. 插件安全要求

### 10.1 上传阶段

- 流式读取并同时计算 SHA-256，不把完整 ZIP 读入普通请求内存；
- 检查 MIME、后缀、压缩目录、条目数、压缩比、单文件和总解压上限；
- 规范化每个 ZIP 路径后再判断重复、逃逸、大小写碰撞和保留名；
- 拒绝符号链接、硬链接、设备文件、加密条目和嵌套归档；
- 原始包先进入 storage 隔离用途引用，不解压到仓库或长期共享目录；
- API 请求只创建对象和异步校验任务，不 import、安装依赖或运行测试。

### 10.2 环境构建

- 只使用部署批准的 Python runtime 和离线 wheel 仓库；
- 锁文件每个依赖必须带哈希，拒绝 sdist、URL、VCS、editable 和本地路径；
- 环境身份含包、锁、runner contract 和 runtime 摘要；
- 环境目录只读，构建日志限长且不泄露内部路径；
- 构建失败形成报告，不回退主机环境或未锁版本；
- 环境缓存可删除、可重建、按摘要共享，不按用户复制。

### 10.3 动态执行

- 独立 `plugin_runner` 容器以非 root 运行，根文件系统只读；
- 不挂载 Docker Socket、仓库、业务 `/data`、数据库或宿主机目录；
- 用户子进程获得空白 allowlist 环境和独立临时工作目录；
- 默认禁止外网和业务内网；
- 强制 CPU、内存、进程数、打开文件数、写入字节、日志和超时；
- 禁止 shell、额外子进程、动态模块下载和未声明路径；
- runner 只接收固定字节/摘要，不接收 Cookie、DB Session 或对象存储长期凭证；
- 取消、超时、OOM、系统调用拒绝和 runner 崩溃分别形成稳定失败；
- 普通 Worker 绝不在 runner 不可用时本地降级执行，也不把加工与求解合并成一条绕过 Solver Bundle 的捷径。

Docker 容器本身是部署边界，但长驻 runner 内仍需对子进程做文件、环境、资源和系统调用约束。不得把“已经在 Docker 里”作为取消内部隔离的理由。

## 11. 快照与证据

插件计算快照必须新增并固定：

- 插件稳定 ID、精确版本和 ZIP SHA-256；
- manifest、依赖锁和环境 SHA-256；
- runner contract、Python runtime 和 sandbox adapter 精确版本；
- 加工入口、算法入口和结果 schema/adapter；
- 规范选项、随机种子、停止条件和实际资源上限；
- 输入装配与每个资源摘要。

每个 attempt 证据必须包含加工输出 manifest、生成文件摘要、隔离执行回执、限长日志摘要、算法输出摘要、结果适配报告和实际资源统计。历史读取不要求当前用户仍安装该版本，也不受共享状态变化影响。

## 12. 分切片实施计划

以下切片按顺序执行。每个切片必须独立 review、Docker 验证并提交；不得合并为一个超大变更。

### 切片 1：公共 contract 与 schema

- 新增 CustomEntry/Publication/Installation/ShareRequest 应用 contract；
- 发布算法插件 manifest、JSON Schema、合法/非法样例和纯校验器接口；
- 定义 runner 内部请求、响应、环境身份和执行回执；
- 只做纯 contract/schema 测试，不接 API、ORM 或 UI。

退出：所有 schema 与示例可在 Docker 中校验，非法路径/字段/版本有稳定诊断。

### 切片 2：持久化与对象引用

- migrations、repository、唯一/不可变约束；
- 安装引用 attach/detach 与内容去重；
- 并发发布、重复安装、卸载和对象 reconciliation 测试。

退出：100 个用户安装同一 publication 仍只有一个内容对象，引用与清理正确。

### 切片 3：模型库后端

- 模型条目、草稿 revision、上传、校验和不可变 publication 用例/API；
- 复用 devices 唯一 YAML 解析与规范化器；
- 所有者发布后自动创建安装引用。
- 增加项目候选模型“完整校验 → 分配 `_N` → 规范化/摘要 → 模型及配套文件原子保存”用例，失败不产生正式项目文件。

退出：表单等价 YAML 与上传 YAML 产生相同规范摘要；竞态发布被 409 阻断；非法字段类型、interface/source、方程或配套文件均不会占号或落入项目正式目录。

### 切片 4：模型库前端

- 一级导航、三个入口骨架；
- 模型列表、表单、YAML 编辑、上传、诊断、发布和 publication 详情；
- contract/form/model/mapper 分离及 Playwright 非拖放验收。

退出：三种作者路径共享草稿且无字段静默丢失，刷新/冲突/离线可恢复。

### 切片 5：模型选择接入

- 后端统一设备模型 catalog；
- 画布设备目录消费系统 + 当前用户安装模型；
- 保存稳定 ID、publication revision 与内容摘要，装配和项目快照固定摘要。

退出：未安装/不可用模型不可选择；property/interface 配置浏览器验收通过；拖放人工验收。

### 切片 6：插件上传与静态校验

- ZIP 流式上传、配额、归档安全、manifest/锁/schema 校验；
- 创建异步校验任务和报告，不执行用户入口；
- 算法插件列表与详情 UI。

退出：压缩炸弹、路径逃逸、符号链接、缺哈希依赖和错误入口在运行前阻断。

### 切片 7：独立 plugin runner

- Docker 服务、内部协议、离线环境构建和环境缓存；
- 非 root、只读、临时目录、无业务凭证、网络和资源限制；
- 最小样例动态自检与执行回执。

退出：runner 可执行合法最小包；网络、越权文件、资源超限、超时和取消测试通过；无 Docker Socket。

### 切片 8：插件任务与算法选择接入

- 普通 Worker 固定插件快照，通用沙箱 GeneratorProvider 调 runner 加工并生成 Solver Bundle，SolverRuntime/ExecutorProvider 再调 runner 求解；
- 加工→算法→结果适配→证据全链路；
- 统一算法 catalog 与计算配置选择器。

退出：至少一个用户插件完成 Docker 端到端任务；runner 不可用时明确失败且不本地降级。

### 切片 9：共享申请与管理员审批

- 分享申请、撤回、管理员列表、批准/驳回/停止共享/阻断；
- 设备 publication/插件精确版本与摘要绑定、权限、冲突、审计；
- 管理员 UI。

退出：他人私有内容不可见；审批不能作用到新上传同版本不同摘要；全部管理动作有审计。

### 切片 10：共享园地与引用安装

- 浏览、筛选、详情、安装和卸载；
- 安装状态与用户目录、模型/算法 catalog 联动；
- 并发幂等、对象引用和存储去重验证。

退出：共享 publication 安装后进入对应选择器；取消共享不破坏既有安装；存储不随安装人数增长。

### 切片 11：生命周期与历史兼容

- 停用、停止共享、安全阻断、影响预览和明确卸载；
- 项目版本、任务快照、证据和导出历史读取；
- 环境缓存清理与重建。

退出：共享/安装状态变化不改变历史解释；blocked 禁止新任务但历史可读。

### 切片 12：全量验收与文档发布

- OpenAPI/前端 contract、权限、安全、并发、存储、runner 和 E2E 全量测试；
- 运维健康、容量、备份恢复和故障演练；
- 按实际控件更新使用者指南和更新日志，从 Roadmap 删除已完成事项。

退出：本文件第 14 节全部门禁通过，review 无阻断，独立功能提交历史完整。

## 13. 重点测试矩阵

| 层级 | 必测场景 |
|---|---|
| 纯 schema | 合法/非法 YAML、manifest、锁文件、路径、版本、非有限数值 |
| repository | 唯一冲突、revision、不可变、安装幂等、审批竞态、引用恢复 |
| application | 所有者/他人/管理员权限，发布、安装、卸载、共享和阻断状态机 |
| API | 401/403/404/409/413/422/429/500，包装键，multipart 流式摘要 |
| storage | 相同摘要去重，多用户引用，卸载/历史引用/清理/reconciliation |
| runner | 无网络、无凭证、路径逃逸、进程/内存/CPU/文件/日志/超时/取消 |
| Worker | 快照固定、租约、runner 失联、重试、迟到写入拒绝、证据摘要 |
| 前端 | 加载/空/失败/冲突/离线、三种模型作者方式、上传进度、审批与安装 |
| 集成 | 自定义模型建模、插件计算、共享安装、停止共享、历史结果读取 |

安全测试必须包含一个主动读取 `/etc/passwd`、环境变量、业务网络、仓库路径和超限写文件的恶意测试插件；期望是受控拒绝或只看到沙箱允许内容，不能只测试“正常插件不会这样做”。

## 14. 总体验收门禁

- 导航文字和三个入口与本文件一致；
- 模型三种入口产生同一权威 YAML，不存在数据库专用第二格式；
- 插件 ZIP 格式、校验、环境和运行证据可独立复现；
- 用户无需管理员审批即可使用自己的合法内容；
- 只有设备精确 publication 或插件精确版本经管理员批准才进入共享园地；
- 安装是引用，不复制底层对象或环境；
- 模型和插件只在已安装且 `selectable=true` 时进入对应步骤；
- API、普通 Worker 和 provider registry 无用户代码加载路径，且插件仍经过 GeneratorProvider、Solver Bundle、SolverRuntime/ExecutorProvider 和 ResultAdapter；
- plugin runner 在 Docker 内具备非 root、只读、无敏感挂载、无 Docker Socket、限资源和默认断网证据；
- 快照与历史证据固定内容、环境和 runner 身份；
- 权限、审计、诊断、冲突、配额、清理和恢复测试完整；
- 全部验证在 Docker 内完成，测试镜像已按仓库规则清理；
- 开发者指南、使用者指南、OpenAPI、Roadmap/更新日志与实际行为一致。

## 15. Review 阻断项

出现以下任一情况，review 必须判定为阻断：

- 在 API、普通 Worker、前端构建或主应用启动流程 import/执行用户包；
- 向容器挂 Docker Socket 或把业务 `/data`、仓库、数据库凭证暴露给用户插件；
- 模型表单与 YAML 使用两套无法证明等价的数据结构；
- 共享安装复制内容文件、依赖环境或校验报告；
- 项目保存显示名、列表下标或 `latest`，未固定设备 publication 摘要或插件精确版本；
- 管理员批准一个条目后让后续版本自动继承共享；
- 停止共享或卸载破坏历史项目、任务或证据；
- runner 失败时回退到普通 Worker 执行；
- 绕过 Solver Bundle，或在 Worker/runner 编排层把加工、执行和结果适配合成未版本化私有流程；
- 前端合并多个私有注册表并自行推导 `selectable`；
- 用静态扫描替代隔离，或用 Python venv 单独宣称安全；
- 未经用户同意修改架构宪法；
- 在主机环境安装依赖、编译或测试。
