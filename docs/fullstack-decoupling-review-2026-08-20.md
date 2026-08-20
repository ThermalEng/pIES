# 前后端解耦与后端注册表统一审查结论

> 审查日期：2026-08-20
> 最后更新：2026-08-21
> 审查范围：后端设备/建模注册表边界、装配检查注册表使用方式、前后端 API 契约、后端五段式重构后的前端适配情况、对象存储与业务对象生命周期，以及项目文档分区和入口。
> 审查方式：静态调用链核对、Git 变更核对、Docker 内编译与相关后端测试。
> 本文件合并：前序后端审查的 3 项 P2 问题、本次前后端适配审查发现的 6 项问题，以及存储专项审查结论。

## 1. 总体结论

后端按“设备初始化 → 建模 → 装配与检查 → 计算 → 结果分析”分层的方向合理，但各模块尚未通过稳定的公开协议传递设备规格、建模命令和装配要求。当前系统实际并存三套设备语义：

1. `devices.DeviceRegistry` 中的运行时 YAML 规格；
2. `core.registry` 中的旧静态设备规格；
3. 前端 `canvasModel.ts` 中的硬编码端口规则。

因此，新增设备插件后，可能出现“命令执行层认识、API/装配/前端不认识”或各层端口定义不同的情况。各模块内部的注册替换也不是原子操作，失败时可能破坏原有可用命令。

前端曾在提交 `784e8f5` 中进行过一次大规模契约适配，但最新后端五段式重构提交 `0f5214e` 没有修改任何前端文件。新增的 `analysis` 任务、运行时设备注册表、设备端口声明和配置单位边界均未完整传到前端。前端适配层目前约 1800 行，已经承担草稿命令生成、乐观锁、财务假设拼装、评估触发、证据包反查和导出会话等业务编排，不再是单纯的传输适配层。

综合判定：**架构重构部分落地，但前后端尚未完成适应性修改，当前不宜认定为已解耦。** 优先修复财务配置、设备端口连线、人工评估语义和注册表单一事实源后，再开放插件热加载与分析任务前端入口。

## 2. 问题汇总

| 编号 | 优先级 | 问题 | 主要影响 |
|---|---|---|---|
| BE-REG-01 | P2 | 建模模块绕过设备模块公开接口，重新读取其内部目录 | 模块边界被破坏，设备目录实现变化会直接影响建模模块 |
| BE-REG-02 | P2 | 建模命令注册表先清空再逐项注册，不具备原子替换语义 | 热加载失败后丢失旧命令，只剩部分注册状态 |
| BE-REG-03 | P2 | 装配检查捕获所有运行时注册表异常并回退静态表 | 权威注册表转换缺陷被掩盖，可能错误接受或拒绝模型 |
| FE-BE-01 | P1 | 财务配置前端使用旧扁平键，后端使用 `parameters.economic` | 页面修改显示成功但计算仍使用旧值 |
| FE-BE-02 | P1 | 前端按第一个 `in/out` 服务器端口连线，未按选中载能匹配 | 热泵冷端误连热端，电池双向端口无法连接 |
| FE-BE-03 | P1 | 人工评估输入在适配层被静默丢弃 | 用户误以为评分和评论已持久化，实际只触发系统评估 |
| FE-BE-04 | P2 | REST API、模型服务和前端未消费运行时 YAML 设备注册表 | 新插件无法可靠出现在画布或参与模型写入 |
| FE-BE-05 | P2 | 财务基准确认从错误的配置层级读取并提交默认值 | 非默认项目刚确认即被后端判为基准过期 |
| FE-BE-06 | P2 | 后端新增 `analysis` 任务，前端类型和提交入口未同步 | 新分析能力只能直接调用 API，页面不可用 |
| FE-DOC-01 | P2 | 顶部教程使用 TypeScript 硬编码正文，未渲染正式 Markdown 指南 | 文档形成多个事实源，用户和开发者无法从统一目录持续获取当前指南 |
| QA-E2E-01 | P2 | 缺少 Playwright 真实用户验收 | 编译和 API 测试通过仍无法发现导航、表单、画布、文档深链接及浏览器集成问题 |

## 3. 详细审查意见

### BE-REG-01：统一设备规格加载来源

位置：

- `backend/iesplan/main.py:121-125`
- `backend/iesplan/modeling/registry_loader.py:46-55`

API 启动时先调用 `init_registry()` 构造并验证运行时 `DeviceRegistry`，随后 `register_catalog_commands()` 又直接导入 `devices.loader/pricing/profile/spec` 并重新扫描设备模块内部目录。问题的本质不是缺少一个全局共享快照，而是建模模块绕过了设备模块的公开边界，依赖其文件布局和加载细节。

整改要求：

- `devices` 通过公开接口导出已验证的设备描述，不暴露目录扫描、价格解析和 CSV 路径规则；
- `modeling` 只接收公开 `DeviceModelDescriptor` 或插件提供的建模 provider，不导入 `devices.loader/pricing/profile`；
- 各模块独立拥有自己的注册表和原子替换逻辑，不建立跨模块全局注册表；
- 应用启动组合层只负责调用公开注册接口和检查引用是否可解析，不持有或合并各模块内部状态；
- 当前尚未正式发布，暂不提供运行期热加载；启动装配失败直接退出。

### BE-REG-02：原子替换建模命令注册表

位置：

- `backend/iesplan/modeling/registry_loader.py:55-80`
- `backend/iesplan/modeling/command.py`

当前流程先执行 `clear_commands()`，再初始化计算命令并逐设备调用 `build_command()`。任一设备构建或 profile 加载失败后，旧注册表已经被清空，进程中只剩计算命令或部分设备命令。

整改要求：

- 在临时 `dict[str, ModuleCommand]` 中完整构建并校验所有计算命令和设备命令；
- 校验命令 ID 唯一、函数可解析、profile 完整后，一次性替换全局快照；
- 失败时保留旧快照并上报 reload 失败，不能暴露部分新状态。

### BE-REG-03：不要把权威注册表错误静默降级为静态表

位置：

- `backend/iesplan/assembly/checker.py:225-247`

装配检查的注册表装载捕获过宽异常并回退 `core.registry.list_device_types()`。这会把 `to_registry_spec` 的实现错误、插件数据错误或运行时注册表损坏误判为“兼容场景”，使装配检查继续使用过期定义。

整改要求：

- 仅对明确的“注册表未初始化”异常提供兼容回退；
- 转换异常、插件异常和内部实现异常必须阻断装配检查并暴露根因；
- 完成运行时注册表切换后删除静态回退路径。

### FE-BE-01：财务配置仍按旧扁平键读写

位置：

- `frontend/src/api/client.ts:591-634`
- `frontend/src/pages/ConfigPage.tsx:159-169`
- `frontend/src/pages/ConfigPage.tsx:347-354`
- `frontend/src/pages/ConfigPage.tsx:633-653`
- `backend/iesplan/services/config.py:120-142`
- `backend/iesplan/services/config.py:502-546`

后端权威结构是：

```text
parameters.economic.discount_rate
parameters.economic.tax_rate
parameters.economic.project_years
parameters.economic.depreciation_years
```

前端却从 `params.discount_rate`、`params.tax_rate`、`params.evaluation_period_years` 等扁平键读取，并把修改值写回同级。`configFromServer` 已把整个 `parameters` 对象放进 `CalcConfig.params`，因此真正的经济参数位于 `params.economic`。保存时原 `economic` 子对象仍存在，后端校验可以通过，但财务计算继续读取子对象中的旧值，新增的扁平字段基本成为无效附加数据。

