# 后端依赖架构最终收口：第二次验收纠偏

> 文档类型：临时实施指导；状态：执行中、待独立验收；基线：`b8e136e`；日期：2026-09-13。
>
> 本文件不是长期架构规范，不改变产品版本，也不替代正式手册。权威要求以
> [架构宪法](../../manual/developer-guide/zh-CN/ARCHITECTURE_CONSTITUTION.md)、各模块手册和
> Roadmap 为准。Muse 完成代码后必须保留本文件供 Codex 逐项复审；只有复审通过后，才由 Codex
> 删除本文件并确认更新日志的“最终完成”表述。

## 一、为什么需要第二次纠偏

`b8e136e` 已完成第一轮指南中的大量实质工作：无消费者 Repository Protocol 已删除，
application 的 ORM 穿透已消除，project 对 identity 的领域直依赖已上收，Worker 中旧 solver
selector、命令注册与不可达计算主体已删除，analysis 已停止构造 plan 和调用 engine，Docker 全量
测试为 1448 通过。

但独立验收发现，完成报告把“导入边消失”误当成“职责已经归位”。当前门禁主要扫描 import，无法发现
路由依次调用多个 application 函数组装业务、相同授权规则在 application 内复制、Worker 在本地解释
结果、或占位执行器伪造成功。因此更新日志中的“最终收口完成”和“全部问题关闭”仍早于事实。

本轮只修正剩余边界，不重做已正确部分，不提前实现 0.8。

## 二、必须保持的最终方向

```text
HTTP → API → 一个完整 application 用例
                    └→ 领域公开门面 / 各领域 persistence

任务提示 → Worker 常驻守护进程（长任务运行编排与状态机）
                 └→ application.worker 分阶段命令
                           ├→ 权威状态短事务
                           └→ 领域公开任务 handler / 计算执行协议
```

0.8 计算仍未实现，本轮只保证不会从旧路径偏航：

```text
Worker
    └→ application.worker 分阶段命令
         ├→ 领取/续租/进度/写入资格/提交的短事务
         └→ GeneratorProvider → Solver Bundle → SolverRuntime → ResultAdapter
                                                         └→ ComputeResult → analysis / metrics
```

API 的“一个动作转交一个完整 application 用例”不得套用到 Worker。一个 Worker attempt
本来就跨越多个短事务和执行阶段，数据库事务不得跨越长时运算。

“复用”必须发生在稳定公开契约、无状态公共基元或领域公开能力上；复制规则、测试锁相等、换目录的转发层、
把领域规则塞进 core、以及占位成功都不属于复用。

## 三、第二次验收发现（必须逐项关闭）

### A. API 仍在组织业务流程

以下只是把 `project.ensure_access` 换成 `application.projects.authorization.ensure_access`，路由依旧执行
“授权 + 一个或多个业务调用”，未做到一次转交完整用例：

- `api/config.py`：授权、加载工作图、校验、诊断判断、保存、元数据拼装；
- `api/validation.py`：授权、执行校验、保存/读取报告、现场回退执行；
- `api/model.py`：授权、设备/连接操作、二次查询和序列化；
- `api/config_revisions.py`、`api/datasets.py` 等仍直接调用 application 中的授权与其他用例；
- 上传流程仍可能由 API 先调用 quota 用例、再调用保存/导入用例。

门禁 13 只禁止 API 导入领域包，完全没有检测“API 对多个 application 能力扇出”。所以门禁绿色不能证明
API 是纯传输适配层。

整改要求：每个 HTTP 业务动作调用一个完整 application handler。handler 接收已认证主体、业务命令和
事务会话，完成授权、业务步骤与事务，返回与 HTTP 无关的结果；API 只做 DTO、调用和错误/响应映射。
不要创建只改函数名的转发层；新 handler 必须真正吸收原路由里的业务顺序。

### B. 组合授权仍有两套实现

权威实现已经存在于 `application/projects/authorization.py`，但
`application/tasks/submissions.py::ensure_project_access` 仍逐行复制 owner/admin/能力合并与错误语义，
并被 tasks、results、worker 路径继续复用。

整改要求：删除复制实现及导出，所有 application 用例统一调用一个授权能力。项目自身事实仍归 project，
身份角色仍归 identity，组合只在 application。不得再复制一份“专用于任务”的等价授权。

### C. Worker 仍解释结果并伪造成功

`worker/executors.py` 仍有两个职责违规：

