# 后端依赖架构最终收口：第三次验收与结构归位

> 文档类型：临时实施指导；状态：执行中、待 Codex 独立验收；基线：`16c8911`；日期：2026-09-14。
>
> 本文件只服务本次长程重构，不是长期架构规范。权威要求依次来自
> `manual/developer-guide/zh-CN/ARCHITECTURE_CONSTITUTION.md`、生效模块手册和 Roadmap。
> 实现完成后必须保留本文件供 Codex 逐项复审；只有全局验收通过后才删除并更新 changelog。

## 一、为什么上一轮仍不能称为完成

上一轮确实删除了 `services/`，消除了 API/Worker/application 的主要 ORM 穿透，收回了事务，统一了
项目授权并修正了 Worker 的结果解释与占位成功。但验收把“违规 import 归零、当轮清单关闭、测试全绿”
错误地当成了“最终职责和所有权已归位”。因此出现了以下仍未解决的结构事实：

- Worker 仍解析和合并数据、映射引擎字段、补默认数组、完成 SI 换算和时间轴构造；
- 没有 `bootstrap/` 组合根，API readiness 的注册状态没有生产初始化路径；
- ORM 仍集中在 `models/`，`models/calc.py`、`models/audit.py` 等混放多个领域所有者的表；
- application 用例族之间继续导入彼此的实现文件；
- `worker/lease.py` 等层次仍是无业务增量的转发；
- 旧 `engines/` 已无生产计算入口，却因直接单测仍被保留；
- 旧数据库兼容、旧快照字段和临时实施文档仍有残留。

本轮不再以问题清单、测试数量或文件移动为完成依据，只以最终调用图和事实所有权验收。

## 二、本轮必须得到的结果

本轮完成的是“0.8 功能开发前的依赖架构基础”。允许定义 0.8 所需的稳定计算边界 contract，但不实现真实
generator、solver 算法或计算功能；没有 provider 时必须明确不可用，不得恢复旧引擎或制造假成功。

```text
API 进程入口 ─┐
               ├→ bootstrap 组合根 → ApplicationContext / readiness
Worker 入口 ───┘

HTTP → API → 一个完整 application 用例
                    └→ 领域公开门面 → 本领域 persistence/ORM

任务提示 → Worker daemon（领取、租约、心跳、取消、超时、阶段编排）
                 └→ application.worker 阶段命令
                        └→ computation 公开协议
                              GeneratorProvider
                                → SolverBundle
                                → SolverRuntime
                                → ResultAdapter
                                → ComputeResult

devices 2.0 → modeling 2.0 → assembly 当前契约 → computation
ComputeResult → analysis/results/metrics
```

最终生产代码必须满足：

1. `api/worker → application → 领域公开门面 → 本领域 persistence → core`；
2. application 同族内部可以拆文件，跨用例族不得穿透实现文件；
3. 每张 ORM 表只有一个领域所有者，顶层集中式 `models/` 不再作为混合表仓库；
4. 只有 bootstrap 选择并装配数据库、storage、devices 和 computation provider；
5. Worker 不解释装配、数据字段、业务单位和结果，不拥有 generator 职责；
6. 没有生产消费者的旧 engine、兼容字段、兼容迁移和测试专用抽象全部删除；
7. 复用只发生在稳定 contract、领域公开纯能力和无状态公共基元上。

## 三、版本身份裁决：不得看到 `1.0` 就删除

版本号属于各自独立契约，必须先识别身份。

当前有效，必须保留并按公开行为测试：

- `ies.device-model 2.0.0`：当前设备模型契约；
- `ies.assembly 1.0.0`：当前 0.7 装配契约，不是旧设备模型 1.0；
- finance YAML `1.0.0`：当前财务契约；
- 项目包 manifest `1.0`：当前项目包契约；
- device-model instantiator `1.0.0`：若仍是现行公开 instantiator 版本则保留。

应删除：

- 已由 device-model 2.0 替代的旧设备命令、旧 `DeviceSpec`、旧注册/机理调用路径；
- 仅为未发布数据库升级准备的旧列、旧表、回填、ALTER/DROP 兼容流程与 `legacy.db` 测试；
- `CalcSnapshot` 中“旧 `assembly_text` + 新 `canonical_assembly_text`”双字段兼容；当前 schema 只保留一个
  含义明确的规范装配字段和必要回执；
- 仅被测试直接调用、没有生产消费者的旧 `iesplan.engines.*` 计算原型；
- 仅锁定 `parser10`/`builder10` 等实现文件名的装配测试。装配 1.0 公共 contract 保留，但实现收敛成一条
  parser/builder/validator 管线，不保留双实现或版本迁移包装。

## 四、测试裁决：测试不得反向决定保留旧实现

