# pIES 架构宪法

> 状态：生效
> 版本：1.8.0
> 生效日期：2026-08-20
> 最后更新：2026-08-28
> 适用范围：根目录入口、`manual/`、`docs/`、`backend/`、`frontend/`、数据库、对象存储、Worker、测试与后续插件
> 依据：2026-08-20 架构专项审查、ADR-0001 至 ADR-0006，以及项目所有者对 Roadmap 生命周期和设备纯技术语义的明确要求

## 1. 文档效力

本文件规定 pIES 后续设计、开发、重构和审查必须遵守的整体原则。关键词含义如下：

- **必须 / 禁止**：强制要求，违反即不得合并；
- **应该 / 不应该**：默认要求，偏离时必须提交 ADR 说明理由；
- **可以**：在不违反其他条款时允许采用。

文档冲突时按以下顺序裁决：

1. 本架构宪法；
2. 已批准且明确修订本宪法的 ADR；
3. `manual/developer-guide/` 下已生效的稳定公共契约与开发者规范；
4. `manual/user-guide/` 下已确认的当前用户行为与限制；
5. `manual/changelog/` 下当前版本事实和 Roadmap 顺序；
6. `docs/reviews/` 下带明确基线的审查证据；
7. 现有代码、测试和历史兼容行为。

`docs/archive/` 没有现行规范效力。旧合同、规格和 2026-08-20 架构材料中与本文件冲突的内容不再有效，尤其包括：全局跨模块注册表、静态注册表回退、运行期热加载、旧接口兼容转发、后端替前端完成简单展示换算等设计。

## 2. 最高原则

### 2.1 开放优先于效率

系统首先保证新设备技术内容、新方程能力、新计算生成器、新执行器、新存储后端和新分析能力可通过稳定协议接入；不得为了少一次查询、少一个对象或共享一份可变状态而破坏模块独立性。

允许在证据证明存在性能瓶颈后增加缓存、索引或批处理，但缓存必须可重建，不能成为第二权威事实源。

### 2.2 正确性优先于兼容

项目尚未正式发布，不保留旧字段别名、旧模块转发、旧响应并集或静默兼容分支。前后端和开发数据库应一次性迁移到新契约。

错误输入、未注册插件、损坏对象和内部异常必须可见，禁止回退到旧逻辑、静态映射、默认接口、空数组或伪造成功结果。

### 2.3 模块自治，边界公开

每个模块拥有自己的领域规则、注册状态和持久化实现，只通过公开门面、不可变契约或事件与其他模块交互。

跨模块禁止：

- 导入以下划线开头的函数、类、常量或变量；
- 导入对方的 `loader`、`repository`、`persistence`、文件路径 helper 或内部 registry；
- 查询或修改对方 ORM 模型；
- 复制对方维护的设备、接口、算法、单位或状态映射；
- 通过捕获宽泛异常猜测对方是否可用。

### 2.4 单一职责不等于全局单例

“单一事实源”表示一个概念只有一个权威所有者，不表示建立跨模块全局对象。设备、建模、生成器、执行器和装配分别拥有自己的注册状态；存储拥有统一对象协议；组合层只负责连接这些公开能力。

### 2.5 失败可见、状态完整

模块初始化、注册、持久化和任务执行必须满足“全部成功或不发布新状态”。禁止先清空再逐项写入、半注册、部分兼容或吞掉异常。

## 3. 系统边界与总体数据流

核心业务流程为：

```text
设备技术定义/序列数据 → 声明式方程 → 装配与检查 → 计算生成 → 受控求解 → 结果适配
                                ↓            ↓          ↓          ↓
                          不可变规范装配 ─── Solver Bundle ─── 证据/结果对象
                                └────── 存储与任务基础能力 ─────────┘
```

前后端边界为：

```text
用户表单/UI 状态
      ↓ 前端 mapper：格式、百分比、简单单位和 DTO 组装
公开 HTTP DTO
      ↓ 后端：结构、权限、范围、领域约束、版本校验
应用用例
      ↓ 公开模块协议
领域模块 / 存储 / 任务
      ↓ 装配校验与规范化
ValidatedAssemblyArtifact
      ↓ GeneratorProvider 统一单位转换与文件生成
Solver Bundle
      ↓ SolverRuntime 受控执行 / ResultAdapter 解释
计算结果与证据
```

前端预检查用于即时反馈，后端校验始终是权威闸门。前端不得直接调用 Python 函数，后端不得返回只适用于某个 React 组件的临时结构。

## 4. 后端模块与职责

### 4.1 `core`：无业务状态的公共基础

负责：

- 错误与诊断基础类型；
- 单位、时间轴、ID、哈希、安全和纯数据结构；
- 不包含运行状态的通用协议类型。

禁止：

- 设备、生成器或 solver 静态注册表；
- 业务默认值；
- 数据库访问；
- 导入任何业务模块。

`core.registry` 必须退役。纯 `ParameterSpec` 等类型可迁入 `core/contracts/`，设备和计算 provider 定义分别归属其业务模块。

### 4.2 `devices`：设备规格与扩展发现

负责：

- `ies.device-model` YAML/schema 的加载、规范化与完整校验；
- 设备身份、技术常量、序列接口和声明式方程的公开描述；
- 设备 provider 注册和本模块注册表；
- 设备目录 API 所需的领域数据。

设备文件必须使用稳定 ID，并由统一 `schema_version`、规范内容摘要和校验回执固定语义；禁止为单个设备声明独立语义版本。设备文件禁止包含函数、包、模块、shell、可执行路径、价格、成本、税务、折旧或其他项目经济假设。不负责计算精度、算法选择、项目设备实例、画布布局或前端展示文案。

设备文件顶层只使用 `schema`、`schema_version`、`device`、`properties`、`interfaces` 和 `equations`：

- `device` 只表达稳定身份和显示信息；
- `properties` 只表达不随时间变化的技术常量；
- `interfaces` 只表达序列交互，统一使用 `in`、`out`、`bidirectional`、`predefined`、`blind` 五种类型；缺省类型规范化为 `blind`；
- `predefined` 只能从 `constant`、`data_repeat`、`data_predict` 获得序列，不连接其他设备；`blind` 既不连接其他设备，也不接收预定义数据；
- `equations` 以受限声明式方程表达 properties、接口序列与内部变量的技术关系，不得成为任意代码执行入口。