1. `execute_check` 本地读取/解析证据 JSON、补齐四维字段、计算 `overall_score`、决定 outcome；
2. dataset/export/package-import 三个未实现 I/O 执行器返回 `status=placeholder`、
   `outcome=normal_completion`。

同时 `worker/runner.py` 与 `application/worker/lease_cases.py` 存在缺少明确 outcome 时默认
`normal_completion` 的路径，需要一起裁决，禁止未知结果被默认为成功。

整改要求：

- Worker 保持为长时后台运算的守护进程，拥有领取、租约、心跳、取消、超时、重试、资源隔离、任务阶段编排和结果上报；
- Worker 按运行时序调用多个 `application.worker` 阶段命令；由 application 调用注册的领域公开任务 handler、GeneratorProvider、SolverRuntime 和 ResultAdapter，继续保持宪法规定的 `worker → application → 领域模块` 依赖方向；
- `execute_check` 所需的证据结构解释、评估维度、评分和业务 outcome 由 results/analysis 公开能力唯一拥有；application.worker 阶段命令调用该能力，Worker 根据显式结果 contract 驱动任务状态，不在本地复制规则；
- 不得为了形式上“只调用一个用例”而新建包装整个长任务的 application handler，也不得让一个数据库事务持续到后台运算结束；
- 尚未实现的 I/O 任务不得产生成功回执：删除未实现分派入口，或使用语义正确的结构化 unavailable/failure；
- 若现有错误码没有适合 I/O 未实现的语义，先检查正式错误契约，再由 tasks 领域增加一个明确执行不可用错误并
  同步契约/测试；不得用 `TASK-SOLVE-001` 假装所有 I/O 都是求解错误；
- 完成路径必须要求显式、合法的业务 outcome，不得以缺字段默认成功。

独立验收又发现：把函数拆成“分阶段命令”后，执行线程仍从读取任务到最终提交
持有同一 SQLAlchemy Session，读取触发的事务可跨越整个长时运算；进度也只 flush 而没有独立提交。
`execute_check` 仍自己决定证据定位优先级、无证据时的业务 outcome/payload，并组装评估 DTO。
这些不是 Worker 的运行时序，而是 results/tasks 业务规则。

补正要求：

- 长时 attempt 不得持有跨阶段的 Session 或数据库事务；输入加载完即关闭读取会话，求解/分析/导出在无数据库事务状态下运行；
- 领取、进度、续租、评估写入和最终提交各自使用新的短会话/短事务；Worker 可持有会话工厂或注入的 application gateway，不持有长寿命 Session；
- 进度在短事务提交后对其他会话可见；失败或取消不得把之前未提交的评估/进度意外一并提交；
- report 证据定位和无证据结果由 application 编排 results/tasks 公开能力完成；Worker 只安排“定位→检查点→评估→上报”时序；
- 增加跨会话行为测试，证明进度中途可见、执行阶段无开启事务、失败不提交别的阶段副作用。

### D. 业务规则被错误放入 core

`core/patterns.py` 当前拥有 identity 的用户名/邮箱格式和 tasks 的幂等键格式。它们虽无状态，但具有明确
领域语义，不是“不知道具体业务也成立”的 core 共同语言。

整改要求：用户名/邮箱规则归 identity 的公开 contract/rules；幂等键规则归 tasks 的公开 contract/rules。
API 可引用不可变公开 contract 做输入 DTO，ORM/DDL 可引用相应领域常量，不得为了避免循环依赖把业务规则
上提 core。若 `core/patterns.py` 不再有真正通用内容则删除。

### E. results 规则尚未完全回归所有者

`application/results/writes.py::_validate_evidence_payload` 同时包含：

- 证据公开输出的字段、类型、rows/fields 结构规则；
- hourly object 引用存在性；
- 证据 snapshot 与任务 snapshot 的跨领域一致性。

此外 application 仍直接调用 metrics 的四维摘要，并通过 engines 的 `CAPACITY_PARAM` 解释结果容量。

整改要求：

- 证据/用户自定义输出的结构校验是必要边界校验，必须保留，但纯结构规则归 results 公开能力；
- object 引用和 task/snapshot 一致性由 application 组合 storage/tasks/results；
- 四维摘要经 results 或 analysis 的公开能力消费，application 不直接拥有/拼接单领域状态规则；
- application 不得依赖旧 engines 静态设备容量映射解释业务结果。结果采用规则必须基于稳定公开结果契约；
  若该映射只服务已删除的旧计算链，应删除并同步测试，不得仅从 engines 子模块提升到 facade；
