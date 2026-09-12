# 架构解耦重构临时工作指导

> 状态：临时工作文件，仅适用于 `0.8.0` 开发开始前的解耦重构窗口。
>
> 本文件不是宪法、开发者指南或长期契约；与现行宪法、公开契约冲突时无效。
> 重构完成并通过最终审阅后删除，不作为后续功能设计依据。

## 目标

把当前后端收敛为以下依赖方向，同时不实现 `0.8.0` 的新计算业务：

```text
API → application 用例 → 领域模块公开接口 → persistence/storage
Worker → application.worker → Generator/Runtime/ResultAdapter 边界
ComputeResult → analysis
devices 2.0 → modeling 2.0 → GeneratorProvider
```

## 为什么要做这次重构

当前问题不是单个函数错误，而是调用方向和职责边界混在一起：

- API 直接读取 ORM，导致 HTTP 形状、事务和数据库结构互相绑定；
- `services` 同时承担领域规则、跨模块编排、权限、事务和持久化，形成互相调用的“大混包”；
- Worker 直接读取多个领域的 ORM 和 service，任务执行边界无法独立替换；
- analysis 直接驱动旧计算引擎，结果分析和计算执行无法分别演进；
- assembly 的规则依赖 checker 内部实现，1.0 公共装配契约和 2.0 接口契约难以并行收敛；
- storage 夹带审计和业务持久化细节，替换对象存储实现会牵动业务代码。

如果不先整理这些方向，后续每新增一个功能都会把依赖带入更多模块，最终只能通过大范围重写纠偏。此次重构的目的不是增加抽象层数量，而是把已有职责分配给唯一拥有者，令后续 `0.8.0` 只需实现新计算边界，不再穿透旧代码。

## 最终要达到的效果

重构完成后，应能从依赖图和代码检查中观察到以下结果：

1. API 文件不导入 ORM、不提交事务、不调用多个领域 service 组织业务流程；一个端点只负责 DTO、认证/授权和调用一个 application 用例。
2. application 用例是跨模块流程和事务的唯一所有者；领域模块只维护自己的规则、公开 contract 和本领域 persistence，不反向调用 application。
3. 每张业务表有明确领域归属；跨领域读取通过公开 repository/门面完成，不复制 ORM 查询和字段映射。
4. Worker 只负责租约、取消、分派和结果提交；它不解释项目、设备、财务或装配语义。
5. 计算执行与分析完全分离：计算链未来产出 `ComputeResult`，analysis 只消费结果和声明输出，不重新构造 plan 或启动引擎。
6. assembly 的 parser、context、rules、validator 依赖公开 contract 和只读上下文；规则不反向依赖 checker 内部实现。
7. storage 只拥有对象和引用生命周期；审计由 audit/application 负责，二者可以独立替换。
8. 旧 1.0 运行原型、兼容旁路和无调用包装全部消失；未来 0.8 不会再出现“新旧双轨”。

最终效果不是“所有模块互不依赖”，而是依赖方向稳定、每条依赖只有一个业务原因：

```text
传输适配只依赖用例；用例依赖公开领域能力；
领域依赖自己的持久化；Worker 依赖执行边界；分析依赖结果。
```

## 防止长程任务跑偏的裁决规则

遇到新增文件、临时兼容或重构争议时，按以下问题裁决：

1. 这个依赖是为了完成当前业务规则，还是为了复用实现细节？后者删除或移入拥有者模块。
2. 该调用是否跨越公开 contract？如果是，先补最小公开 contract，不得直接导入 ORM、私有函数或内部 registry。
3. 该逻辑是否同时包含传输、编排、领域规则和持久化？如果是，拆回各自唯一拥有者。
4. 新增抽象是否有真实消费者和明确替换点？没有就不新增。
5. 迁移是否只是换名字而保留同一条旧调用链？如果是，不算完成，必须改变依赖方向。
6. 删除旧入口后是否还有调用方？有则先迁移调用方；无则立即删除，不保留“以后可能用”的兼容层。
7. 是否为了假想风险新增摘要、完整性复核、备用分支或重复诊断？如果不是用户输入、公开自定义输出或核心业务规则，删除。