整改要求：

- 前端表单直接读取和更新 `params.economic`；
- 将 `evaluation_period_years` 统一为后端定义的 `project_years`；
- 为配置 DTO 建立明确类型，停止以 `Record<string, unknown>` 传播结构；
- 增加非默认贴现率、税率、项目年限保存后重新读取并进入计算快照的往返测试。

### FE-BE-02：前端连线没有按实际端口匹配

位置：

- `frontend/src/pages/ModelPage.tsx:298-339`
- `frontend/src/pages/model/canvasModel.ts:91-147`
- `backend/iesplan/services/model.py:169-188`
- `backend/iesplan/services/model.py:643-653`

画布先根据前端硬编码规则得到用户选择的 `carrier:direction`，但发送请求前只通过 `direction === 'out'/'in'` 查找服务器端口，没有匹配 `port_type` 或句柄载能。

直接后果：

- 热泵同时有热、冷输出时，选择冷端可能仍取到第一个热输出端口，后端以载能不一致拒绝；
- 电池后端端口方向是 `bidirectional`，前端只查 `in/out`，因此找不到真实端口；
- 未来插件若声明多端口或非当前硬编码方向，前端无法正确表达。

整改要求：

- 画布节点直接使用 `GET /model` 返回的真实端口 ID、类型和方向；
- 新建设备响应已返回 `ports`，前端适配层不得丢弃；
- 连线时按 `device_id + carrier/port_type + direction compatibility` 精确匹配；
- 双向端口在 UI 可同时作为 source/target，但提交使用同一个真实端口 ID；
- 后端仍作为最终权威校验，前端本地检查仅用于即时提示。

### FE-BE-03：人工评估功能是假适配

位置：

- `frontend/src/pages/ResultsPage.tsx:477-500`
- `frontend/src/api/client.ts:1533-1539`
- `backend/iesplan/api/results.py:45-49`
- `backend/iesplan/api/results.py:101-128`

结果页收集人工维度、总分和评论，但客户端方法忽略整个 `AssessmentInput`，固定提交 `{assessment_type: "full"}`。后端接口语义是触发系统四维检查，并不支持人工评估持久化。

整改要求二选一：

1. 如果产品需要人工评估：新增独立请求 DTO 和后端持久化端点，明确 `assessor=human`、审计人和不可变记录；
2. 如果当前阶段不支持：删除人工评分表单，按钮和文案改为“重新运行系统评估”。

禁止继续静默忽略用户输入。

### FE-BE-04：运行时设备注册表没有贯通 API 和前端

位置：

- `backend/iesplan/devices/registry.py:33-67`
- `backend/iesplan/api/model.py:31`
- `backend/iesplan/api/model.py:134-137`
- `backend/iesplan/services/model.py:43`
- `backend/iesplan/services/model.py:592`
- `backend/iesplan/services/config.py:59-68`
- `frontend/src/pages/model/canvasModel.ts:100-147`

后端已实现运行时 `DeviceRegistry`，但公开设备目录、设备创建、参数校验、配置生成等主要路径仍导入旧 `core.registry`。前端 API 类型只接收能源载体和参数 schema，不接收 YAML 中的真实端口、`model_method`、`stateful`、能力与时间序列声明，随后又在前端复制九类设备规则。

整改要求：

- 设备目录 API 从当前 `DeviceRegistry` 快照生成；
- API schema 至少包含 `ports`、`capabilities`、`model_method`、`stateful` 和版本；
- 模型服务、配置服务、装配检查统一通过设备模块门面查询；
- 前端删除 `PORT_RULES`，按后端 schema 渲染设备和端口；
- 将通用数据类型移出 `core.registry`，设备与算法分别归属其公开模块；随后直接删除 `core.registry`，不保留兼容转发。

### FE-BE-05：财务基准确认读取了错误对象层级

位置：

- `frontend/src/api/client.ts:1403-1424`
- `backend/iesplan/services/validation.py:531-541`
- `backend/iesplan/services/validation.py:576-584`

`api.config.get()` 已返回规范化的 `CalcConfig`，其中服务端 `config.parameters` 被映射到 `cfg.params`。但基准确认又把 `cfg` 强制转换成 `{config:{parameters:...}}`，所以 `rec.config` 始终不存在，最终提交固定默认值。非默认项目的确认哈希与后端当前假设哈希不一致，刚确认后仍会产生 `VALID-FIN-STALE`。

整改要求：

- 前端从 `cfg.params.economic`、`cfg.min_irr` 和项目真实币种生成后端契约要求的完整假设对象；
- 后端只负责校验该输入、计算哈希和记录确认，不增加用于简单字段换算的额外接口；
- 增加非默认配置下“确认后立即校验不 stale”的端到端测试。

### FE-BE-06：`analysis` 任务未适配到前端

位置：

- `backend/iesplan/api/tasks.py:37-53`
- `backend/iesplan/services/tasks.py:75-86`
- `backend/iesplan/worker/runner.py:42-43`
- `frontend/src/types.ts:528-536`
- `frontend/src/pages/TasksPage.tsx:62`

后端 API、服务和 Worker 已支持 `analysis`，但前端 `TaskType` 联合类型、任务筛选、提交类型和表单均没有该值，也没有构造后端要求的 `task_params.sweeps`。

整改要求：

- 在前端类型和 i18n 中加入 `analysis`；
- 提供扫描参数、取值序列和目标指标表单；
- 结果页识别 `result_kind=analysis_result`，展示汇总、单调性和极值点；
- 在后端 analysis 汇总契约稳定之前，不应只加入一个无法构造有效 payload 的下拉选项。

## 4. 后端为适应前端形成的反向耦合

### 4.1 缺失必填负荷曲线被补成 `null`

位置：

- `backend/iesplan/services/model.py:103-111`
- `backend/iesplan/services/model.py:457-474`
- `backend/iesplan/services/model.py:615-623`

代码明确以“前端拖拽负荷设备会跳过 `default=null` 参数”为理由，把缺失的负荷曲线引用补成 `null` 并允许创建设备。允许草稿暂时不完整可以是合理领域规则，但当前规则由前端实现细节驱动，且“必填字段”另行硬编码在 `_REQUIRED_PARAMS`，没有来自设备 YAML。

建议把语义改为：设备草稿允许缺少运行输入，但保存时返回明确的 non-blocking/incomplete 状态；完整校验和任务提交闸门必须按 YAML `required` 声明阻断。删除注释和实现中对具体前端行为的依赖。

### 4.2 设备语义在后端和前端重复硬编码

位置：

- `backend/iesplan/services/model.py:63-96`
- `frontend/src/pages/model/canvasModel.ts:126-147`

两端分别维护端口方向、粗分类别和特殊设备模式。任何插件新增、设备模式调整或双向端口规则修改都需要同时改两端，已经违反插件式设备目录的目标。

### 4.3 前端适配层承担业务编排

位置：

- `frontend/src/api/client.ts:815-904`
- `frontend/src/api/client.ts:1131-1205`
- `frontend/src/api/client.ts:1463-1491`
- `frontend/src/api/client.ts:1564-1604`
- `frontend/src/api/client.ts:1608-1678`