- 不在内部交接重新校验 hash、内容摘要或已过边界的相同字段。

### F. 门禁只检查 import，没有检查职责回流

现有门禁没有发现 A、B、C。需要补充可维护的事实门禁或结构测试：

- API 业务端点不得先调用独立授权再调用业务函数，也不得对多个 application 用例扇出；
- 组合授权只能有一个生产实现；
- Worker 不得出现证据 JSON 解释、评估维度/评分规则或 placeholder success；
- 未实现执行器不得返回成功 outcome；
- core 不得拥有 identity/tasks 的业务规则。

门禁应验证真实禁止形态，不应依赖固定行号，也不能强迫无意义包装。常设豁免必须证明是稳定、无状态、由唯一
所有者公开的复用；相同豁免不要在多个测试文件复制成可能漂移的两份政策。

独立验收否决以下门禁形态：

- 按 `USERNAME_RE`/`ensure_access` 等私有符号名搜索并锁定精确所在模块；改名即失效，也无法检出换名复制的规则；
- 要求当前三个“未实现执行器”函数必须永久存在、有 `raise` 且无 `return`；这会阻止未来正式实现；
- 只扫描已删除 `services` 导入的 API fanout 门禁；它无法检出同一端点对多个 application 能力的调用。

语义行为用行为测试证明；静态门禁只保留稳定的依赖方向和通用禁止形态。API 门禁可按函数调用图
检查“一个 API 动作至多转交一个 application handler”，不得锁定具体端点、函数名或当前未实现状态。

### G. 现行文档和更新日志与事实不一致

- `manual/developer-guide/zh-CN/modules/application.md` 仍把 `services/` 写作迁移边界，并指导从 service
  开始阅读；
- 若干 production docstring/persistence 说明仍声称实现某个已删除 Repository Protocol，或写着以后再迁移；
- 测试中有大量 `matches_old`、`legacy_service` 和已删除路径说明。仅“确保旧模块不可导入”的回归测试可以
  保留历史路径，其余当前职责说明必须改为现行所有者；
- changelog 顶部“最终收口完成”“API 只调用完整 application 用例”“10 项全部关闭”不符合当前事实。

整改要求：先撤回不真实的完成表述。Muse 本轮完成后只提交准确的实施事实，不得自行再次宣布“最终验收通过”；
最终完成结论由 Codex 独立复审后写入。稳定手册只描述职责、公开边界和结果，不写本轮文件名、行号、agent、
波次或迁移过程。

依赖计数和当前豁免是审查证据，不是长期文档。不得把无可重现生成器的“后端依赖/职责清单”提交到
`docs/development/`；依赖事实在完成报告中给出即可，稳定边界只由 `manual/` 正文表达。

### H. 第二次独立验收的其他残留

- `api/projects.py` 的管理列表、归档/撤销归档、导入确认，`api/results.py` 的评估列表/检查任务，
  `api/tasks.py` 的提交/取消/重试，以及 `api/health.py` 仍在单个 API 动作中多次调用 application 能力；
- `LEGACY_SERVICE_CALLS = ()` 及其空集合测试只是已删历史的实现哨兵，应删除，不作为永久公开面；
- `TASK_DATA_HASH_MISMATCH`/`TASK-DATA-002` 和对应前端文案仍假定快照 hash 复核，与宪法 §2.6 冲突，应连同无生产者的诊断分支删除；
- `docs/development/backend-decoupling-duty-inventory.md` 是会立即过期的第二份架构清单，应删除；
- `capacity_params` 无任何生产或前端消费者，且属旧 engines 设备参数映射，本轮删除可接受，不得恢复。

## 四、实施波次

使用“波次内并行、波次间串行集成”。各子 agent 必须使用独立工作树和独立分支；协调者逐项 review 后合并，
禁止共享工作树并发编辑。

### Wave 0：真实基线与验收测试（串行）

- 逐项确认 A～G 的所有生产调用者和行为测试；
- 设计能抓到职责回流的门禁，但不先在集成分支提交故意失败状态；
- 记录现有 HTTP 行为、权限、事务和错误语义，重构不得无意改变它们。

### Wave 1：API 完整用例与单一授权（可按业务域并行）

- 切片 1：config/config-revisions；
- 切片 2：model/validation；
- 切片 3：datasets/projects/package 上传与 quota；
- 切片 4：删除重复 `ensure_project_access`，统一 application 授权入口，并迁移 results/tasks/worker 消费者。