效力顺序固定为：宪法 → 生效公开契约/模块手册 → 本轮目标结构 → 当前代码 → 当前测试。
测试与目标冲突时修改或删除测试，不为测试恢复旧代码、兼容别名、转发层或占位实现。

### 4.1 直接删除或迁移的旧实现测试

以下测试直接把旧 `iesplan.engines.*` 当成现行公共实现；若生产调用图确认没有消费者，应随旧 engine 删除，
不得因此保留原实现：

- `backend/tests/test_balance.py`
- `backend/tests/test_devices.py`（这里测试的是 `engines.devices`，不是 devices 2.0 目录）
- `backend/tests/test_eval_run.py`
- `backend/tests/test_planning.py`
- `backend/tests/test_solver.py`

其中仍有价值的算法样例只能在新 computation provider 真正实现时迁入相应 provider 测试；本轮不得先造
无生产消费者的包装器来继续运行这些测试。

### 4.2 必须改写的过渡测试

- Worker session/lease 测试不得 monkeypatch `worker.executors.execute_calc` 来锁定旧执行器形状；改为注入公开
  application.worker/computation 阶段 gateway，验证 daemon 的租约、短事务、取消和阶段顺序；
- integration 中“0.8 未实现”可暂时验证稳定的结构化 unavailable 结果，但不得断言
  “旧计算链已删除”等实现文案、旧函数名或旧 engine 入口；
- 删除 `assembly_text is None`、双列兼容和旧快照升级断言；只验证当前快照 schema；
- 删除创建 `legacy.db` 后运行兼容迁移的测试；项目未发布，不测试旧数据库升级；
- `test_wave1_assembly.py` 不得锁定 `parser10`/`builder10` 内部模块图；改测公共装配入口、现行 contract 和
  依赖方向；
- 测试名、注释中的“0.8 起”“旧服务一致”“matches_old”等实施史改成当前行为描述。

### 4.3 必须保留的测试

- devices 2.0 用户输入、方程、参数、接口和 CSV 关键字实例化边界测试；
- assembly 1.0 当前公共契约、用户输入诊断和 `ValidatedAssemblyArtifact` 测试；
- finance 1.0、项目包 1.0 等各自现行契约测试；
- API 权限、事务、幂等、错误语义和关键业务值测试；
- Worker 租约、fencing、取消、迟到写回和短事务行为测试；
- 架构门禁，但门禁只检查稳定依赖规则，不锁函数名、行号、当前内部文件布局或临时未实现函数。

### 4.4 全量测试原则

“全量通过”是最终结果，不是保留过时测试的前提。每个切片必须同时修改生产代码和与新权威行为对应的测试；
删除测试时说明它锁定了哪条已废弃行为，以及由哪个当前 contract/行为测试覆盖。禁止单纯为提高通过数量保留
旧实现，也禁止在没有替代行为证明时机械删测。

## 五、实施波次

Muse 使用动态工作流。各子 agent 必须独立工作树、独立分支；波次内可并发，波次之间必须串行集成并审查。
每个切片单独提交。不得在主工作树并发编辑。

### Wave 0：事实调用图与测试裁决

- 以生产代码为准生成 API、application、领域、persistence、Worker、bootstrap、engines 的实际调用图；
- 标出每张 ORM 表的唯一领域所有者；
- 列出旧 engines、旧 snapshot 字段、旧 migration、装配双实现的生产消费者和测试消费者；
- 测试是唯一消费者的旧实现直接判定为死代码；
- 提交测试裁决清单和最小门禁调整，不提交会长期漂移的第二份架构清单。

### Wave 1：application 内部边界与 Worker 去转发

- 同族内部实现可直接组合；跨用例族改为稳定公开能力，或把事实重新归给正确领域；
- 不允许通过扩大 `__init__.__all__` 把所有内部函数都变成“公开”来掩盖穿透；
- 删除无业务增量的 `worker/lease.py` 式转发层，Worker 直接消费 `application.worker` 的阶段 gateway；
- 缩小 `application.worker` 公开面，只导出 Worker 真正消费的阶段命令和不可变结果，不导出实现模块对象和
  repository 级原子操作；
- 补跨 application 用例族实现穿透门禁，避免循环依赖和实现布局泄漏。

### Wave 2：ORM 与数据库所有权归位

- 把 ORM 类移动到各领域 `persistence.py` 或明确的本领域 `tables.py`；
- 拆开 `models/calc.py` 中 configuration/tasks，拆开 `models/audit.py` 中 audit/package/retention；
- 测试造数可以导入领域内部 persistence，但不得为测试创建生产公开 ORM 大门面；
- `db.py` 只保留连接、Session/Base 等基础设施；种子身份数据归 identity/application/bootstrap；
- 删除顶层混合 `models/`，由 bootstrap 显式注册各领域 metadata；
- 删除旧库 ALTER/DROP/回填分支、旧 snapshot 字段和 legacy migration 测试；当前 schema 从空库直接建立。