适配层维护跨请求内存缓存、生成草稿差量命令、主动触发评估、拼装逐时结果和伪造临时 `Report` ID。刷新页面或换入口后缓存会丢失，且调用顺序成为隐含契约。上述操作应逐步下沉到稳定的后端应用服务或由 API 返回可直接消费的资源标识。

## 5. 测试与覆盖结论

本次按仓库要求仅在 Docker 环境执行编译和测试：

- `docker compose build web backend`：通过；
- `pytest -q tests/test_model_api.py tests/test_config_api.py tests/test_tasks_api.py`：`68 passed, 1 warning`。

现有测试通过不能证明前后端已完成适配，原因包括：

- 前端没有自动化测试，镜像构建只验证 TypeScript 可编译；
- `frontend_smoke.mjs` 和 `contract_smoke.py` 主要验证字段/信封形状，没有验证值是否按业务语义往返；
- 没有覆盖热泵冷端、电池双向端口等真实画布连线；
- 没有覆盖非默认财务配置保存后进入快照与计算；
- 没有覆盖人工评估输入持久化；
- 没有覆盖运行时插件从 YAML → API → 前端 → 模型写入 → 命令执行的全链路；
- 没有覆盖 `analysis` 前端任务提交与结果展示；
- 现有模型 API 测试还主动固化了“缺必填负荷曲线补 `null`”的前端兼容行为。

## 6. 建议整改顺序

### 第一阶段：修复当前用户可见语义错误

1. 修正财务配置嵌套结构和字段名；
2. 使用真实端口 ID 修复热泵/电池连线；
3. 删除假人工评估或实现真实人工评估端点；
4. 修正前端财务基准输入生成，后端校验并记录该规范输入。

### 第二阶段：建立开放的模块协议

1. `devices`、`modeling`、`assembly` 各自拥有注册表和公开 provider 协议；
2. 建模模块通过公开描述接收设备建模信息，不读取设备模块目录；
3. API、模型、配置、装配全部只调用对应模块公开门面；
4. 各模块内部先完整构建候选注册表，再原子替换本模块状态；
5. 启动组合失败直接退出，不保留静态回退；
6. 删除 `core.registry` 设备静态表和 `services/model.py` 设备硬编码表。

### 第三阶段：前端薄化与新能力适配

1. 设备目录 API 输出端口和模型元数据，前端删除 `PORT_RULES`；
2. 将草稿差量、评估解析、导出资源定位下沉后端；
3. 增加 `analysis` 任务输入和结果视图；
4. 以生成 DTO/OpenAPI 类型或共享契约测试替代大量 `unknown` 强制转换。

## 7. 完成验收标准

- 每个模块独立报告自己的注册项与版本；跨模块引用通过公开 ID/版本约束校验，不共享注册表对象；
- 任一设备或命令构建失败时，旧快照仍完整可用，不出现部分替换；
- 新增一个仅通过 YAML 声明的测试设备，无需修改 Python/TypeScript 硬编码即可出现在前端并正确连线；
- 热泵热/冷输出和电池双向端口均有浏览器级连线测试；
- 非默认财务参数保存、重读、基准确认、快照和计算结果使用同一数值；
- 页面不再展示无法持久化的人工评估功能；
- `analysis` 能从前端构造有效 sweeps、提交、查看汇总结果；
- 契约测试不仅断言字段存在，还断言关键业务值往返一致；
- 全部编译与测试继续只在 Docker 环境执行。

## 8. 前端内部解耦建议

### 8.1 结论

前端也需要独立重构。目前不是简单的“文件偏大”，而是以下四类职责交叉：

1. **传输层**：HTTP、Cookie、错误信封、下载 Blob；
2. **契约适配层**：后端 DTO 到前端对象的字段映射；
3. **应用编排层**：多接口调用、重试、轮询、缓存失效、提交动作；
4. **展示层**：React 状态、表单、弹窗、图表、i18n。

`frontend/src/api/client.ts` 同时承担前 3 类职责；`ModelPage.tsx`、`ResultsPage.tsx`、`ConfigPage.tsx`、`DataPage.tsx` 和 `TasksPage.tsx` 又把应用编排、领域规则和展示混在单个页面中。当前主要文件规模为：

| 文件 | 行数 | 主要混合职责 |
|---|---:|---|
| `api/client.ts` | 1854 | HTTP、DTO 映射、缓存、跨端点工作流、降级兼容 |
| `pages/ModelPage.tsx` | 1324 | 画布、模型 CRUD、端口规则、连线、布局、诊断 |
| `pages/ResultsPage.tsx` | 1194 | 任务发现、结果聚合、评估、逐时数据、图表、导出 |
| `pages/ConfigPage.tsx` | 1178 | DTO 转换、表单状态、业务校验、设备变量、算法能力 |
| `pages/DataPage.tsx` | 1145 | 上传、解析预览、版本、绑定、质量报告、下载 |
| `pages/TasksPage.tsx` | 1047 | 轮询、筛选、提交、校验门禁、详情、取消和重试 |
| `types.ts` | 868 | 所有域类型、API DTO、UI 类型混放 |

建议采用“**按业务 feature 垂直切分，feature 内再分 api/domain/hooks/components**”的结构。不要按全局 `pages/components/utils` 继续横向堆文件，也不建议一开始引入复杂的全局状态框架。

### 8.2 哪些功能需要解耦

#### A. API 基础设施与业务 API 分离

当前 `client.ts` 中只有以下能力应属于公共基础设施：

- URL/query 构造；
- `fetch`、超时和 AbortSignal；
- Cookie 会话；
- 标准成功/错误信封解析；
- JSON、FormData、Blob 请求。

项目、模型、配置、任务、结果等方法应分别移动到 feature 内的 `api.ts`。跨端点流程不能放在底层 HTTP client 中，例如“评估不存在就主动创建”“通过证据包缓存反查任务”“生成草稿命令差量”等。

建议基础接口：

```ts
// shared/api/http.ts
export interface HttpClient {
  get<T>(path: string, options?: RequestOptions): Promise<T>
  post<T>(path: string, body?: unknown, options?: RequestOptions): Promise<T>
  put<T>(path: string, body?: unknown, options?: RequestOptions): Promise<T>
  delete<T>(path: string, options?: RequestOptions): Promise<T>
  blob(path: string, options?: RequestOptions): Promise<BlobResponse>
}
```

`shared/api/http.ts` 不得导入任何 Project、Device、Task、Result 领域类型。

#### B. DTO、领域模型和 UI 表单类型分离

当前 `types.ts` 同时描述数据库投影、API DTO、页面对象和表单输入，导致大量 `unknown`、强制转换和虚构缺省字段。

每个 feature 至少区分：

- `contracts.ts`：与后端 JSON 一一对应的 DTO；
- `model.ts`：前端稳定领域模型；
- `form.ts`：只服务表单的字符串/临时状态；
- `mappers.ts`：DTO ↔ model/form 的纯函数。

例如配置域不应再用一个 `CalcConfig` 同时代表后端配置和页面表单：

```ts
interface CalcConfigDto {
  parameters: {
    devices: Record<string, Record<string, unknown>>
    economic: EconomicParametersDto
    environmental: EnvironmentalParametersDto
  }
  variables: ConfigVariableDto[]
  algorithm: AlgorithmDto
  irr_floor: number | null
}

interface EconomicFormState {
  discountRatePercent: string
  taxRatePercent: string
  projectYears: string
  depreciationYears: string
}
```