每个切片的提交说明必须回答：删除了哪条旧依赖、谁接管了它、调用方向如何变化、哪个门禁或测试证明目标效果已经出现。

## 实施原则

1. 不修改宪法和长期开发者指南；只按本文件安排重构顺序。
2. 每次只迁移一个领域或一条完整调用链，完成后独立 review、Docker 测试和提交。
3. 新边界先提供公开 contract/Protocol，再迁移调用方；旧入口无调用后立即删除。
4. application 是跨模块编排和事务所有者；领域模块不反向调用 application。
5. API 不导入 ORM；Worker 不直接编排领域服务；analysis 不直接驱动计算引擎。
6. 不增加哈希、完整性复核、内部文件交换校验或假想安全分支。
7. 不把 `0.8.0` GeneratorProvider、Solver Bundle、SolverRuntime 的业务实现混入本次重构。
8. 允许重构窗口内计算任务暂不可用，但不得留下静默 fallback 或新旧双轨。

## 切片顺序

### 1. 盘点与门禁

- 建立模块、表、公开 contract、调用方和事务所有者清单。
- 增加或修正架构门禁：API 禁止 ORM、禁止私有导入，领域跨模块只走公开接口。
- 本切片不改变业务行为。

### 2. 公开 contract 与 repository Protocol

- 为 project、identity、dataset、config、task、result、package 建立最小公开 DTO/Protocol。
- repository 只负责查询、写入、flush/savepoint 和领域错误，不负责提交事务。
- 不暴露 ORM、内部路径、loader 或 registry。

### 3. persistence 按领域收敛

按 `project → identity → dataset → config → task → result → package` 顺序迁移 ORM 查询。

- 每个领域只访问自己归属的表。
- 删除 service 之间为复用查询而产生的直接 ORM 穿透。
- 保持现有公开行为，禁止顺手添加新业务。

### 4. application 用例迁移

按项目、模型、配置、任务、结果、项目包顺序迁移跨模块流程：

- 明确输入 DTO、权限检查、事务边界、领域调用和输出 DTO；
- 事务只由 application 提交/回滚；
- 导入导出、版本创建、任务创建等流程不再由单个 service 统包。

### 5. API 迁移

逐个资源域把 API 改为 `route → application use case`：

`projects → datasets → config → models/templates → tasks → results → packages → admin/health/objects`。

迁移后删除 API 的 ORM 导入、跨 service 编排和领域计算。

### 6. Worker 边界迁移

- `worker` 只保留领取任务、租约、取消、分派和结果提交基础能力；
- 业务快照读取和跨模块编排移入 application.worker；
- 旧计算原型已经删除，计算执行入口保持显式未实现，等待 `0.8.0` 新计算链；
- 不恢复进程内命令注册表或 Python 函数分发。

### 7. Assembly 内部层次

- `parser → context → rules → validator → artifact`；
- rules 只依赖公开 assembly contract 和只读上下文；
- 删除 rules 对 checker 内部实现的反向依赖；
- 保留已完成的 `ies.assembly 1.0.0` 公共契约，不能按版本号误删。

### 8. Analysis 与计算边界

- analysis 只消费 `ComputeResult`、回执和声明输出；
- 不再直接构造旧 plan 或调用旧 engine；
- 计算生成、执行和结果适配接口只预留边界，不在本窗口实现 0.8 业务。

### 9. Storage 与 audit

- storage 只负责对象、引用和 Blob 生命周期；
- 审计通过公开 audit 接口或 application 记录；
- 不让 storage 依赖具体业务审计 ORM。

### 10. 清理与收尾

- 删除无调用的旧 service、兼容导出、旧测试和过期文档；
- 全量检查 import 图、API ORM、私有符号和循环依赖；
- Docker 中执行与切片相称的测试；
- 删除本临时文件，并在 roadmap/log 中记录重构完成事实。

## 切片 9 后的并行执行方案

下列波次以切片 9 的提交 `43cedbc` 为起点。执行原则是：按依赖关系分波次，波次内并行，波次间串行集成。不得为了并行度让两个写代理修改同一组文件，也不得越过前置波次提前迁移调用方。

### 协调方式