公开门面只导出类似 `get_device()`、`list_devices()`、`register(provider)` 的能力以及不可变 `DeviceDescriptor`。

### 4.3 `modeling`：声明式技术模型

负责：

- 设备声明式方程与内部变量契约；
- 技术方程到规范变量、关系、状态、接口流和结果映射元数据的标准转换；
- 受限方程语言、公共 AST 及技术贡献 contract 的版本兼容；
- 方程输入、输出、单位、状态和 schema 校验。

`modeling` 输出声明式变量、技术关系、状态、接口流和结果映射元数据；禁止加入价格/成本目标、启动求解器或返回求解器私有对象。`modeling` 可以消费 `devices` 的公开 descriptor/provider，禁止读取设备目录、价格文件或 profile 内部路径。不得以设备 ID 分支或私有命令映射替代设备文件中的公开技术方程。

### 4.4 `assembly`：interface 网络装配与同步闸门

负责：

- 将项目模型、配置和数据绑定构造成 `AssemblySpec`；
- 校验接口类型、载体、单位、有效区间、连接、预定义序列、方程、generator/solver 能力和整体可解性；
- 校验每个项目设备实例明确区分 `existing` 与 `new`，以及求解前必需的设备投资、固定/可变 O&M 和能源购售价格；
- 把相对资源解析为内容寻址引用并生成唯一规范装配文本；
- 签发由规范文本、SHA-256 和校验回执组成的 `ValidatedAssemblyArtifact`。

装配阶段证明业务单位与量纲兼容，但保留明确业务单位；不生成求解器文件和命令。存量设备的历史投资按沉没成本处理，不重复计入新增投资；新增设备投资必须绑定建设或容量决策，存量设备未来 O&M、剩余寿命、残值和退役成本仍可作为明确规划输入。接口缺省类型只能规范化为 `blind`，禁止默认双向接口、`_direct_plan` 或任何绕过装配检查的计算路径。装配失败必须返回诊断并阻断任务。

### 4.5 `computation`：生成、执行与结果适配

计算必须拆为三个独立边界：

- `GeneratorProvider` 只接受 `ValidatedAssemblyArtifact` 与已固定资源，确定性生成 Solver Bundle；
- `SolverRuntime` 只校验并执行 Bundle 中的结构化命令，生成 `ExecutionReceipt` 与原始输出；
- `ResultAdapter` 只把 Bundle、回执和声明输出映射为带 schema/version 的 `ComputeResult`。

GeneratorProvider 是业务单位到求解器内部单位的唯一转换边界，并从独立规划经济配置形成系统目标；不访问数据库、对象服务、网络或进程环境，不启动求解器。计算精度、离散化、算法和 solver 选择属于计算配置。SolverRuntime 不读取装配语义、不判断设备类型、不补数据或切换 solver。ResultAdapter 是结果反向单位换算的唯一边界，不读取当前项目或最新 provider。

计算模块禁止：

- 访问 HTTP、Cookie、数据库或对象路径；
- 读取前端形状；
- 猜测单位、补业务默认值或接受未经校验的原始装配；
- 直接选择未注册设备实现；
- 在装配 YAML 中接受命令、脚本、实现路径或环境变量；
- 用 shell 字符串执行 solver，或让每个生成器自行管理 subprocess。

### 4.6 `finance`：财务计算

负责现金流、税、折旧、融资、折现、NPV、IRR、LCOE、回收期和财务状态分类。输入是已经使用规划经济配置形成的逐时/年度结果与独立财务参数，输出为不可变 `FinancialResult`。finance 不从设备技术定义读取价格，也不能替代求解前影响容量和运行决策的经济输入。

`finance` 不依赖 HTTP、数据库或前端，不反向依赖应用服务。

### 4.7 `analysis`：结果分析

负责敏感性、批量扫描、指标和评估的纯分析能力。系统评估与人工评估必须是不同命令和 DTO；禁止把人工输入静默转换成系统评估。

### 4.8 `storage`：对象与引用生命周期

负责：

- 内容寻址、哈希和完整性校验；
- 对象元数据、owner 引用、清理、保留和恢复；
- 文件系统或其他 BlobStore provider；
- 存储容量和本模块健康状态。

项目、数据集、证据和导出模块只能持有公开 `ObjectId/ObjectHandle`，不得拼路径、读取 `StoredObject` ORM 或自行落盘。

### 4.9 `application`：跨模块用例编排

目标目录使用 `application/` 表达用例层。迁移期间现有 `services/` 可以逐步承担此职责，但不得继续成为无边界的公共杂物目录。

负责：

- 权限检查后的业务用例；
- 调用多个模块的公开接口；
- 事务边界；
- DTO 与领域命令之间的映射；
- 提交任务、创建快照、导出和删除生命周期协调。

应用层可以依赖多个模块，但不能访问模块内部实现。

### 4.10 `worker`：异步执行

负责领取任务、租约、重试、超时、编排 GeneratorProvider/SolverRuntime/ResultAdapter 和提交结果。Worker 启动时必须验证快照所需设备内容、方程 contract、generator、executor、solver 与 result adapter 可解析；缺少依赖则启动失败或不进入 ready 状态，不得领取后再临时降级。Worker 不拼 solver 命令，也不解释装配业务。

### 4.11 `api`：传输适配

负责：

- HTTP 路由、认证依赖和 Pydantic DTO；
- HTTP 状态码、序列化与标准错误信封；
- 调用 application 用例。

禁止直接查询 ORM、拼对象路径、实现领域计算、组织跨端点工作流或返回兼容响应并集。

### 4.12 `persistence` / ORM

数据库表按领域归属。ORM 是模块内部实现，不是跨模块 DTO。目标状态下，每个持久化模块拥有自己的 repository；`models/` 在迁移期只是物理集中目录，不构成允许所有模块任意查询所有表的授权。

## 5. 后端依赖与交互规则

### 5.1 允许的交互形式

跨模块只允许：