只有 `config/mappers.ts` 可以做百分比展示转换和字段映射，页面组件不得直接拼后端 JSON。

#### C. 页面组件与应用编排分离

页面应成为路由级容器，只做三件事：

1. 读取路由参数；
2. 调用 feature hook/use-case；
3. 组合展示组件。

建议将页面控制在约 150～300 行。不是机械按行数拆文件，而是让每个模块只有一个变化原因。

推荐拆分：

- `ModelPage`：`useModelGraph`、`useDeviceMutations`、`useConnectionMutations`、`ModelCanvas`、`DeviceInspector`、`ModelDiagnostics`；
- `ConfigPage`：`useCalcConfig`、`EconomicSection`、`VariablesSection`、`ConstraintsSection`、`AlgorithmSection`；
- `TasksPage`：`useTaskList`、`useTaskDetail`、`useTaskPolling`、`TaskSubmitDialog`、`TaskDetailPanel`；
- `ResultsPage`：`useResultCatalog`、`useTaskResult`、`useHourlySeries`、`AssessmentPanel`、`ResultCharts`、`ExportActions`；
- `DataPage`：`useDatasets`、`useDatasetUpload`、`DatasetVersionList`、`QualityReportPanel`、`DatasetBindingActions`。

#### D. 服务器状态和本地 UI 状态分离

当前页面使用多个 `useEffect + useState + catch` 自行管理服务器状态，`client.ts` 又使用模块级 `Map` 缓存，形成两套不透明缓存。

建议统一服务器状态层，可以采用专门的 query/mutation 库，也可以先实现项目内轻量 hooks，但必须具备：

- query key，例如 `['project', id]`、`['model', projectId]`；
- loading/error/data 明确状态；
- mutation 成功后的定向失效；
- 请求取消和竞态保护；
- 轮询只由任务 feature 管理；
- 不以模块级 `Map` 保存业务关联。

本地 React state 只保留尚未提交的表单、弹窗开关、选中项等瞬时 UI 状态。

#### E. 模型画布与设备领域规则分离

画布应只负责坐标和交互，不应定义设备端口业务。建议数据流为：

```text
DeviceCatalogDto.ports
        ↓ mapper
CanvasPortViewModel（含真实 port id / carrier / direction）
        ↓
ReactFlow Handle
        ↓
connect({from_port_id, to_port_id})
        ↓
后端权威校验
```

前端可以保留不依赖设备种类的通用提示规则，例如禁止自环、同一边重复，但热泵模式、燃气端口、双向端口等规则必须来自后端 schema。

布局也应明确产品语义：

- 若布局只属于单浏览器个人偏好，可保留 `localStorage`，但增加 schema version 和项目/用户命名空间；
- 若布局需要跨设备或多人共享，应使用后端现有 `position/layout` 字段，不能同时维护服务器布局和本地布局两个事实源。

#### F. 配置表单与后端 schema 解耦

配置页目前硬编码参数键、变量命名和算法能力表。建议：

- 设备参数、单位、范围、枚举、是否可优化从设备目录 API 获取；
- 算法能力从 `/registry/algorithms` 获取，删除前端 `ALGORITHM_CAPABILITIES`；
- 页面只保留展示相关分组和输入控件选择；
- 本地校验只覆盖必填、格式和即时反馈；量纲、算法兼容、约束表达式和求解可行性由后端权威校验；
- 保存和“只校验”都接收同一个待提交 DTO，不能像现在一样校验已保存配置而不是当前表单内容。

#### G. 结果读取、评估和导出分离

当前结果页和客户端把任务、证据包、评估、逐时字段和导出串成隐式调用顺序。建议后端结果视图返回稳定链接或资源 ID，前端分成三个 use-case：

- `loadResult(taskId)`：结果摘要、候选解和证据包；
- `loadHourly(taskId, fields, range)`：按图表当前需要的字段与范围读取；
- `exportResult(taskId, options)`：直接返回可下载资源，不在内存中伪造 `Report.id`。

评估属于独立 feature。系统评估和人工评估必须使用不同命令和 DTO，不能共用一个方法后静默转换。

#### H. 错误与降级策略统一

页面中大量 `.catch(() => null/[])` 会把权限错误、契约错误和服务器错误显示成“暂无数据”。建议：

- 只有明确标记为 optional 的附加区块允许局部降级；
- 主资源失败必须显示可重试错误；
- 并行请求返回 `PartialData` 时保留每个区块的独立错误；
- 禁止捕获契约转换异常后返回空列表；
- API 错误、领域诊断和 UI 文案错误分别建模，不再全部压成 `ApiError`。

### 8.3 推荐目录结构

```text
frontend/src/
├── app/
│   ├── App.tsx
│   ├── routes.tsx
│   └── providers.tsx
├── shared/
│   ├── api/
│   │   ├── http.ts
│   │   ├── error.ts
│   │   └── session.ts
│   ├── ui/
│   ├── i18n/
│   ├── format/
│   └── types/
├── features/
│   ├── auth/
│   │   ├── api.ts
│   │   ├── contracts.ts
│   │   ├── model.ts
│   │   └── components/
│   ├── projects/
│   ├── modeling/
│   │   ├── api.ts
│   │   ├── contracts.ts
│   │   ├── model.ts
│   │   ├── mappers.ts
│   │   ├── hooks/
│   │   └── components/
│   ├── datasets/
│   ├── config/
│   ├── validation/
│   ├── tasks/
│   ├── results/
│   └── exports/
└── pages/
    ├── ModelPage.tsx
    ├── ConfigPage.tsx
    └── ...（只保留路由组合）
```

依赖规则：

```text
app/pages → features → shared
feature A 不直接导入 feature B 的内部文件
跨 feature 编排放在 pages 或显式 application/use-case 模块
shared 永远不依赖 feature
components 不直接调用底层 http
```

### 8.4 建议实施顺序

不要一次性重写前端。建议保持页面可运行，按以下顺序小步迁移：

1. **先拆 `client.ts`**：提取无业务含义的 `shared/api/http.ts`，其余方法按 feature 移动，行为暂时不变；
2. **拆类型和 mapper**：优先处理 config、model、results 三个已发现契约错误的域；
3. **修复 P1 语义问题**：财务配置、真实端口连线、人工评估；
4. **引入统一服务器状态 hooks**：先迁任务轮询和模型 CRUD，删除模块级业务缓存；
5. **拆大页面组件**：每迁一个 feature，同步增加组件和 hook 测试；
6. **切换 schema 驱动设备画布**：后端设备目录稳定后删除前端端口硬编码；
7. **最后接入 analysis**：避免在旧页面结构上继续增加条件分支。

### 8.5 前端专项验收标准

- `shared/api` 不含任何业务类型和跨端点流程；
- `client.ts` 被删除或仅保留兼容 re-export，不再存在模块级业务缓存；
- 页面组件不直接构造后端 DTO；
- 设备端口、算法能力和参数范围不存在前后端重复硬编码；
- 服务器状态只有一个明确缓存来源，mutation 后可预测地刷新；
- 主请求错误不会被转换为空数组或“暂无数据”；
- config/model/results 的 DTO mapper 有纯函数单元测试；
- 关键用户流程有浏览器测试：设备连线、配置保存、任务提交、结果评估和导出；
- 单个 feature 可在不导入其他 feature 内部实现的情况下独立测试。

## 9. 架构原则与已采纳决定

### 9.1 开放优先，不建立跨模块统一注册表