### Wave 3：bootstrap 组合根

- 新建单一 `bootstrap/`，分别装配 API、compute Worker、I/O Worker 所需能力；
- 统一初始化 database、storage、devices 2.0 registry 和 computation provider 目录；
- 删除 `main.py` 中无法被生产设置的 `_registry_status`，readiness 从已装配 context 的公开健康状态得出；
- 业务模块不自行读取环境并选择 provider；允许基础 adapter 接收由 bootstrap 传入的配置；
- 启动失败不发布半初始化状态，不增加 fallback。

### Wave 4：Worker 与 computation 空槽归位

- 从 Worker 删除数据集字段解释、缺省补值、SI 换算、时间轴物化和装配解释；
- 定义最小稳定的 computation 公共 contract：`GeneratorProvider`、`SolverBundle`、`SolverRuntime`、
  `ResultAdapter`、`ComputeResult` 及明确 unavailable；只定义真实边界，不实现 solver 功能；
- Worker 只按公开阶段结果驱动状态机；没有 provider 时明确失败或不 ready；
- 生产调用图无消费者的旧 `engines/` 整包删除，旧 engine 单测同步删除；
- 不把旧函数包进新类，不保留 compatibility facade，不让测试替代生产消费者。

### Wave 5：装配实现收敛、门禁与全局验收

- 保留当前 `ies.assembly 1.0.0` 公共契约，合并 parser/builder/validator 双实现；
- 删除实现史命名、旧版本转换和同一结构的重复复检；只在用户输入/公开自定义输出边界验证；
- 门禁验证：无顶层混合 models、无旧 engines、无跨 application 实现穿透、Worker 无 computation 业务、
  provider 只由 bootstrap 选择；
- 门禁不得锁定具体函数名、文件行号或为了当前树设置白名单；
- 修正 changelog 过早的“最终完成”表述；稳定手册只描述最终职责，不写本轮波次和实现文件清单；
- 在最终集成 HEAD 只运行一次 Docker 全量测试，随后删除本轮生成的非基础镜像与孤儿容器。

## 六、独立验收矩阵

Muse 只能报告“实现完成，等待 Codex 验收”。Codex 逐项确认全部成立后才能宣布完成：

| 边界 | 完成条件 |
| --- | --- |
| API | 每个业务动作只转交一个完整 application 用例；API 只做 DTO/响应/错误映射 |
| application | 只负责用例、事务和跨域顺序；跨族不穿透实现；无大范围公开内部函数 |
| 领域 | 每项规则、contract 和表有唯一所有者；跨域只走公开门面 |
| persistence | ORM 位于所有者领域；无顶层混合 models；无跨领域表读取 |
| bootstrap | API/Worker 的实现选择、初始化和 readiness 只有一个组合根 |
| Worker | 只负责 daemon、租约、资源隔离和阶段编排；无输入/单位/结果业务解释 |
| computation | 稳定边界 contract 存在；旧 engines 不存在；未实现能力明确不可用 |
| assembly | 当前 1.0 contract 保留；只有一条实现管线；无重复内部复检 |
| tests | 不以旧实现为真相；删除项有现行行为覆盖；门禁不形成第二份代码 |
| docs | 临时指南仍待 Codex 删除；changelog 不提前宣布完成 |

任一行未满足，都不得使用“彻底完成”“最终收口”或“残余为零”。

## 七、禁止事项

- 不修改架构宪法；发现冲突立即停止并报告；
- 不实现真实 0.8 solver/generator 算法，不恢复旧计算链；
- 不因测试失败保留旧实现、旧字段、旧迁移、兼容别名或纯转发层；
- 不新增内部 hash、文件完整性复核、重复解析校验、冗余诊断或假想防御；
- 不把领域规则搬进 core，也不建立新的全局 registry；
- 不通过修改测试去掩盖真实行为回归；
- 不在未完成全局矩阵时修改 changelog 宣告最终完成；
- 所有编译、格式化和测试只在 Docker，完成后只清理非基础测试镜像。

## 八、Muse 报告要求

报告必须逐行对应第六节矩阵，并提供：

1. 每波各独立分支、提交与集成顺序；
2. 删除的旧生产路径及其真实生产消费者为零的证据；
3. 删除/改写的测试、旧行为和替代覆盖；
4. ORM 表到领域所有者的最终映射；
5. bootstrap 对 API/Worker 的实际装配和 readiness 证据；
6. Worker 最终调用图及不再承担的数据/单位/结果职责；
7. 主动构造违规样例证明门禁确实能失败；
8. Docker 局部、波次与最终全量测试结果及非基础镜像清理结果；
9. 任何仍未满足的矩阵项，不得隐瞒或改称后续优化。