1. 调用对方 `__init__.py` 或明确 `public.py` 导出的门面；
2. 实现对方公开的 `Protocol`/provider；
3. 传递不可变 contract/dataclass/Pydantic model；
4. 通过 application 层编排多个公开接口；
5. 对不要求立即一致的行为发布版本化事件。

业务模块之间不共享可变字典、SQLAlchemy Session 内部状态或文件句柄。

### 5.2 组合根

应用启动组合层是唯一允许选择具体 provider 并完成依赖注入的位置。它可以：

- 发现并注册设备内容 provider、计算生成器、执行器、结果适配器和存储适配器；统一方程 parser/validator 是版本化公共 contract，不建立每设备命令注册表；
- 校验公开 ID、版本和依赖能否解析；
- 发布模块健康状态。

它禁止读取模块内部 registry、合并成新的全局权威表或修改业务数据。

### 5.3 注册表

- 每个模块独立拥有注册表；
- 注册项必须有稳定 ID 和显式版本；
- 候选注册表必须完整构建、校验后一次性发布；
- 任一必需 provider 失败时启动失败；
- 正式发布前不实现运行期热加载；
- 禁止静态 fallback、宽泛异常回退和部分注册状态；
- `devices`、`computation` 与 `storage` 之间不共享注册表对象；`modeling` 和 `assembly` 不为设备建立第二注册表。

扩展 ID 应采用稳定命名空间，例如 `ies.device.pv`、`vendor.device.foo`。设备文件自身不声明独立语义版本，以规范内容摘要固定；provider、generator、solver、executor 和 result adapter 使用语义化版本。破坏性 schema 变化必须提升主版本。

### 5.4 事务

- application 用例拥有 `commit/rollback`；
- repository 和领域服务只能 flush、使用 savepoint 或返回错误；
- 下层模块禁止对调用方共享 Session 执行全局 `rollback()`；
- 唯一键竞争使用 upsert 或嵌套事务处理；
- 跨数据库和文件系统不能伪装为单一 ACID 事务，必须设计幂等恢复协议。

### 5.5 同步与异步

- 快速校验、查询和确定性变更使用同步 HTTP；
- 长时间计算、分析、导入、报告生成使用 Task；
- Task 请求必须携带幂等键；
- Task 消费不可变快照，不读取不断变化的当前草稿；
- Worker 结果以不可变证据或对象提交，状态转换必须可重试。

## 6. 目标目录结构

目录按稳定业务边界组织。迁移允许分阶段进行，但新增代码必须优先落入目标结构。

仓库根目录必须保持为项目统一入口和顶层能力导航，不堆放专题说明或临时审查文件：

```text
pIES/
├── README.md               # 唯一项目入口，分流三个正式文档入口
├── LICENSE
├── AGENTS.md               # agent 工作约束及宪法入口
├── docker-compose.yml
├── manual/                 # 使用者指南、开发者指南、更新日志/Roadmap
├── docs/                   # Review 证据和只读历史归档
├── backend/
├── frontend/
└── data/                   # 运行数据，不作为文档来源
```

正式产品文档目录使用正确英文名称 `manual/`，禁止使用拼写错误的 `mannal/`。`README.md` 继续作为原有项目入口，不另建与其竞争的站点根入口。

```text
backend/iesplan/
├── bootstrap/              # 组合根、provider 发现、启动校验
├── core/
│   ├── contracts/          # 无状态纯类型
│   ├── diagnostics.py
│   ├── errors.py
│   ├── units.py
│   └── timeaxis.py
├── devices/
│   ├── __init__.py         # 公开门面
│   ├── contracts.py
│   ├── registry.py
│   ├── providers.py
│   └── catalog/            # ies.device-model YAML 与配套数据样例
├── modeling/
│   ├── __init__.py
│   ├── contracts.py
│   ├── parser.py
│   ├── validator.py
│   └── canonicalizer.py
├── assembly/
│   ├── __init__.py
│   ├── contracts.py
│   ├── parser.py
│   ├── validator.py
│   ├── canonicalizer.py
│   └── artifact.py
├── computation/
│   ├── contracts.py
│   ├── generators/
│   │   ├── registry.py
│   │   └── providers/
│   ├── runtime/
│   │   ├── contracts.py
│   │   └── executors/
│   └── result_adapters/
├── finance/
├── analysis/
├── storage/
│   ├── __init__.py
│   ├── contracts.py
│   ├── service.py
│   ├── persistence.py
│   └── adapters/
├── application/
│   ├── projects/
│   ├── datasets/
│   ├── modeling/
│   ├── config/
│   ├── tasks/
│   ├── results/
│   ├── exports/
│   └── operations/
├── api/                    # 与 application 用例一一对应的 HTTP 适配
├── worker/
└── migrations/
```

前端按 feature 垂直切分：

```text
frontend/src/
├── app/                    # 路由、provider、应用装配
├── shared/
│   ├── api/                # 纯 HTTP、会话、错误解析
│   ├── ui/
│   ├── i18n/
│   ├── format/
│   └── types/
├── features/
│   ├── auth/
│   ├── projects/
│   ├── modeling/
│   ├── datasets/
│   ├── config/
│   ├── validation/
│   ├── tasks/
│   ├── results/
│   ├── exports/
│   └── admin/
└── pages/                  # 只做路由级组合
```

每个 feature 推荐包含：

```text
feature/
├── api.ts                  # 该 feature 的后端调用
├── contracts.ts            # 与后端 JSON 一一对应
├── model.ts                # 前端领域模型
├── form.ts                 # UI 临时表单类型
├── mappers.ts              # 纯转换
├── hooks/                  # 查询、mutation、用例
└── components/             # 展示组件
```

依赖方向必须是 `app/pages → features → shared`。feature A 不得导入 feature B 的内部文件；跨 feature 工作流放在 page 或显式 application hook。

## 7. 数据类型与序列化规范

### 7.1 基本格式