不实施跨 `devices/modeling/assembly` 的全局 `CatalogSnapshot`。每个模块独立实现、独立测试和独立替换：

```text
应用启动组合层
├── devices.register(DeviceProvider)
├── modeling.register(ModelCommandProvider)
├── assembly.register(AssemblyRuleProvider)
└── 校验公开 ID/版本引用是否可解析
```

组合层只做依赖注入和启动校验，不读取模块内部字典，也不把不同模块的注册项合并成新的权威状态。模块之间允许调用公开接口，但禁止读取对方目录、全局变量、私有函数或 ORM 实现。

在正式发布前不实现运行期热加载。扩展在进程启动时发现和注册；任一模块装配失败则进程启动失败。这样优先保证开放边界和故障可见性，避免为了热加载引入跨模块事务。

### 9.2 `core.registry` 的处理决定

`backend/iesplan/core/registry.py` 是早期的单文件静态注册表，当前同时包含：

- `ParameterSpec`、`DeviceTypeSpec`、`AlgorithmSpec` 数据类型；
- 九类设备的静态定义；
- 算法静态定义；
- `get_device_type/list_device_types/get_algorithm` 等全局查询函数。

它与新的 `devices/registry.py + catalog/*.yaml` 重复，且仍被 API、模型、配置、装配、校验和引擎直接导入。整改决定：

- 通用数据结构移动到不含注册状态的契约模块，例如 `core/contracts/parameters.py`；
- 设备静态定义和查询函数直接删除，由 `devices` 模块公开接口替代；
- 算法定义归属 `modeling` 或 `engines` 的公开注册接口，不继续与设备混放；
- 不保留旧 `core.registry` 转发兼容层。

### 9.3 模块公开边界

允许模块间依赖，但只能依赖公开门面或协议：

```text
from iesplan.devices import get_device, list_devices
from iesplan.modeling import resolve_command
from iesplan.assembly import build_assembly, validate_assembly
```

禁止：

- 跨包导入 `_private_function`、私有常量或内部 registry 字典；
- 导入其他模块的 `loader.py`、文件路径 helper 或 ORM repository；
- 通过异常捕获猜测另一个模块是否已经实现；
- 复制另一个模块的设备、端口、容量参数或算法映射表。

增加静态架构测试，检查跨包导入和 `_` 私有符号导入。

### 9.4 DTO 的含义

DTO 不是“专门让后端替前端做简单换算的接口”，也不要求新增额外端点。它只是 API 边界上明确的数据结构，例如现有 `POST /tasks` 的请求体和 `GET /model` 的响应体。

采纳的边界规则：

- 前端负责表单字符串、百分比展示、简单单位换算和请求对象组装；
- 前端提交后端公开契约要求的规范输入；
- 后端负责结构、范围、权限和领域约束校验，不替前端维护展示状态；
- 后端返回稳定业务数据，不返回专属于某个页面组件的临时形状；
- 类型转换通过两端各自的纯 mapper 完成，不通过额外网络请求完成。

后端 Pydantic request/response model 的作用是让接口自描述、可校验并支持外部调用；前端 `contracts.ts` 与其字段一一对应，`form.ts/mappers.ts` 负责生成该输入。

### 9.5 删除装配回退映射

`backend/iesplan/assembly/plan.py:89-136` 当前存在两条路径：

```text
content → build_assembly → plan_from_assembly → engine
content ──装配异常──→ _direct_plan → engine
```

第二条就是“回退直接映射”。`plan_from_content()` 捕获所有装配异常，随后 `_direct_plan()` 跳过端口、边、管道、建模方法、状态声明和装配检查，仅从旧 `content.model.devices` 拼出引擎 plan，并补入几项旧参数。

这会让装配层失去强制边界：错误模型本应停止，却可能绕过装配继续计算。整改决定：直接删除 `_direct_plan` 和宽泛异常捕获。调用方必须显式提供合法 `AssemblySpec`；需要从项目内容构建时，装配错误原样向上报告。

同时删除 `_port_direction()` 中“注册表异常则返回 bidirectional”的回退，端口定义缺失必须是明确错误。

### 9.6 不保留未发布兼容层

项目尚未正式发布，因此：

- 前端和后端同时切换到新契约；
- 删除旧字段别名、静态注册表、直接 plan 映射和旧模块转发；
- 不以“兼容旧调用方”为理由捕获异常或补默认结构；
- 数据库若已有开发数据，使用一次性开发迁移或重建，不把迁移分支留在长期业务代码中。

### 9.7 失败语义

- 插件、注册或模块装配错误：启动失败；
- 项目输入或装配错误：返回完整诊断并阻断任务；
- Worker 缺少命令：Worker 启动失败，不领取任务；
- 数据库、对象存储等外部设施错误：readyz/任务重试明确反映；
- 内部实现异常：不得降级为旧逻辑或空数据。

## 10. 存储专项审查与适应性调整

### 10.1 结论

存储需要适应性重构，而且优先级高于目录整理。当前 `services/objects.py` 虽然声明自己是对象域唯一写入单元，`services/project.py` 却又实现了一套内容寻址、落盘、读取和引用计数逻辑。两套实现共享同一张 `objects` 表，却使用不同的 `storage_path` 解释规则，已经不是合理的模块自治，而是两个实现争用同一持久化协议。

前端不应感知对象文件路径、引用计数或存储后端类型；它只应消费上传、下载、导出和管理员存储视图等公开资源。当前前端没有直接操作文件路径，但管理员客户端仍在适配后端“两版响应并集”。由于项目尚未发布，应直接确定单一 DTO 并同步切换，不保留兼容形状。

### 10.2 问题汇总

| 编号 | 优先级 | 问题 | 主要影响 |
|---|---|---|---|
| STO-01 | P1 | 项目模块和对象模块各自实现对象落盘，且路径解释不兼容 | 同一对象表中的记录无法被另一套读取/校验/清理逻辑可靠使用 |
| STO-02 | P1 | 业务引用只增加、不解除，项目内容还绕过 `object_refs` 直接累加计数 | 删除项目、替换草稿或清理导出后对象永久不可回收，计数持续漂移 |
| STO-03 | P1 | 对象服务在捕获唯一键冲突后调用调用方 Session 的 `rollback()` | 一个幂等存储操作可能回滚同事务内无关的项目、数据集或任务变更 |
| STO-04 | P2 | 文件落盘与数据库事务没有明确恢复协议 | 数据库回滚会留下无记录文件，进程故障可能造成文件与元数据不一致 |
| STO-05 | P2 | 存储模块硬编码业务实体映射，调用方直接依赖存储 ORM | 新业务模块接入需要修改存储内部代码，模块边界不开放 |
| STO-06 | P2 | 导出包另存非托管副本且静默忽略失败，容量探测失败时仍允许写入 | 配额、校验、清理和故障语义失真 |
| STO-07 | P2 | 存储 API 合并旧响应，且在存储路由中实现全系统健康聚合 | 前端被迫适配历史形状，存储模块反向依赖任务、项目、用户和队列内部数据 |

### 10.3 STO-01：收敛为一个公开对象存储协议

位置：

- `backend/iesplan/services/objects.py:163-165, 313-344`
- `backend/iesplan/services/project.py:1305-1409`

通用对象服务把 `storage_path` 定义为相对 `data_dir`，新对象写成 `objects/{sha256}`。项目服务则以 `data_dir/objects` 为根，记录 `{前缀}/{oid}.json`。因此：