1. Muse 主会话是唯一集成协调者，负责波次拆分、文件归属、review、顺序合并、中央门禁和最终验收。
2. 每波次最多同时运行 3 个写代理。代理必须从该波次同一个已集成 HEAD 创建独立 Git worktree 和 branch，只修改事先分配的文件，review 通过后提交并回报 hash。
3. 某个代理完成后可以立即从本波次队列补入下一个无冲突任务；但未通过本波次集成检查前，不得启动依赖它的下一波次。
4. 协调者逐个审阅提交，按依赖顺序合并；每波次完成后建立干净集成基线，再让下一波次的 worktree 从新 HEAD 创建。
5. 子任务只在 Docker 中运行聚焦测试；协调者在每波次合并后运行架构门禁及相关集成测试，最后收尾时只运行一次全量测试。测试后删除本轮生成的非基础镜像。

下列高冲突文件只由协调者修改，子代理发现需求时只在报告中列出：

- `backend/tests/test_architecture_gates.py`；
- `backend/iesplan/application/__init__.py` 和应用启动/组装入口；
- `backend/iesplan/migrations/__init__.py`；
- 宲法、roadmap、changelog 和本临时文件。

分配任务前，协调者必须先记录每个子任务的文件集、公开边界和预期删除的旧依赖。发现文件归属重叠时必须重新拆分，不允许依靠事后解决大量冲突。

### Wave 1：底层边界收敛

本波次先清理不依赖新 application 用例的底层边界，为后续调用方迁移提供稳定公开面。最多 3 个任务同时运行，其余任务候补。

- **W1-Package**：收敛 package 的 persistence 和跨领域读取，去掉 `services.package` 对非归属 ORM 的直访；跨领域数据经已有 project/dataset/configuration/results/audit 公开接口获取。
- **W1-Model**：收敛 model template、draft 和 project model 的领域 persistence，消除 application 直接 ORM 访问；不改 API。
- **W1-Storage**：将 `RetentionRule` 归入明确的 storage 领域持久化边界，删除 storage 对 audit ORM 的依赖；对象操作的审计由 application/audit 编排，storage 不反向依赖 audit。
- **W1-Assembly**：收敛为 `parser → context → rules → validator → artifact`，把真实共享能力提升为公开接口，删除 rules/checker/schema 之间的跨模块私有符号导入。
- **W1-Analysis**：analysis 只消费 `ComputeResult`、回执和声明输出，删除对 engines、services 和 `assembly.plan` 的依赖；不实现 0.8 计算入口，无可用新计算结果时保持显式未实现。

Wave 1 集成时，协调者统一更新架构门禁和公开导出，确认上述旧依赖已从白名单删除。

### Wave 2：application 用例

只在 Wave 1 集成后启动。将跨模块流程和事务收敛到 application，此波次不修改 API 或 Worker。可并行三组文件互不重叠的用例：

- **W2-A**：projects、datasets、configuration 和 validation；
- **W2-B**：identity/auth、model/templates 和 project-model；
- **W2-C**：tasks、results、package/export。

每个用例必须有明确输入/输出 DTO，只经领域公开门面访问数据，并由 application 拥有 commit/rollback。不允许应用用例导入 ORM、领域内部 repository 实现或另一个 application 子模块的私有符号。公共导出由协调者在集成时一次处理。

### Wave 3：API 迁移

只在 Wave 2 集成后启动。按路由文件互不重叠分为三组：

- **W3-A**：projects、datasets、config/config_revisions、model/model_templates/project_models；
- **W3-B**：auth、admin、health、limits 和 objects；
- **W3-C**：tasks、results、validation 和 exports/packages。

每个端点只保留 HTTP DTO、认证/授权和错误映射，只调用一个 application 用例。删除 API 中的 ORM 导入、commit/rollback 和多 service 编排。必须保持现有 HTTP 公开契约，不借机重新设计接口。

### Wave 4：Worker 与执行边界

Worker 生命周期文件共享状态和调用链，本波次不拆给多个写代理，由一个代理完成：

- 建立 `application.worker` 用例，接管业务快照读取、跨领域编排和事务；
- Worker 只保留租约、取消、分派和结果提交，不依赖 services 或领域 ORM；
- 0.8 计算入口继续显式未实现，不恢复已删除的 1.0 运行链，不新增 fallback。