- HTTP JSON 使用 UTF-8、`application/json`；
- 字段名统一 `snake_case`，前后端 contract 保持同名，不做隐式 camelCase 转换；
- 每个长期保存的文档、快照、装配文件、证据和插件规格必须有 `schema_version`；
- 插件文件还必须有独立 `schema` 标识；设备模型 YAML、设备数据 CSV、装配 YAML 和 Solver Bundle 分别使用 `ies.device-model`、`ies.device-data`、`ies.assembly`、`ies.solver-bundle`；
- 枚举值使用小写 `snake_case`，枚举之外的值必须被拒绝；
- 不允许以 `Record<string, unknown>` 代替已经稳定的公开 DTO；
- 不允许同一字段有多种未标记形态，例如有时数字、有时对象、有时字符串。

### 7.2 标识符

- 数据库内部主键可以使用 `BIGINT`；
- 对外 JSON 中的标识符必须作为不透明十进制字符串传输，前端类型使用品牌化 `EntityId`/具体 `ProjectId`，不得参与算术；
- 稳定插件、设备、generator、solver、executor 和 result adapter ID 使用命名空间字符串；设备方程随设备内容寻址，不再拥有独立命令 ID；
- 哈希使用小写 64 位十六进制 SHA-256 字符串；
- ID 类型不能与普通字符串、名称或数组下标混用。

### 7.3 数值

- 物理连续量使用 JSON number，后端计算使用 `float64`；
- 金额、费率结算值和必须精确往返的小数使用十进制定点字符串，后端用 `Decimal`，数据库用 `NUMERIC`；
- 比例的 API 规范值统一为 `0..1`；前端负责把百分比表单转换为规范比例，后端校验范围；
- 整数计数和年份必须是 JSON integer，不接受隐式字符串转数字；
- `NaN`、`Infinity`、`-Infinity` 禁止进入 JSON、数据库或快照；
- 缺失值使用 `null` 仅表示领域上允许“无值”，不能表示加载失败或兼容默认。

### 7.4 单位

系统区分三处表示：

1. 前端展示单位；
2. API/业务契约规定的规范单位；
3. 生成器/求解器内部单位。

规则：

- 单位必须由设备 property、接口或字段 schema 明确声明；
- 含义不固定的裸数值必须使用 `{value, unit}` 或由所在 schema 明确单位；
- 前端 mapper 负责简单展示换算并生成后端要求的规范输入；
- 后端校验单位、量纲、范围和领域约束；
- 装配阶段校验量纲和单位兼容并保留明确业务单位；业务单位到 solver 内部单位只在 GeneratorProvider 边界发生；反向换算只在 ResultAdapter 边界发生；
- 技术方程解析、运行时和业务服务中禁止散落 `×1000`、`×3600`、百分比 `/100` 等隐式换算；
- 单位 ID 与符号由 `core.units` 的无状态规范定义，设备 property 与接口引用该规范，不复制映射。

### 7.5 时间

- HTTP、数据库、事件、规范装配和证据中的时间戳使用 ISO 8601 UTC 字符串并带 `Z`；
- 人工编写的 `ies.device-data` CSV 可以使用文件级 `fixed_utc_offset_minutes` 解释无偏移本地时间，但不得依赖机器时区或隐式夏令时；进入规范数据和装配后必须转换为 UTC；
- 数据库存储 `TIMESTAMPTZ`；
- 项目时区语义使用 `fixed_utc_offset_minutes` 明确保存；
- 持续时间使用明确单位或 ISO 8601 duration，不用含义不明的整数；
- 时间序列必须声明分辨率、起点、点数和单位；数组长度必须与时间轴一致。

### 7.6 集合与顺序

- 列表是否有序必须在 DTO 中明确；
- 需要稳定哈希的集合必须在规范化前按稳定键排序；
- JSON object 的字段顺序不具有语义；
- 分页列表使用 cursor，不使用在并发变更下不稳定的页码作为长期契约；
- 大型逐时数组不得无条件嵌入普通资源 DTO，应按字段、时间范围或对象引用读取。

### 7.7 不可变内容与哈希

- 快照、版本、证据和已发布结果不可原地修改；
- 哈希必须基于版本化、规范化后的字节计算；
- 规范化算法必须唯一并由公开纯函数实现；
- 哈希、`schema_version`、创建来源和依赖版本必须共同保存；
- 草稿可变更，但每次持久化形成新修订，不覆盖历史对象。

### 7.8 公共文件契约

- 设备模型 YAML、设备数据 CSV 和装配 YAML 必须可按开发者指南直接手写，并使用统一 schema 校验；
- 设备模型 YAML 只保留纯技术语义：稳定设备身份、非时变 properties、序列 interfaces 和声明式 equations；价格、成本和计算精度不得进入设备文件；
- 设备序列接口只有 `in`、`out`、`bidirectional`、`predefined`、`blind` 五种；`predefined` 仅允许 `constant`、`data_repeat`、`data_predict`，`blind` 不连接且不接收预定义数据；
- YAML 使用 YAML 1.2 安全子集，禁止自定义 tag、anchor、alias、合并键、重复键和任意对象构造；
- 文件路径只能是所属包内规范相对路径，禁止绝对路径、`..`、符号链接逃逸和宿主机路径；
- 未知核心字段默认拒绝；扩展只能放在命名空间化 `extensions`，不得改变核心语义或安全规则；
- 装配 YAML 禁止 shell、command、executable、函数/模块路径、环境变量和凭证；
- 原始装配通过结构、模型/数据、图/系统、计算兼容四阶段校验后，才能生成 `ValidatedAssemblyArtifact`；
- Solver Bundle 只能由已注册 GeneratorProvider 生成，必须包含输入摘要、结构化命令、输出声明和 ResultAdapter 精确版本；
- Bundle 命令以受信任 executor/executable ID 和参数数组表达，禁止 `sh -c`、管道、重定向、替换、通配和未声明网络；
- 四种 schema 独立语义化版本；不能识别的 MAJOR 必须拒绝，不得猜测或静默降级。

具体字段和人工示例以[文件格式标准](file-formats.md)为唯一正式说明。

## 8. HTTP API 契约

### 8.1 DTO

DTO 是请求和响应的明确数据结构，不等于为前端增加额外业务接口。后端 Pydantic DTO 和前端 `contracts.ts` 必须字段一一对应，并通过 OpenAPI/契约测试校验。

DTO 禁止：

