# 架构解耦重构临时工作指导

> 状态：临时工作文件；`fe3d83b` 审查未通过，当前用于 `0.8.0` 开发前的解耦纠偏。
>
> 本文件不是宪法、开发者指南或长期契约；与现行宪法、公开契约冲突时无效。
> 重构完成并通过最终审阅后删除，不作为后续功能设计依据。

## `fe3d83b` 审查裁决与纠偏目标

`fe3d83b` 完成了 API/Worker 表面入口迁移、analysis 执行解耦和 assembly 私有穿透清理，但不是本文定义的最终解耦结果。此前的“Wave 1–5 已完成”和 100% goal 状态已被本次审查否定；后续不得以它们作为停止条件。

审查确认的实际偏离如下：

1. 至少 16 个 application 文件仍导入 `iesplan.services`，多个所谓用例只是直调旧 service 的薄包装。
2. application 通过 `sa.table("...")` 重新声明表名和列名，以避开 ORM import 门禁；这仍是穿透 persistence，而且制造了第二份数据库结构。
3. Worker 仍有 commit/rollback，任务尝试的事务所有权没有完整上收到 application.worker。
4. 旧 `services` 仍约万行，部分业务又被复制进 application，形成两套实现和双重修改点。
5. 现有门禁只检查 import 形态，无法发现 application 裸 SQL/表定义、Worker 事务和 application→services；且 API/cross-model 白名单并未实际清空。
6. `test_full_business_chain` 中，旧计算链的 `NotImplementedError` 被回滚后的租约丢失错误地报告为 `lease_rejected`，真实失败原因被遮蔽。
7. storage 仍反向调用 audit，与本次已确定的“业务审计由 application 编排”目标不一致。
8. 收尾记录声称本文已删除，但文件仍存在；roadmap/changelog 也没有基于真实完成事实更新。

本轮的最终结果不是继续加 wrapper，而是把已有业务实现收敛到唯一拥有者：

```text
API/Worker → application 用例 → 领域公开门面 → 该领域 persistence
```

完成时必须同时满足：

- application 对 `iesplan.services` 的生产代码导入为零；
- application 不声明或按名访问业务表，不导入其他领域的 persistence/repository 内部实现；
- Worker 不调用 commit/rollback，完整任务尝试由 application.worker 用例拥有事务；
- 每项领域规则、常量、映射、表定义和序列化只有一个权威实现；
- 旧 `services` 的全部生产调用方已迁移，无调用文件立即删除，不保留转发、别名或“只读兼容”；
- 计算尚未实现时，必须显式返回真实的结构化失败，不得被租约或内部异常遮蔽；
- Docker 全量测试零失败，不使用 skip/xfail/白名单隐藏本轮未完成项。

## `fe3d83b` 后的纠偏波次

以 `fe3d83b` 为审查基线，只补完未完成部分，不回退已正确的 API、analysis 和 assembly 收敛。仍采用“波次内最多 3 个独立 worktree 并行、波次间串行集成”，但必须先画出真实 DAG 和文件归属，不得为凑并行度拆散同一事务。

### Correction Wave 0：补齐可信门禁

由协调者串行完成并独立提交：

1. 新增 application 禁止导入 `iesplan.services`、`iesplan.models`、领域内部 `persistence/repository/loader` 的门禁。
2. 新增 application 禁止 `sa.table/Table/text`、裸表名列名和直接 SQL DML 的门禁；领域 persistence 实现不受该禁止。
3. 新增 Worker 禁止 commit/rollback 以及 services/models/裸 SQL 的门禁。
4. 所有临时迁移集合都必须与实际检测结果精确相等：既不漏报，也不允许过期项留在集合里冒充“已清空”。
5. 门禁可以在纠偏过程中使用命名明确的临时债务基线，但必须列出每项真实违规和责任切片；最终验收时全部为空。

本波次的目的是让门禁如实显示当前债务，而不是通过放宽规则继续保持绿色。

### Correction Wave 1：领域垂直收敛

协调者在启动子任务前必须用 `rg` 建立 service 符号→真实调用方清单，然后按互不重叠文件集动态分组。每个纵向切片必须在同一分支内完成“唯一领域实现 → application 调用方迁移 → 旧 service 符号删除”闭环，不得提交临时 service 转发层或两份实现。建议队列：