- 通用对象服务读取项目对象时会查找 `data_dir/{前缀}/{oid}.json`，缺少 `objects/`；
- 项目服务读取通用对象时会查找 `data_dir/objects/objects/{sha256}`，重复 `objects/`；
- 两者以同一 `sha256/oid` 去重时可能复用一条自己无法读取的记录；
- 完整性抽查、清理和项目内容读取不共享同一事实。

整改决定：

- 只保留一个公开存储门面，例如 `iesplan.storage`；`project/dataset/package/results/worker` 只能调用该门面；
- `storage_path` 的解释、分桶、临时文件和哈希校验全部是存储模块内部实现；
- 项目模块只保存公开 `ObjectId/ObjectHandle`，不得导入 `StoredObject`、拼路径或调用存储私有函数；
- JSON 规范化可由项目模块完成，形成字节后交给存储；存储不理解“草稿/版本”等业务内容；
- 文件系统、S3 或其他实现通过 `BlobStore` provider 接入，业务模块不随适配器变化。

建议最小公开协议：

```python
class ObjectStore(Protocol):
    def put(self, content: bytes, media_type: str) -> ObjectHandle: ...
    def get(self, object_id: ObjectId) -> bytes: ...
    def stat(self, object_id: ObjectId) -> ObjectHandle: ...
    def attach(self, object_id: ObjectId, owner: ObjectOwner) -> None: ...
    def detach(self, object_id: ObjectId, owner: ObjectOwner) -> None: ...
```

这是后端模块协议，不是专门为前端增加的接口，也不要求每个方法都暴露为 HTTP 端点。

### 10.4 STO-02：以引用清单为权威生命周期，不维护双事实源

位置：

- `backend/iesplan/services/project.py:1320-1344`
- `backend/iesplan/services/objects.py:484-575`
- `backend/iesplan/services/project.py:466-495`

`project._store_content()` 每次遇到相同内容都直接 `ref_count += 1`，但不建立 `ObjectRef`。其他业务路径通过 `add_ref()` 同时写引用行和计数；生产代码中没有任何 `remove_ref()` 调用，只有测试调用。项目删除、草稿替换、数据版本退役和短期导出过期均没有成对解绑。

开放优先于效率的前提下，不建议继续同时维护 `ref_count` 与 `object_refs` 两个权威值：

- `ObjectRef`/`ObjectOwner` 清单作为唯一权威；引用数按查询计算；
- 将来确有性能问题时，`ref_count` 只能作为可重建缓存，并增加一致性巡检，不能参与正确性判断；
- 每个业务模块在自己的公开删除/替换流程中显式 `attach/detach`，存储模块不反向理解项目、快照、证据或报告表；
- 项目软删除是否释放版本、草稿和证据引用必须由产品保留策略明确决定，不能以“以后清理”代替生命周期协议；
- 为草稿替换、项目删除、数据版本退役、证据删除、导出过期增加引用对称性测试。

这样可以删除 `REF_ENTITY_TYPE_MAP` 和 `PROTECTED_ENTITY_TYPES`。任意存在的公开 owner 引用都自然阻止清理，新插件或新业务域只需使用自己的命名空间，不必修改存储模块。

### 10.5 STO-03：存储模块不得回滚调用方事务

位置：

- `backend/iesplan/services/objects.py:348-370`
- `backend/iesplan/services/objects.py:498-522`

`put_object()` 和 `add_ref()` 为处理并发去重/重复引用而调用 `db.rollback()`。Session 和业务事务由上层用例拥有，底层存储模块无权整体回滚。否则一次正常的唯一键竞争可能撤销此前已 flush 的项目、任务或数据集修改，随后函数还返回成功对象，使调用方无法知道事务内容已经丢失。

整改要求：

- 使用数据库 upsert，或在 `begin_nested()` savepoint 内处理唯一键竞争；
- 存储门面只 `flush` 或返回结果，提交/回滚由应用用例统一决定；
- 增加测试：先修改业务行，再触发重复对象/重复引用竞争，断言业务修改仍在事务中；
- 禁止所有 repository/service 捕获局部冲突后调用共享 Session 的全局 `rollback()`。

### 10.6 STO-04：明确文件与数据库的故障恢复协议

当前位置先把文件原子 rename 到最终路径，再插入数据库行。这保证数据库不会引用半文件，但外层事务回滚后会留下没有元数据行的完整文件。现有清理只扫描数据库候选，不扫描这种磁盘孤儿。

不需要追求文件系统与数据库的虚假“单事务”，而要公开且可恢复：

1. 临时文件完整写入、`fsync` 并计算摘要；
2. 以确定性路径原子提交完整文件；
3. 在业务数据库事务中 upsert 元数据和 owner 引用；
4. 提供幂等 reconciliation：清理超龄临时文件、登记或删除无元数据最终文件、报告有元数据但缺文件的损坏；
5. 启动 readiness 和管理员巡检明确报告损坏，不返回空内容或旧数据。

清理执行也应采用状态机或可重试步骤，不能在数据库提交前删除文件后假设提交一定成功。

### 10.7 STO-05：去除业务反向依赖和 ORM 泄漏

位置：

- `backend/iesplan/services/objects.py:48-67`
- `backend/iesplan/services/dataset.py:737-778`
- `backend/iesplan/services/package.py:278-320, 879-885`
- `backend/iesplan/services/validation.py:779-783`
- `backend/iesplan/models/audit.py:1-67`

存储服务硬编码 `dataset_file/version_ref/snapshot_ref/evidence_package/report` 到业务表名的映射；反方向上，数据集、校验和包服务直接接收、查询或返回 `StoredObject` ORM。结果是新模块接入既要懂存储表，又要修改存储业务映射。

整改要求：

- 公开不可变 `ObjectHandle(id, digest, size, media_type)` 和 `ObjectOwner(namespace, id, purpose)`，不向外返回 ORM；
- `get()` 接受公开对象 ID，不要求调用方自己加载 `StoredObject`；
- owner namespace 是调用方声明的稳定标识，存储只判断“是否有引用”，不导入业务模型；
- `StoredObject/ObjectRef` 从混合的 `models/audit.py` 移至存储所属持久化模块；审计、导入提案、保留规则不再与对象 ORM 混放；
- 存储通过公开审计接口或领域事件记录审计，不直接创建另一个模块的 `AuditLog` ORM。

### 10.8 STO-06：删除非托管副本和静默放行

位置：

- `backend/iesplan/services/package.py:561-594`
- `backend/iesplan/services/objects.py:195-201`

项目包登记到对象存储后，又写入 `data_dir/packages/project-{id}-{object_id}.zip`，失败被静默忽略。该副本没有引用、配额、校验和清理协议。应直接删除这条复制逻辑；若确有运维归档需求，应把它定义为独立、显式配置的 export sink，并拥有自己的保留与故障策略。

磁盘容量无法读取时，写入门禁当前直接放行。按已采纳的故障语义，容量状态未知应拒绝新写入并使 readiness 降级，同时保留只读能力；不能以静默放行为默认。

### 10.9 STO-07：拆开存储管理与全系统运维聚合

位置：

- `backend/iesplan/api/objects.py:1-116`
- `backend/iesplan/api/admin.py:1-16, 100-140`
- `frontend/src/api/client.ts:1044, 1703-1736`