- 暴露 ORM、文件路径、内部函数名或 registry 对象；
- 包含仅为某个页面准备的临时展示状态；
- 依赖前端调用顺序才能解释；
- 用默认值掩盖必需字段缺失。

### 8.2 成功响应

成功资源响应顶层只允许 1-3 个键，每个键名直接表达资源语义（如 `{project}` / `{items, next_cursor}` / `{ok, ...}`）。禁止 `{data, meta}` 这种通用包装。必要时可嵌套（嵌套键同样遵循自我文档化原则），列表与分页/版本/追踪分置同顶层两个键。

状态码与包装键的全局统一规则见 [contracts.md](contracts.md)「HTTP 语义」与「成功与错误」节（ADR-0005）。

### 8.3 错误与诊断

错误统一使用：

```json
{
  "error": {
    "code": "DOMAIN-CATEGORY-001",
    "message_key": "ies.error.example",
    "severity": "error",
    "blocking": true,
    "params": {},
    "location": null,
    "fix_hint_key": null,
    "ref_ids": []
  }
}
```

后端不得把堆栈或内部路径返回给客户端。面向用户的本地化文案由前端根据 `message_key + params` 生成；日志可以包含受控技术详情和 request ID。

错误码 `code` 字段格式 `DOMAIN-CATEGORY-NNN`（域-类别-三位序号），`DOMAIN` 是 API 子域（API/PROJ/TASK/DATA/CONFIG/OBJ/PERM/AUTH），`CATEGORY` 是错误类别（REQ/VAL/NF/CONFLICT/SEC/QUOTA/MISS 等）。同 `code` 可跨 message_key 复用（同语义不同文案），但禁止跨 code 共享 message_key。新码须在 `core/diagnostics.py NEW_DIAG_CODES` 登记。详细规则见 ADR-0005 与 [contracts.md](contracts.md)「成功与错误」节。

禁止捕获契约转换或主资源错误后返回空列表、“暂无数据”或 HTTP 200。

### 8.4 HTTP 语义

- `GET` 只读且可安全重试；
- `POST` 创建资源或执行命令；
- `PUT` 完整替换；
- `PATCH` 显式部分更新；
- `DELETE` 执行明确生命周期操作；
- 冲突使用 `409`，校验失败使用 `400/422` 的项目统一选择（详细语义与选择规则见 [contracts.md](contracts.md)「HTTP 语义」节，ADR-0005），认证使用 `401`，授权使用 `403`，不存在使用 `404`；
- 创建返回 `201`，异步任务接受返回 `202`；
- 可重试写操作必须支持幂等键；
- 并发编辑必须使用 revision/ETag/If-Match 等明确乐观锁，禁止最后写入静默覆盖。

### 8.5 上传与下载

- 上传使用 multipart 或预签名对象协议，API 明确大小、媒体类型和摘要限制；
- 下载返回短期授权或标准资源链接，不让前端拼存储路径；
- 导出响应返回真实资源 ID、文件名、摘要和过期时间，前端不得伪造临时报告 ID；
- 浏览器文件名和展示格式属于前端，内容摘要与授权属于后端。

## 9. 前端职责与交互

### 9.1 基础 HTTP 层

`shared/api` 只负责 URL、query、fetch、Cookie、超时、取消、JSON/FormData/Blob 和标准错误解析。它不得导入 Project、Device、Task 等业务类型，也不得发起隐式第二个业务请求。

### 9.2 Feature API 与 mapper

- 每个 feature 拥有自己的 `api.ts` 和 `contracts.ts`；
- DTO 到前端领域模型的转换只存在于 `mappers.ts`；
- 表单字符串、百分比和本地单位只存在于 `form.ts`；
- 页面和组件不得直接拼后端 JSON；
- mapper 是纯函数，不访问网络、缓存或 React 状态。

### 9.3 应用编排

多接口工作流放在 feature hook/use-case 或 page 组合层。底层 API 客户端禁止：

- 自动创建缺失评估；
- 通过模块级 Map 反查业务关联；
- 静默丢弃用户输入；
- 为缺失后端能力伪造资源；
- 根据旧响应形状进行多分支猜测。

### 9.4 服务器状态

- 服务器状态必须有一个明确缓存来源；
- query key、失效、轮询、取消和竞态策略必须显式；
- 模块级全局 Map 不得作为业务事实；
- React local state 只保存未提交表单、选中项和弹窗等瞬时 UI；
- 主请求失败必须展示错误和重试入口，只有明确 optional 区块可以局部降级。

### 9.5 Schema 驱动 UI

设备 properties、序列接口、有效区间、单位和方程必须来自公开 schema；规划变量、计算精度和 generator/solver 能力来自计算配置公开 schema。前端可以保留通用交互规则，如禁止自环和重复边，但不得按设备类型硬编码热泵、电池或燃气接口规则。

画布必须使用后端返回的真实 interface ID，并按 carrier、五类接口兼容规则和公开接口语义连接；`predefined` 与 `blind` 均不得连接其他设备。

## 10. 存储与数据生命周期

### 10.1 公开协议

存储模块至少提供以下语义：

```python
class ObjectStore(Protocol):
    def put(self, content: bytes, media_type: str) -> ObjectHandle: ...
    def get(self, object_id: ObjectId) -> bytes: ...
    def stat(self, object_id: ObjectId) -> ObjectHandle: ...
    def attach(self, object_id: ObjectId, owner: ObjectOwner) -> None: ...
    def detach(self, object_id: ObjectId, owner: ObjectOwner) -> None: ...
```

该协议是模块内调用能力，不要求全部映射成 HTTP 端点。

### 10.2 路径与适配器

- 全仓库只有存储适配器可以解释 `storage_path`；
- 数据库中路径必须相对一个明确根目录，禁止不同模块二次拼根；
- 文件系统、S3 等通过 adapter/provider 替换；
- 不允许导出对象后再静默写一份非托管副本。

### 10.3 引用

- `ObjectOwner(namespace, id, purpose)` 是公开引用契约；
- 引用清单是权威事实；
- `ref_count` 如保留只能是可重建缓存；
- 任意存在的 owner 引用都阻止清理，存储不硬编码业务表名；
- 创建、替换、删除和过期流程必须成对 attach/detach；
- 保留策略必须说明软删除后哪些引用继续有效。