- **C1-Task/Result/Queue**：由 tasks/results 领域 persistence 接管租约、槽、attempt、result index、sample task 及队列公开能力；删除 application.worker 中的裸表。
- **C1-Project/Model**：将 project/model/template/draft/project-model 的规则和 persistence 收敛到各自领域公开面；去掉 application 对 `services.project` 的依赖和重复实现。
- **C1-Configuration/Dataset**：将 config/config-revision/dataset 的权威规则、序列化和 persistence 归回 configuration/dataset；不在 application 保留从 service 复制的映射和校验表。
- **C1-Identity/Audit/Package/Storage**：将 external-auth、audit、package 和 storage 相关实现收敛到对应领域。普通业务审计由 application 在用例成功路径编排，storage 不导入 audit。

并行时不得让两个子任务同时修改 application 公共入口、架构门禁、启动组装或同一 service。若两个领域存在强事务耦合，合并为一个纵向切片，不通过中间兼容包装强行并行。

### Correction Wave 2：跨领域 application 编排收敛

只在 Wave 1 的相关垂直切片已合并后，处理因跨领域事务而不能在单领域切片中完成的用例。可按下列互不重叠的用例族并行：

- projects/models/configuration/datasets/validations；
- tasks/results/packages/audits；
- identity/health/objects/application.worker。

每个 application 函数只负责一个完整用例：输入 DTO、权限、跨领域调用顺序、事务和输出 DTO。它不拥有领域映射、表结构、序列化真相或持久化查询。

禁止以下做法：

- 新增“薄包装”仅转调 service；
- 把 service 整段复制进 application 后保留两份；
- 在 application 内复制正则、设备/载体映射、状态机、单位表、错误类或数据库列；
- 用延迟导入、别名重导出或捕获异常回退到旧 service。

每完成一个用例族，必须在同一切片中删除已无调用的 service 符号或整个 service 文件，不留到“以后再清”。

### Correction Wave 3：Worker 事务与可见失败

本波次由一个写代理串行完成，避免 lease/runner/application.worker 的同一事务链被拆散：

1. `worker` 只调用 application.worker 公开用例，不接收或操作领域记录，不 commit/rollback。
2. application.worker 通过 tasks/results/dataset/project/storage 公开门面执行快照读取、租约和结果提交，不声明裸表。
3. 任务领取必须在后续执行回滚之前形成正确可见的租约/尝试状态；执行未实现或异常时，失败收拢不得被误判为 lease rejected。
4. 不实现 0.8 计算业务。将旧全链测试拆为“任务提交/快照/预检正常”和“当前计算入口显式不可用且错误码正确”两类断言，删除对已不存在的 1.0 真实求解的期待。
5. 使用现有结构化错误语义；只有在公开契约确实无合适错误码时，才增加一个最小、专用的错误码。

### Correction Wave 4：全局删除与真实收尾

本波次串行执行：

1. 证明生产代码对 `iesplan.services` 零导入，删除剩余 services 文件、重导出、复制常量和过期测试缝线。
2. 将 API 的 `models.common` 常量移到真正的 core/领域公开所有者，删除 API ORM 豁免。
3. 所有迁移债务集合必须为空；仅能保留门禁语义上明确的同领域 persistence 归属声明，不能把它命名为白名单。
4. storage 删除 audit 导入和 `_audit` 编排；需要审计的公开业务用例在 application 成功路径记录。
5. 修正或取消 `fe3d83b` 的错误“已完成”收尾记录；在真正验收前不更新 roadmap/changelog 为完成。
6. Docker 中运行架构门禁、相关切片测试和一次全量测试，要求零失败；清理本轮非基础镜像。
7. 最终 review 通过后，根据真实事实更新 roadmap/changelog，删除本临时文件，提交收尾并确认工作树干净。

### 纠偏期间的协作和提交规则

- Muse 主会话仍是唯一集成协调者；子代理不修改中央门禁、application `__init__`、启动组装、migration、roadmap/changelog、本文或收尾记录。
- 每个子任务从当前纠偏波次的同一已集成 HEAD 创建独立 worktree/branch，只修改已分配文件，自行 review 和 Docker 聚焦测试后提交。
- 协调者不盲目合并：每个提交先检查依赖方向、责任归属、重复实现和事务语义，再顺序合并；波次合并后重跑门禁。
- 一个切片不得通过增加 wrapper、复制实现、放宽门禁、无当前契约依据地改弱测试期望，或保留旧调用方来自证完成。
- 每个提交报告必须列出：被删除的 service 符号/裸表/跨层事务，新的唯一所有者，仍存在的调用方，Docker 验证，提交 hash。

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