`api/objects.py` 明确把历史两版 `/storage`、`/health` 响应做并集合并；同一文件还直接查询 Task、Project、User 并调用队列，已经超出存储边界。

整改决定：

- `/admin/storage` 只返回一个严格的 `StorageStatusDto`，包含对象用量、后端容量、损坏/待清理数量；
- `/health` 或 `/readyz` 由独立 operations/application 聚合层调用各模块公开 health provider；存储模块只提供自己的健康结果；
- 删除 `{**stats, "stats": stats}`、`{**view, **verify}` 等兼容并集；
- 前端 `admin.storage()` 和 `admin.health()` 与新 DTO 同步切换，mapper 不再猜测多种层级；
- 清理仍可保留管理员端点，但请求应使用明确计划 ID 或版本，避免 dry-run 与执行之间候选集变化。

### 10.10 推荐模块结构与依赖规则

不建立跨业务全局仓库。存储作为独立基础能力，通过公开协议被组合层注入：

```text
backend/iesplan/storage/
├── __init__.py          # 只导出 ObjectStore、ObjectHandle、ObjectOwner、错误类型
├── contracts.py         # 纯类型与公开协议
├── service.py           # 哈希、引用、校验、清理、恢复编排
├── persistence.py       # StoredObject/ObjectRef repository，模块内部
└── adapters/
    └── filesystem.py    # 当前本地文件实现；未来可增加其他 provider
```

依赖方向：

```text
project/dataset/results/package/worker
                  ↓ 仅公开协议
                storage
                  ↓
        configured BlobStore adapter
```

应用启动层选择 provider 并注入；它不读取存储内部路径或 ORM。前端继续负责上传前的表单/简单元数据预处理，后端负责字节完整性、权限、配额和生命周期，不增加用于简单换算的网络接口。

### 10.11 存储专项验收标准

- 全仓库只有一个地方解释 `storage_path` 和操作对象文件；
- 项目 JSON、数据集、证据、报告和导出对象能通过同一 `put/get/verify` 往返；
- 业务模块不导入 `StoredObject/ObjectRef`，不拼接对象路径；
- 对象引用 attach/detach 成对，项目删除和资源过期后的对象可按策略回收；
- 唯一键竞争不会回滚调用方事务；
- 模拟数据库回滚、rename 失败、文件缺失和进程中断后，reconciliation 可幂等恢复或报告；
- 容量不可测或对象损坏时 readiness 明确降级，新写入失败可见；
- `/admin/storage` 和 `/health` 各有单一 DTO，前后端无兼容分支；
- 文件系统适配器可被测试内存适配器替换，业务模块无需修改；
- 相关编译和测试继续只在 Docker 环境执行。

### 10.12 本次验证

按仓库要求，仅在 Docker 环境运行：

```text
pytest -q tests/test_objects_api.py tests/test_project_api.py tests/test_dataset_api.py tests/test_package_api.py
75 passed, 1 warning
```

这些测试证明当前各自路径内的既有行为可运行，但没有覆盖两套对象协议互操作、生产删除解绑、调用方事务被局部冲突回滚、数据库回滚后的磁盘孤儿及 reconciliation，因此不能否定上述问题。

## 11. 项目文档审查与定案

### 11.1 结论

当前根目录 `README.md` 已经承担快速开始、架构概览、目录和开发测试入口，应该继续作为项目唯一入口。此前把 `docs/` 定义成开发者指南是错误的：该目录当前保存输入规格、重构方案、调研、审查和路线图，属于初步开发阶段的过程材料，不能与正式开发者指南混同。

定案如下：

- 根目录 `README.md`：保留为项目统一入口；
- 根目录 `manual/`：正式产品文档总目录，包含用户指南和开发者指南两个并列入口；
- `manual/user-guide/`：用户指南；
- `manual/developer-guide/`：开发者指南；
- `manual/developer-guide/zh-CN/ARCHITECTURE_CONSTITUTION.md`：开发者指南总则和最高开发裁决依据；
- 根目录 `docs/`：当前开发过程文档，不属于对外开发者指南；
- `docs/README.md`：开发过程材料索引。

`handbook/` 容易被理解为团队制度，`user-docs/` 虽明确但冗长，`guide/` 又不能自然表达完整参考手册。综合项目规模和现有 `docs/` 用法，`manual/` 最清晰。

### 11.2 用户指南边界

用户指南按用户任务组织，不按后端包、API 文件或数据库表组织。内容包括安装登录、项目、模型、数据、配置、任务、结果、导入导出、管理员操作、错误修复和概念解释。

用户指南不得暴露：

- ORM、表结构和私有模块路径；
- 对象存储路径和内部 provider；
- Python 函数、内部命令分发和堆栈；
- 尚未实现却写成已可用的目标功能。

功能、页面名称、字段、单位、错误处理和操作结果变化时，用户文档必须在同一变更中更新。

### 11.3 开发者指南边界

开发者指南面向扩展开发者、集成开发者、维护者和贡献者，保存已经确认、可以长期维护的架构原则、公共 API、DTO、数据格式、插件扩展、部署维护和贡献规范。架构宪法是其总则。

以下内容不属于正式开发者指南：

- 临时架构方案和备选方案；
- 差距分析和代码审查记录；
- 阶段实施顺序、实验和故障调查；
- 尚未稳定的数据库或 API 草案。

上述内容统一放在 `docs/`。成熟结论应经过整理后提炼到开发者指南，不能直接复制过程文档并形成双事实源。

### 11.4 开发过程文档边界

`docs/` 服务于当前开发过程，包括输入规格、ADR、调研、计划、路线图、审查、实验和阶段性结论。过程文档可以同时描述当前事实、目标状态、建议和迁移步骤，但必须明确标注各自状态。

过程文档不是稳定外部契约，也不能因为内容面向程序员就自动称为“开发者指南”。历史审查提供证据和决策背景；与架构宪法冲突时按宪法效力顺序裁决。

### 11.5 推荐目录

```text
README.md
manual/
├── README.md
├── user-guide/
│   ├── README.md
│   ├── zh-CN/
│   └── en-US/
└── developer-guide/
    ├── README.md
    ├── zh-CN/
    │   ├── ARCHITECTURE_CONSTITUTION.md
    │   ├── architecture/
    │   ├── public-api/
    │   ├── extensions/
    │   ├── data-formats/
    │   ├── deployment/
    │   └── contributing/
    └── en-US/              # 与中文采用相同稳定章节结构

docs/
├── README.md
├── spec/
├── adr/
├── plans/
├── reviews/
├── investigations/
└── archive/
```

现有过程文档可以继续保留在 `docs/`，不要求为了目录整洁搬入 `manual/`。只有成熟、稳定且确实需要交付的知识才提炼到正式指南。

### 11.6 文档验收要求

- 根 README、`manual` 总入口、两套指南入口及 `docs` 过程入口链接有效；
- 用户可见变化与用户指南同步，稳定公共契约变化与开发者指南同步，内部开发变化与过程文档同步；
- 三类文档不混放，过程材料不被描述成稳定公开契约；
- 长期规范标明状态、版本、日期、适用范围和权威所有者；
- OpenAPI、schema 和错误码等生成内容不手工复制维护；
- 示例只使用 Docker 工作流，与当前 DTO、枚举和单位一致；
- 文档移动后修复仓库内引用；
- Markdown、代码块、标题层级及中英文导航通过检查。

## 12. FE-DOC-01：开发统一 Markdown 帮助中心