### 10.4 故障恢复

文件和数据库之间必须使用确定性路径、原子 rename、幂等 upsert 和 reconciliation。至少处理：

- 超龄临时文件；
- 有文件无元数据；
- 有元数据无文件；
- 摘要或大小不一致；
- 清理中文件已删但事务失败；
- 并发写入相同内容。

容量未知、对象损坏或 provider 不可用时，新写入必须失败，readiness 降级；禁止静默放行。

### 10.5 管理 API

`/admin/storage` 只返回存储模块公开状态。全系统 `/health`/`readyz` 由 operations 聚合各模块公开 health provider，禁止存储 API 直接查询任务、项目、用户和队列内部表。

## 11. 数据库与持久化

- schema 变化必须通过版本化 migration，不依赖运行时 `create_all` 作为发布机制；
- 外键、唯一约束、检查约束和不可变性尽量由数据库保证；
- repository 只属于表的领域所有者；
- 跨领域查询通过公开 read model/application query，不直接导入对方 ORM；
- 审计记录不可变，业务模块通过公开审计接口或版本化事件写入；
- 删除行为必须明确是软删除、硬删除还是保留，不能以模糊状态代替生命周期；
- 开发数据不需要长期兼容，使用一次性迁移或重建；
- 敏感信息、密码哈希、令牌和内部路径不得进入普通 DTO、日志或证据包。

## 12. 快照、任务与结果

- Task 必须绑定不可变 `CalcSnapshot`；
- 快照包含 `ValidatedAssemblyArtifact`、输入内容哈希、schema 版本、设备规范内容摘要、generator/solver/executor/result adapter 精确版本、单位契约、随机种子、选项和容差；
- 每个计算 attempt 必须封存 Solver Bundle、ExecutionReceipt、stdout/stderr 摘要、声明输出摘要和统一结果；
- 同一幂等键重复提交返回同一逻辑任务，不重复执行或扣配额；
- 任务状态机只有公开允许的转换；
- 失败结果必须保留结构化诊断；
- 结果、证据和评估必须可追溯到任务、attempt、快照和对象摘要；
- 系统评估与人工评估分别记录 assessor、输入、时间和审计信息；
- 不得通过重新读取当前配置解释历史结果。

## 13. 故障与健康语义

| 故障 | 要求 |
|---|---|
| 插件/provider/注册失败 | 启动失败或不 ready，不发布部分注册状态 |
| 数据库不可用 | 不 ready；依赖数据库的请求明确失败 |
| 存储容量未知或不足 | 不 ready 或存储 degraded；拒绝新写入 |
| 对象缺失/损坏 | 返回损坏诊断，禁止空内容或旧副本回退 |
| 项目/装配输入非法 | 返回完整诊断并阻断任务 |
| Worker 缺设备内容/方程 contract/generator/executor/result adapter | Worker 不 ready，不领取任务 |
| 生成器失败或结果不确定 | 不发布部分 Bundle；provider 不 ready 或 attempt 明确失败 |
| Bundle 摘要、路径或命令策略非法 | runtime 不启动进程，形成拒绝回执 |
| solver 超时、取消、OOM 或异常退出 | 终止进程组，封存失败回执，不伪装成功 |
| 可重试外部故障 | 按明确策略重试，保留 attempt 和原因 |
| 内部异常 | 记录 request ID 和堆栈；客户端收到标准 500 错误 |

`healthz` 只表示进程存活；`readyz` 表示实例具备承接流量/任务的必要依赖。不得用健康检查的“degraded”掩盖实际上无法正确处理请求的状态。

## 14. 测试与架构门禁

### 14.1 环境

所有编译、测试、格式化和数据库验证必须在 Docker 环境运行，不得在主机安装或执行项目依赖。只允许读写本仓库和 `/tmp`。

### 14.2 必需测试

每项变更按风险至少覆盖：

- 纯函数单元测试；
- 模块公开协议测试；
- HTTP DTO 契约测试；
- 数据库约束和事务测试；
- 前后端关键值往返测试；
- Worker 幂等、租约和失败恢复测试；
- 四种文件 schema、规范化、摘要和非法人工样例测试；
- generator 确定性与 Bundle contract 测试；
- runtime 命令注入、路径逃逸、隔离、取消、超时和输出边界测试；
- result adapter 状态、单位和非有限值映射测试；
- 存储互操作、引用对称和 reconciliation 测试；
- 关键用户流程浏览器测试。

测试不得只断言字段存在，还必须验证关键业务值、单位、ID、版本和错误语义。

### 14.3 静态架构测试

CI 必须逐步加入并最终强制：

- 禁止跨模块导入私有符号；
- 禁止导入其他模块的 loader/repository/persistence；
- 禁止 API 直接导入 ORM；
- 禁止业务模块拼对象路径；
- 禁止 `core` 依赖业务模块；
- 禁止前端 `shared` 依赖 feature；
- 禁止页面直接调用底层 HTTP；
- 检测后端/前端重复设备、generator 和 solver 映射；
- 检测技术模型转换/runtime/业务服务中的隐式单位换算常量；
- 禁止装配和设备文件出现实现模块路径、shell 命令、价格或成本；
- 禁止 GeneratorProvider 访问网络、数据库、对象存储或启动进程；
- 禁止 runtime 按设备/generator/solver 名称增加业务分支。

### 14.4 完成定义

一项功能只有同时满足以下条件才算完成：

1. 公开 contract 已定义且版本语义明确；
2. 实现只依赖允许的公开边界；
3. 失败路径和权限已定义；
4. DTO 和前端 mapper 同步；
5. 数据迁移或开发数据重建方案已提供；
6. Docker 内相关测试通过；
7. 无静默兼容、fallback、空数据降级或未管理副本；
8. 文档与代码一致。

### 14.5 真实用户验收

用户可见功能在合并前必须进行浏览器级验收。除画布拖放与接口连接按仓库约束由人工核查外，其余 GUI 流程使用 Playwright 模拟真实用户，而不是只验证接口返回：