本波次集成后必须清空 Worker 直连 services 和跨领域 ORM 的门禁白名单。

### Wave 5：串行清理与最终验收

本波次不并行删除共享旧入口，避免调用方和定义方同时消失：

1. 用 `rg` 证明旧 services、兼容导出、1.0 运行原型和无调用包装已无消费者，再删除定义。
2. 清空架构门禁中的迁移白名单和 TODO；真实的同领域持久化导入应由门禁明确建模，不得伪装成违规或以新白名单掩盖。
3. 检查实际 import 图和循环依赖，按本文最终效果逐项验收。
4. 在 Docker 中运行一次全量测试，区分本次回归与有证据的既有失败，不得用白名单、跳过或放宽断言隐藏回归。
5. 删除本临时指导文件，再根据已完成事实更新 roadmap/log，review 后提交最终收尾。

### 最终验收矩阵

- API 不导入 `iesplan.models`/数据库会话提交能力，不跨多个 service 编排。
- application 不导入 ORM 或领域内部 persistence，并是跨领域事务的唯一所有者。
- Worker 不导入 services 或业务 ORM，并存在明确的 `application.worker` 边界。
- analysis 不导入 engines、services 或 `assembly.plan`，只消费结果契约。
- assembly 规则不跨模块导入私有符号，内部依赖方向无反转。
- storage 不导入 audit ORM，不负责业务审计编排。
- 无调用的 services、兼容旁路和旧计算原型已删除，不用别名或 fallback 保留新旧双轨。
- 全部架构门禁无迁移白名单，聚焦测试和最终全量 Docker 测试通过。
- 本次工作未实现 0.8 计算业务，未增加非必要校验、hash、完整性复核或防御分支。

## 每个切片的完成条件

- 依赖方向符合目标图；
- 没有新增兼容旁路；
- 相关 Docker 测试通过；
- `git diff --check` 通过；
- review 后独立提交；
- 旧入口确认无调用后才删除。

## 阶段之间的因果关系

| 阶段 | 为什么先做 | 做完后获得什么 |
| --- | --- | --- |
| 盘点与门禁 | 没有依赖基线和自动门禁，迁移时会重新引入旧边界 | 能持续发现 API/领域/Worker 的越界依赖 |
| contract 与 repository Protocol | 没有公开接口，调用方只能继续依赖 ORM 或 service 实现 | 有稳定的替换面，后续迁移不搬运内部细节 |
| persistence 收敛 | ORM 查询散落在多个层，事务和表归属无法判断 | 每张表有唯一拥有者，repository 可独立测试 |
| application 用例 | 跨模块流程若不集中，services 会继续膨胀和互相调用 | 每个业务流程有明确事务所有者和依赖清单 |
| API 迁移 | API 仍直接编排时，application 边界不会真正生效 | HTTP 层可独立变化，业务逻辑不再绑定路由 |
| Worker 边界 | Worker 直接读业务数据会把执行基础设施锁死在领域实现上 | 任务执行可替换，未来只接入生成/运行/结果边界 |
| assembly 层次 | 规则反向依赖 checker 会阻碍 1.0/2.0 契约并行演进 | 装配校验按公开 contract 分层，内部依赖无环 |
| analysis/计算边界 | 分析直接驱动计算会重新制造旧计算耦合 | analysis 可独立消费 `ComputeResult`，0.8 计算链可独立实现 |
| storage/audit | 对象生命周期和审计绑定会扩大存储替换面 | storage 与 audit 可分别替换，业务审计归 application |
| 清理收尾 | 旧入口和文档残留会诱导后续开发继续使用旧路径 | 依赖图、代码、测试和指导文件一致，重构窗口可以关闭 |

如果某一阶段无法达到右栏效果，只完成文件搬移或接口改名，不得标记为完成；应停留在该阶段继续收敛。

## 明确不在本窗口处理

- `0.8.0` 设备目录新功能；
- GeneratorProvider 的具体 generator；
- Solver Bundle schema 的业务实现；
- SolverRuntime、ResultAdapter 的端到端执行；
- 前端视觉和拖放行为；
- 与本次依赖迁移无关的业务重写。