### 12.1 现状与结论

位置：

- `frontend/src/App.tsx:8, 165-166, 218-219`
- `frontend/src/pages/TutorialPage.tsx:1-272`
- `frontend/src/data/tutorial.ts:1-360`
- `frontend/src/styles.css:1046-1297`
- `frontend/package.json`

当前顶部入口是“使用教程”，路由为 `/tutorial`。页面内容全部硬编码在 `frontend/src/data/tutorial.ts` 的双语 TypeScript 对象中，使用专用 block renderer 渲染；前端没有 Markdown 渲染依赖，也没有读取根目录 `manual/`。这会形成第三套文档事实源：正式 Markdown、TS 教程数据和界面零散帮助需要分别维护。

整改决定：开发统一“帮助中心”（英文 `Help Center`），取代“使用教程”。`manual/` 是唯一正文来源，用户指南和开发者指南在同一目录树中作为两个并列一级节点。

### 12.2 信息架构

```text
帮助中心
├── 用户指南
│   ├── 快速开始
│   ├── 项目规划完整流程
│   └── 账户、管理与故障排查
└── 开发者指南
    ├── 架构宪法
    ├── 系统架构与模块边界
    ├── API 与数据契约
    ├── 扩展开发
    └── 开发、测试与文档
```

`manual/SUMMARY.md` 登记可用语言，各语言目录顺序由 `manual/SUMMARY.<locale>.md` 定义。文件夹结构决定归属，localized SUMMARY 决定展示顺序和标题；不同语言必须保持相同稳定章节 ID，前端不得另建硬编码目录常量。

### 12.3 实现要求

- 顶部入口文案改为“帮助中心”，路由使用 `/help/*`；
- 帮助中心保持静态可读，不依赖登录、项目数据或后端计算服务；
- Docker 前端构建阶段从仓库 `manual/` 生成内容 manifest 和静态 Markdown 资源；生成物不作为第二份手工维护源码提交；
- 浏览器根据 manifest 渲染同一棵目录树，点击节点加载对应章节；
- 章节支持可复制深链接、标题锚点、前后页、当前节点高亮和刷新恢复；
- Markdown 至少支持标题、段落、列表、表格、引用、链接、行内代码和 fenced code block；
- 默认禁用原始 HTML并过滤 `javascript:` 等不安全 URL，外部链接明确标识；
- 内部 Markdown 链接转换为客户端路由，不能触发整页丢失状态；
- 桌面端显示树形侧栏，移动端提供可展开目录；键盘焦点和 `aria-current` 完整；
- 当前语言没有对应章节时明确提示可用语言，不把中文静默冒充英文版本；
- 帮助中心显示适用产品版本和文档最后更新时间。

项目尚未发布，完成后直接删除 `/tutorial`、`TutorialPage.tsx`、`data/tutorial.ts` 和专用重复正文，不保留旧路由/内容兼容层。仍有复用价值的无障碍、滚动目录和样式可以迁移到 Help Center 组件。

### 12.4 文档覆盖检查与本次补充

检查前，`manual/` 只有入口文件和架构宪法，未覆盖已经存在的项目工作台、模型、数据、配置、校验、任务、结果、导出、设置和扩展开发。

本次已增加：

- `manual/SUMMARY.md` 与 `manual/SUMMARY.zh-CN.md`：语言入口和简体中文统一帮助目录；
- 用户指南：快速开始、项目规划完整流程、账户/管理/故障排查；
- 开发者指南：系统架构、API 与数据契约、扩展开发、开发测试与文档；
- 两套指南 README 的章节导航。

仍需随对应功能整改同步完善：

- 设备目录和真实端口契约稳定后，补充逐设备参数与端口参考；
- 数据模板最终 schema 稳定后，补充字段、单位、示例文件和质量诊断参考；
- 财务配置和人工评估语义修复后，更新相关操作与结果解释；
- `analysis` 前端入口完成后，增加分析输入、任务和结果章节；
- 存储模块重构后，补充管理员容量、完整性巡检和恢复步骤；
- 公共 provider 接口实际发布后，把签名和完整扩展示例加入开发者指南；
- 正式发布前补齐英文用户章节，或明确产品只发布中文帮助，不能保留假双语入口。

### 12.5 验收标准

- 顶部“帮助中心”一次点击可打开文档；
- 用户指南和开发者指南是同一目录树中的两个一级节点；
- 新增 Markdown 章节只修改 `manual/` 和 SUMMARY，无需修改 React 目录常量；
- Markdown 表格、代码块、相对链接、标题锚点和非法 HTML 有渲染/安全测试；
- 直接访问深链接和刷新不会返回 404 或错误章节；
- 帮助中心在后端、数据库和计算 Worker 不可用时仍能打开；
- 删除 TS 教程正文后不存在功能文档双事实源；
- Docker 内前端构建和浏览器测试通过；
- 功能验收清单包含“对应用户指南和开发者指南是否同步更新”。

## 13. QA-E2E-01：使用 Playwright 模拟真实用户验收

### 13.1 结论

当前验证主要由 TypeScript 构建、pytest API/服务测试和少量 smoke 脚本组成。它们无法证明真实浏览器中的路由、Cookie、表单、拖拽、文件上传、下载、前后导航、响应式布局和 Markdown 深链接正确。后续每个用户可见功能必须增加 Playwright 验收，不得以“接口测试已通过”替代。

### 13.2 真实用户原则

- 从应用公开入口打开页面并通过 UI 登录；
- 优先使用 role、label、可见名称和语义结构定位，不依赖脆弱 CSS 层级；
- 业务动作通过点击、输入、选择、拖拽、上传、确认和等待完成；
- 禁止直接修改 localStorage、React 状态、数据库或调用业务 API 来跳过正在验收的步骤；
- 允许 API/数据库仅用于测试环境前置造数和结束清理，并必须与被测动作隔离；
- 每个场景使用独立用户、项目和幂等键，可重复执行并可清理；
- 页面断言之外同时检查 console error、失败网络请求和未处理异常。

### 13.3 首批场景

1. 登录、首次改密、退出和会话失效；
2. 创建项目，进入模型/数据/配置/校验/任务/结果/导出各页面；
3. 添加热泵和电池，使用真实端口完成合法连接，并验证错误连接诊断；
4. 上传年度数据，查看质量报告并绑定版本；
5. 保存非默认财务配置，重新读取、确认基准并通过校验；
6. 提交任务，观察状态变化，查看结果并下载导出；
7. 管理员切换注册、停用/启用用户并查看存储健康；
8. 从顶部进入帮助中心，验证两个一级指南、Markdown 表格/代码、章节跳转和返回；
9. 直接打开帮助章节深链接并刷新，验证仍停留在同一章节；
10. 切换中文/英文；缺失翻译必须明确提示可用语言；
11. 使用桌面和移动视口检查目录树、画布外页面和键盘焦点。

### 13.4 执行与证据

- Playwright 与应用均在 Docker 环境运行；
- Chromium 是每次变更的最低门禁，发布前增加 Firefox/WebKit 关键流程；
- 失败时保存 trace，关键视觉或交互问题保存截图，复杂失败可保存视频；
- CI 报告必须能定位场景、步骤、浏览器、console 和失败请求；
- flaky 测试必须修复根因，禁止简单增加无限等待或长期重试掩盖竞态；
- 帮助中心开发完成时，必须实际执行本节相关 Playwright 场景后才能验收。