- 从页面入口开始，通过可见文本、role、label 等可访问定位器操作；
- 登录、导航、填写表单、上传、确认、等待和下载等业务动作通过 UI 完成；
- 画布拖放与接口连接由人工完成，并记录设备、接口、操作步骤和可见结果；画布 property 编辑等其他功能仍由 Playwright 验收；
- 禁止直接修改 localStorage、组件状态或数据库来跳过被测步骤；
- API 可以用于隔离环境的前置造数和结束清理，但不能代替本场景要验收的用户动作；
- 覆盖桌面和移动视口、中文和已发布英文内容、前进后退、深链接与刷新；
- 断言页面可见结果，同时检查 console error、失败请求和未处理异常；
- 失败时保留 trace，并按风险保存关键截图或视频；
- 测试数据必须隔离、可重复、可清理，不能依赖开发者浏览器已有会话；
- Playwright 和被测服务全部在 Docker 环境运行。

关键主流程至少包括：认证、项目生命周期、设备 property 与真实接口、数据上传、配置、校验、任务、结果、导出、管理员操作和帮助中心。纯 HTTP 契约测试不能替代浏览器或人工验收。

## 15. 项目文档

### 15.1 单一正式文档体系

项目不再维护与正式指南并行的活跃开发过程文档。长期使用说明、设计蓝图、版本历史和开发顺序统一收敛到 `manual/`：

```text
README.md                                  # 项目唯一总入口
manual/                                    # 唯一长期正文来源
├── README.md                              # 三个正式入口
├── SUMMARY.md
├── SUMMARY.<locale>.md
├── user-guide/                            # 使用者指南
├── developer-guide/                       # 开发者指南与架构宪法
└── changelog/                             # 倒序更新日志与 Roadmap

docs/                                      # 后台证据，不是开发输入
├── README.md
├── reviews/                               # 带日期和基线的审查快照
└── archive/                               # 停止维护的历史材料
```

`manual/` 是帮助中心和仓库阅读者共同使用的唯一正文。`docs/reviews/` 只提供证据，`docs/archive/` 没有现行规范效力。

### 15.2 根入口与三个正式入口

- 根 `README.md` 必须保持简短、可执行，链接使用者指南、开发者指南、更新日志、Review/归档、许可和安全提示；
- `manual/README.md` 下必须有“使用者指南”“开发者指南”“更新日志”三个并列入口；
- 架构宪法保持在开发者指南固定路径，作为最高规范；
- 根 README 和子入口不得复制完整手册或 Roadmap；
- 任何文档重组都必须保持入口、语言 SUMMARY 和帮助中心路由有效。

### 15.3 使用者指南

使用者指南面向第一次接触产品的规划工程师、能源工程人员和系统管理员，按 GUI 任务组织。每项任务必须说明：

1. 开始前的条件；
2. 页面、按钮和字段；
3. 可执行步骤；
4. 可观察的预期结果；
5. 失败处理、权限和当前限制。

指南只描述当前 GUI 确实可完成的操作。未实现能力必须移入 Roadmap；不能用后端接口存在推断 GUI 已交付。禁止暴露 ORM、私有函数、内部路径、堆栈、实现争议或无助于操作的后台术语。

### 15.4 开发者指南

开发者指南是后续开发的设计蓝图，面向维护者、集成开发者、扩展开发者和贡献者。它至少覆盖：

- 架构原则、系统上下文、模块公开边界和依赖方向；
- 领域模型、不可变追溯链和对象生命周期；
- 公共 HTTP、DTO、诊断、单位、时间和版本规则；
- 设备模型 YAML、设备数据 CSV、装配 YAML、Solver Bundle 及设备、技术模型、生成器、执行器、结果适配器、存储和数据 provider；
- 前端、帮助中心、部署、运行、测试和贡献规范。

开发者指南描述稳定意图、公开不变量和扩展方式，不写文件行号、私有函数签名、ORM 表清单、迁移 SQL、实施 agent 分工或临时默认值。实现成熟度由更新日志与 Roadmap 说明。

### 15.5 更新日志与 Roadmap

`manual/changelog/README.md` 使用三段式产品版本，按新版本在上维护已经实现但尚未发布的用户可感知变化和公共迁移影响。`Unreleased` 不是计划清单；发布时转为版本号和日期。

`manual/changelog/roadmap.md` 是唯一开发顺序来源。Roadmap：

- 按目标产品版本组织，不使用“版本 1 / 版本 2”混淆主版本；
- 只写尚未实现的目标、顺序、依赖和退出标准；
- 不包含后台实施流水账；
- 不能修改架构宪法或已生效公共契约；
- 未经实现和验收的内容不能提前进入使用者指南；
- 任一事项达到完成定义并通过验收后，必须在同一次文档变更中写入 `Unreleased`，并从 Roadmap 删除；
- 部分完成时只删除已完成子项，Roadmap 只保留剩余工作；目标版本没有剩余事项时删除整个版本段落；
- 禁止使用完成勾选、删除线或“已完成”段落把 Roadmap 变成历史记录，完成历史只保留在更新日志。

### 15.6 Review 与归档

- 新 Review 只放 `docs/reviews/`，必须注明日期、审查对象、提交基线、结论和验证证据；
- Review 不自动表示当前事实，也不能直接充当实现规格；
- 已被提炼、替代或停止维护的合同、规格、计划、调研、ADR 和工作流放入 `docs/archive/`；
- 归档原文保持历史语义，不为了当前规则反复改写；内部链接可能过时，索引必须明确提示；
- Review 或归档中的稳定结论只有提炼进入 `manual/` 后才成为长期规范。

### 15.7 文档元数据与命名

长期规范、ADR 和 Review 应标明标题、稳定 ID、状态、版本或日期、适用范围、所有者、上游依据和替代关系。

普通路径使用小写 `kebab-case`；约定俗成的根文件和本宪法可以使用大写。禁止使用 `new`、`final2` 等含糊名称。日期适用于 Review 快照，不用于替代稳定章节 ID。

### 15.8 文档与代码同步