集成后逐个端点确认：路由内没有授权+业务调用链，一个动作只转交一个完整用例。

### Wave 2：Worker 职责与失败语义（串行集成，可分实现/测试两切片）

- 保持 Worker 对 report/check 长任务的运行编排，把本地评估、评分和证据业务解释提升为 results/analysis 公开能力，并由 application.worker 阶段命令调用；
- Worker 只传递不可变输入、调用该阶段命令并消费显式结果 contract，不复制其业务规则；
- 领取、续租、进度和提交继续使用 application.worker 短事务命令，不建立包住整个 report/check 的长事务用例；
- 删除 I/O placeholder success 和默认成功路径；
- 保持计算类 0.8 未实现的结构化失败，不实现 Generator/Solver。

### Wave 3：领域所有权收尾（可并行）

- identity/tasks 业务 pattern 从 core 回归领域；
- results 证据结构规则与摘要回归公开领域能力，application 只保留跨域一致性和对象协调；
- 删除 application 对旧 engine 容量映射的依赖；
- 审计同类“复制自 services”“以后迁移”代码，不按文件大小机械重构。

### Wave 4：门禁、文档与集成验收（串行）

- 补齐 F 的门禁并用构造样例证明能检出违规；
- 清理 G 的过期文档和测试说明；
- 重新生成真实依赖/职责清单；
- 在最终集成 HEAD 运行一次 Docker 全量测试并清理本轮非基础镜像、孤儿容器；
- 保留本指南，提交所有修改，等待 Codex 独立验收。

## 五、明确排除

- 不修改架构宪法；发现规范冲突立即停止并报告；
- 不实现 0.8 GeneratorProvider、Bundle、SolverRuntime、ResultAdapter 或真实 solver；
- 不恢复旧计算链、旧 services、1.0 兼容、fallback 或静默默认；
- 不增加内部 hash、重复校验、冗余诊断、假想安全加固或备用分支；
- 不为通过门禁建立只转发、无生产消费者的抽象；
- 不顺便修改数据库结构、前端或无关业务功能。

## 六、测试与提交

- 所有编译、格式化和测试只在 Docker；主机只做源码/依赖静态检查和 Git 操作；
- 每个切片运行最小相关测试与架构门禁，每波集成运行相关测试，最终 HEAD 只运行一次全量测试；
- 测试公共行为、权限、事务和错误语义，不测试复制常量、文件行号、空转包装或私有布局；
- 每个独立切片 review 后单独提交，协调者按依赖顺序合并；
- Docker 测试后删除本轮生成的非基础镜像和孤儿容器，不删除基础镜像与项目正常基础设施；
- 不重写或压缩用户既有无关提交，不强推，不 push。

## 七、Muse 完成报告要求

Muse 只能报告“实现完成，等待独立验收”，不得报告“最终验收通过”或“残余为零”。报告必须包含：

1. A～G 每项的代码落点和行为证据；
2. 每个 API 端点如何收敛为一次完整用例调用；
3. 唯一授权实现及所有旧调用者迁移结果；
4. Worker 删除的解释/评分/placeholder success 与新的失败语义；
5. identity/tasks/results 规则最终所有者；
6. 新门禁如何主动检出构造的违规，而不只是当前树为零；
7. 各切片和集成提交；
8. Docker 测试命令、结果、镜像/容器清理结果；
9. 仍需 Codex 裁决的任何事项。

## 八、独立验收完成定义

只有 Codex 复审确认以下全部成立，才算真正完成：

1. API 每个业务动作只调用一个完整 application 用例，不组织授权、校验、配额与保存顺序；
2. owner/admin 项目授权只有一个生产实现；
3. Worker 是长时后台运算的守护进程并拥有运行编排；它不复制证据解释、评估计算或业务 outcome 规则，不穿透 ORM/persistence，未实现功能不会成功；
4. identity/tasks/results 规则由各自领域公开能力唯一拥有，core 无领域业务规则；
5. application 只保留跨域一致性、事务与步骤顺序，不依赖旧 engine 静态映射；
6. 门禁能检出 import 穿透和本地职责复制，白名单/豁免准确且不形成第二份政策；
7. 生效手册、代码注释、测试说明和 changelog 与真实现状一致；
8. Docker 相关测试和最终全量测试通过，工作树干净，非基础测试资源已清理；
9. 未修改宪法、未提前实现 0.8、未恢复兼容或新增多余校验。