- 用户可见行为、字段、单位、错误或页面变化时，同一变更更新使用者指南与更新日志；
- 公共 contract、扩展、架构、部署或维护方法变化时，同一变更更新开发者指南与更新日志；
- 后续计划变化只更新 Roadmap；
- 破坏性架构变化先按第 17 节提交 ADR 并修宪；
- OpenAPI、schema、错误码表等可生成内容由权威源生成，禁止手工维护第二份；
- 当前行为示例必须能在当前版本运行；标记为“生效目标契约”的示例必须 schema 完整，并在 Roadmap 明确实现版本，不能声称当前代码已兼容；所有示例均不得包含真实秘密；
- 删除或移动文档时必须修复现行文档、代码注释、测试说明和入口链接。

### 15.9 文档验收

文档变更至少检查：

- 根 README、三个正式入口、Review/归档入口和所有 SUMMARY 链接有效；
- 使用者指南可由零基础用户按 GUI 执行，且不把目标状态写成现有功能；
- 开发者指南覆盖关键架构意图但不复制后台实现细节；
- 更新日志倒序、Roadmap 顺序和三段式版本一致；
- 命令只使用 Docker 工作流并与实际服务名一致；
- 示例 DTO、枚举、单位和错误信封与公开契约一致；
- Markdown、代码块、表格、标题层级和已发布语言导航通过检查。

### 15.10 网页帮助中心

产品顶部统一入口名称为“帮助中心”（英文 `Help Center`），直接呈现 `manual/` 中的正式 Markdown，不维护另一套正文。

帮助中心必须：

- 在同一目录树中显示使用者指南、开发者指南和更新日志三个一级节点；
- 由 `manual/SUMMARY.md` 登记语言，`manual/SUMMARY.<locale>.md` 规定顺序和稳定章节 ID；
- 支持深链接、标题锚点、前后章节、当前节点、移动目录和键盘导航；
- 支持必要 Markdown，并拒绝原始 HTML、脚本、内联事件和不安全 URL；
- 在前端构建阶段生成只读内容清单，运行时不读宿主机文件，也不请求后端做简单 Markdown 转换；
- 语言缺失时明确列出可用语言，不静默展示错误语言；
- 在后端、数据库和 Worker 不可用时仍能静态阅读。

## 16. 安全与审计

默认部署模型是管理员集中部署、普通使用者通过界面和 API 使用。管理员属于受信任运维角色；以下安全条款主要防护未登录请求、外部请求、普通使用者越权和不可信输入，不把管理员主动修改本身视为攻击事件。管理员权限区分主要用于范围提示、误操作防护和普通使用者的越权防护，不要求管理员相互隔离或双人审批。

- 后端始终是认证、授权和业务权限的权威；
- 前端路由守卫只改善体验，不能替代后端鉴权；
- Cookie、令牌和下载授权使用最小权限与明确有效期；
- 对权限、项目所有权、对象清理、项目/数据/计算配置、恢复和人工评估等关键变更保留不可变、最小化的脱敏审计；普通查看和日常运维不要求重型审计；
- 审计只保存必要的脱敏元数据，不保存密码、原始令牌或不受控大对象；
- 错误响应不泄露堆栈、SQL、主机绝对路径或 provider 凭证；
- 装配和设备文件不得提供任意代码执行入口；
- SolverRuntime 只能执行 allowlist 中 executor/executable ID 对应的参数数组，禁止经过 shell；
- 工作目录和文件必须限制在隔离根内，普通 solver 默认断网，环境变量从空白 allowlist 构造；
- 取消、超时和租约失效必须终止并回收整个进程组，不能留下脱管 solver。

## 17. 变更与修宪流程

普通实现不得自行绕过本文件。确需改变强制条款时，必须先提交 ADR，至少说明：

- 要解决的问题与证据；
- 被修改的条款；
- 对开放性、正确性、安全和迁移的影响；
- 被否决的替代方案；
- 前后端、数据和测试迁移计划；
- 回滚方案。

ADR 获得明确批准后，应同时修改本文件版本。临时性能优化、赶工或“现有测试就是这样”不能作为违反边界的理由。

ADR 在评审期间作为 `docs/reviews/` 中的决策审查材料；批准并完成修宪后，其稳定结论进入本宪法或开发者指南，原 ADR 移入 `docs/archive/adr/`，不再形成第二份长期规范。

## 18. 版本化开发顺序

产品版本使用 `MAJOR.MINOR.PATCH`。当前开发基线为 `0.1.0`，首个正式发布版本为 `1.0.0`。不兼容架构或公共契约重构提升主版本，向后兼容的新功能提升次版本，缺陷修复提升修订版本；正式发布前的具体规则见开发者指南[版本化与发布](versioning-and-release.md)。

开发顺序、发布范围和退出标准只在 [`manual/changelog/roadmap.md`](../../changelog/roadmap.md) 维护。本宪法只保留跨版本不变量，不再复制阶段任务清单。

Roadmap 必须始终优先处理：

1. 错误语义、安全阻断和双事实源；
2. 公开模块协议、事务和追溯正确性；
3. 用户主流程、严格前后端契约和正式文档；
4. 恢复、可运维性、扩展验收与发布门禁。

Roadmap 的任何阶段都不得恢复全局注册表、装配 fallback、旧接口兼容转发、空数据降级或其他违反本宪法的路径。

## 19. 最终验收原则

一个开放且清晰的 pIES 应满足：

- 新增设备不需要修改前端接口规则或旧静态注册表；
- 新增设备技术模型和数据样例可只按公共 YAML/CSV 标准交付，不暴露实现路径，不携带价格、成本或计算精度；
- 新增 generator 不需要修改装配、API、GUI 或通用 runtime 的映射表；
- 新增 solver 主要新增 generator/result adapter/executor provider，不让 Worker 拼命令；
- 更换对象存储不需要修改项目、数据集、结果或前端；
- 任一模块失败不会暴露半初始化状态；
- 历史任务可以凭快照和版本化依赖重建同一 Solver Bundle，并凭 Bundle 与回执解释实际执行；
- 前端提交的是后端公开契约要求的规范输入，后端不承担简单 UI 换算；
- 模块只能调用其他模块公开接口，不能穿透目录和 ORM；
- 错误不会被兼容层、fallback 或空数据掩盖；
- 性能优化可以替换实现，但不能改变权威数据与公开语义。
