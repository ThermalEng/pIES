# 前后端解耦整改复审结论（2026-08-21）

> 审查对象：`docs/fullstack-decoupling-review-2026-08-20.md` 整改后的当前仓库状态  
> 架构裁决：`manual/developer-guide/zh-CN/ARCHITECTURE_CONSTITUTION.md`  
> 基线：`HEAD=31d2423`，并包含 2026-08-21 当前全部未提交工作区改动  
> 结论：**不通过，不应提交或合并；存在 5 个 P1 阻断项和 15 个未闭环的 P2 问题。**

## 1. 审查范围说明

本次不是只检查某几个补丁，而是按原审查意见和架构宪法，对整改后的当前仓库状态做验收复审，包括：

- 已提交的设备注册、装配、帮助中心和存储重构；
- 当前工作区中的建模注册表、前端画布、设置页、Docker E2E 等改动；
- 当前 Playwright 报告和失败痕迹；
- Docker 镜像内的后端定向测试、设备目录打包测试和最小故障复现。

### 1.1 Git 状态必须先澄清

审查开始时 `git diff --cached` 为空，**暂存区没有任何文件**。Claude 的本轮成果实际上是未暂存工作区改动及未跟踪的 `frontend/tests/e2e/`，不能描述为“暂存区工作已完成”。

执行后续整改时应保留用户已有改动，只在全部门禁通过后再统一暂存。`node_modules/`、`test-results/` 和 HTML 报告继续由 `.gitignore` 排除，不得提交生成物。

## 2. 总体判断

整改方向有一部分是正确的：帮助中心已经改为渲染 `manual/` Markdown；人工评分假适配已从界面移除；存储模块已有统一实现、引用表和 reconcile 雏形；设备模块也开始提供公开 descriptor。

但当前实现仍未严格遵守原 review，主要表现为：

1. 设备 YAML 没有进入安装包，重新构建的 Docker 镜像不能加载默认设备目录；
2. 建模命令构建仍会提前修改线上注册表，最终发布又丢失生成的统一调用函数；
3. `core.registry`、静态端口映射、前端设备类型映射和兼容层仍大量存在；
4. 存储冲突路径仍会回滚调用方整个事务；
5. Playwright 当前 13 个场景中 7 个失败，模型连线和任务结果主流程没有通过。

因此这次整改不是“基本完成后的小修”，而是**边界已经搭出、关键消费者和原子语义尚未迁完**。

## 3. P1 阻断项

### RR-P1-01：Docker 安装包遗漏设备 YAML/CSV，默认注册表不可用

位置：

- `backend/pyproject.toml` 的 setuptools 配置；
- `backend/iesplan/devices/pricing.py`、`loader.py` 对包内 `catalog/` 的读取；
- `backend/iesplan/main.py:111-125` 的启动注册流程。

当前 `pyproject.toml` 只发现 Python package，没有声明 `devices/catalog/**/*.yaml` 和 `*.csv` 为 package data。Dockerfile 执行 `pip install .[dev]` 后，运行时导入的是 `site-packages/iesplan`，其下没有 `devices/catalog`。

Docker 证据：

```text
docker compose build backend
docker compose run --rm backend pytest -q tests/test_devices_init.py

12 failed, 23 passed, 28 errors
FileNotFoundError: .../site-packages/iesplan/devices/catalog/prices.yaml
```

这会让 API 的设备注册就绪状态失败，也会直接阻断 Worker 的命令注册。

整改要求：

- 在 Python 包配置中显式打包 `catalog/**/*.yaml`、`catalog/**/*.csv` 及未来声明允许的模型资源；
- 不依赖源码目录恰好位于当前工作目录；
- 增加“从构建后的 wheel/镜像导入并加载默认目录”的启动烟雾测试；
- API、compute worker、IO worker 必须使用同一构建产物完成 ready 验证。

### RR-P1-02：设备命令发布后丢失统一 callable，失败路径还会污染旧快照

位置：

- `backend/iesplan/modeling/build.py:89-155`；
- `backend/iesplan/modeling/registry_loader.py:130-169`；
- `backend/iesplan/modeling/command.py:136-149`。

`build_command()` 不是纯构建器：它在返回前调用 `register_command(cmd, fn=entry)`，立即修改当前全局注册表。随后 `register_catalog_commands()` 又调用 `replace_all_commands(staged)`，但没有传入 generated callable 映射，于是统一五参数 wrapper 被清空。执行时只能回退解析原始函数，例如 `pv_output`、`periodic_repeat`，再以统一调用签名调用，最终报类型错误。

同样，因为候选构建阶段已经逐项注册，后续设备构建失败时，早先候选已经泄漏进旧注册表，所谓“失败保留旧快照”不成立。

已复现结果：

```text
resolved_entry iesplan.modeling.functions pv_output
call_failure TypeError: float() argument must be a string or a real number, not 'dict'
reload_failure AppError
old_snapshot_preserved False
partial_command_published True
```

整改要求：

- 把 `build_command` 改成无副作用构建，返回命令描述与统一 callable；
- 计算命令和全部设备命令一起构建成候选快照；
- 完成 ID 冲突、函数解析、profile 和调用契约校验后才发布；
- 增加至少一个真实设备 `call_command()` 测试，以及“第 N 个候选失败后旧快照逐项完全相等”的测试。

### RR-P1-03：存储唯一键冲突仍会回滚调用方整个事务

位置：`backend/iesplan/storage/service.py:291-309,481-501`。

代码调用 `db.begin_nested()` 后，在 `IntegrityError` 分支调用 `db.rollback()`，并声称“仅回滚 savepoint”。这个注释不符合 SQLAlchemy Session 语义：`Session.rollback()` 会回滚当前会话最外层事务。

Docker 最小复现先写入一条外层事务数据，再制造 savepoint 内唯一键冲突并执行相同写法，结果为：

```text
outer_row_count 0
```

原 review 的 STO-03 因此没有修复。

整改要求：

- 使用 `with db.begin_nested():` 或显式保存 nested transaction handle，只回滚该 handle；
- 存储公开方法不得调用调用方 Session 的 `commit()` 或全局 `rollback()`；
- 增加集成测试：冲突去重前先写入一个无关业务行，冲突处理后该行仍位于同一外层事务中且可提交；
- 同时覆盖 `put_object` 和 `attach` 两条竞争路径。

### RR-P1-04：真实设备端口仍未贯通，合法画布连线失败

位置：

- `backend/iesplan/api/model.py:31,119-137`；
- `backend/iesplan/services/model.py:69-96`；
- `frontend/src/pages/model/canvasModel.ts:91-147`；
- `frontend/tests/e2e/auth-navigation-model.spec.ts` 场景 3。

前端仍按设备 `type_id` 维护 `PORT_RULES`，并用启发式规则兜底；后端设备 API 和项目设备创建仍消费 `core.registry` 及静态映射。YAML 中的真实端口名称、方向和载能没有形成 API DTO → 前端画布 → 项目写入 → 装配检查的一条链路。

Playwright 已记录：热泵到热负荷的合法连接操作后没有生成 `.react-flow__edge`。当前对 `fitView` 和 pointer-events 的修改没有解决端口契约问题。

整改要求：

- `devices` 通过公开 facade 提供设备目录 DTO，其中包括稳定端口 ID、方向、载能、数量/单位和必要能力；
- API 只做该领域 DTO 的序列化，不新建“替前端换算”的业务接口；
- 前端按 DTO 预处理和渲染句柄，只保留自环、重复边等通用交互规则；
- 删除前后端按设备类型维护的端口表和默认方向猜测；
- 装配模块从同一份公开 descriptor 构建自己的模块内候选注册表，不能共享 devices 的可变注册对象；
- Playwright 用真实句柄完成合法连接并断言服务端保存后的端口 ID/载能一致。

### RR-P1-05：任务创建后的详情/结果契约不完整，主流程中断

位置：

- `frontend/src/api/client.ts:1468-1497`；
- `frontend/tests/e2e/data-config-tasks.spec.ts:158-180`；
- 对应后端 task detail/result API。

Playwright 中任务创建已提示成功，随后 `/api/projects/{project_id}/tasks/{task_id}` 或结果读取出现 `404 RES-MISS-003`，任务行消失并超时。前端 `tasks.get()` 在 evidence 为空时不区分任务状态就尝试结果端点，也表明“任务元数据”和“结果可用性”没有明确契约。

整改要求：

- 任务详情在 queued/running/completed 各状态都必须稳定可查；
- DTO 明确 `result_available` 或等价状态，不允许客户端靠 404 猜测；
- 只有完成且结果可用时才读取结果；结果尚未生成不是页面级失败；
- 保留现有 Playwright 的“提交 → 状态 → 结果 → 导出”为验收门禁，不得通过弱化断言规避。

## 4. P2 必须整改项

### RR-P2-01：`replace_all_commands` 仍不是原子发布

`backend/iesplan/modeling/command.py:145-149` 依次 `clear()`、`clear()`、`update()`，没有锁，也没有一次性替换快照引用。并发 reader 可以观察到空表或命令/callable 不匹配。

应创建新的不可变快照对象，在一个锁保护的极短临界区内替换单一引用；reader 先取得快照引用，再在该快照内查找。不要把“连续执行几行”称为原子。

### RR-P2-02：权威 YAML 只接入了 modeling，其他消费者仍使用静态注册表

仍存在：

- `backend/iesplan/api/model.py`、`assembly/builder.py`、`services/model.py`、`services/config.py`、`services/validation.py`、`engines/planning.py` 导入 `core.registry`；
- `assembly/checker.py` 和 `services/model.py` 保留 `_DEVICE_PORT_DIRECTIONS`；
- 前端保留 `PORT_RULES`；
- `devices.list_device_descriptors()` 目前主要只被 modeling loader 消费。

这不符合“开放优先”。新增一个只存在于 YAML 的设备仍不能可靠贯通目录、画布、项目写入、装配和计算。

整改不是建立跨模块 `core.registry`，而是：每个模块消费 `devices` 的不可变公开 descriptor，再构建自己的模块内状态。`core.registry` 应退役，纯 `ParameterSpec` 等无状态类型迁到 `core/contracts` 或明确的领域 contracts。

### RR-P2-03：公开 descriptor 只是 frozen dataclass，不是深度不可变

`backend/iesplan/devices/spec.py:125-175` 的字段仍包含可变 `list`、`dict`，`function` 也只是浅复制。消费者可以在校验后修改 descriptor，甚至触及共享的嵌套对象。

应使用 tuple、只读 mapping，并对嵌套值做 deep-freeze；或输出完全独立、可序列化的不可变值对象。增加修改失败及快照不受源对象后续修改影响的测试。

### RR-P2-04：模块公开边界仍过宽，并跨模块导入私有符号

问题包括：

- `devices/__init__.py` 同时导出 loader、pricing、YAML 解析、registry 实现和公开 descriptor，无法称为窄 facade；
- `modeling/registry_loader.py:28` 导入 `_COMPUTE_COMMANDS`；
- 保留 `reload_catalog_commands()`，与“正式发布前不实现运行期热加载”的决定冲突；
- `base_dir` 参数已经没有真实作用。

应把组合根需要的公开 provider/candidate API 明确定义在各模块 facade；外部模块不得导入下划线符号、loader、路径推导或价格解析实现。删除未发布兼容入口和热加载入口。

### RR-P2-05：装配静态回退并未按已采纳决定删除

`backend/iesplan/assembly/checker.py:236-250,300-320` 仍在注册表未初始化或为空时回退静态设备表和 `_DEVICE_PORT_DIRECTIONS`。虽然转换异常已不再被 `except Exception` 吞掉，但原 review 9.5 和架构宪法要求的是**删除回退**，不是缩小捕获范围。

注册表未初始化、为空或 descriptor 不合法都应使装配不可用并暴露诊断；不能继续用另一份静态事实源运行。

### RR-P2-06：存储兼容层、私有路径和公开 handle 泄漏仍存在

位置：

- `backend/iesplan/services/objects.py`；
- `backend/iesplan/api/objects.py`、`api/admin.py`、`api/health.py`、`api/exports.py` 及多个 service；
- `backend/iesplan/storage/contracts.py:25-37`。

`services.objects` 明确自称“兼容入口”，还动态导入 `storage.adapters.filesystem` 的私有 `_objects_root/_tmp_root`。多个新旧调用方继续依赖它。`ObjectHandle` 又公开 `storage_path` 和缓存字段 `ref_count`。

项目尚未发布，不应保留过渡层。应把所有调用方一次迁到 `iesplan.storage` 的窄公开协议后删除兼容文件；路径只在 adapter 内解释；业务公开 handle 只保留对象 ID、摘要、大小、媒体类型等稳定字段。ORM 模型也不应通过通用 `models` facade 暴露给业务模块。

### RR-P2-07：对象清理“计划—执行”没有稳定计划标识

`POST /api/admin/objects/cleanup` 只接受 `dry_run`。预览和执行会分别重新计算候选集，因此用户确认的集合可能不是实际删除集合。

应让 dry-run 返回不可混淆的 `plan_id`/版本、候选摘要和失效条件；执行时提交该计划标识，并在事务内再次验证引用、状态和版本。候选变化则拒绝执行并要求重新预览。

### RR-P2-08：财务配置仍保留旧扁平兼容和静默默认值

位置：

- `frontend/src/pages/ConfigPage.tsx:360-366`；
- `frontend/src/api/client.ts:1416-1424`。

页面已优先读取 `params.economic`，但仍回退旧扁平键；财务基准确认也使用 `params.economic ?? params`，缺字段时静默填默认值。原 FE-BE-01/05 只完成了一半，并违反“未发布不保留兼容层”。

应定义严格 `EconomicParametersDto`，前端直接构造后端需要的嵌套输入；字段缺失或非法应形成可定位诊断，不能替用户补默认假设后确认基准。

### RR-P2-09：后端 `analysis` 任务仍未同步到前端

`frontend/src/types.ts:528-535` 没有 `analysis`，`TasksPage.tsx:62` 只允许提交 calc/optimization/uncertainty，也没有扫描参数表单和 i18n 文案。原 FE-BE-06 未执行。

若该能力属于当前产品范围，应完成类型、表单、校验、状态、结果和文档；若不属于，应在本阶段删除后端公开入口。不能长期保留只能手工调用 API 的半功能。

### RR-P2-10：Playwright 帮助中心断言拒绝了正确规范路由

`frontend/tests/e2e/admin-help-lang.spec.ts:80-87` 期望 `/help/` 和 `/help/zh/...`，实际规范路由是 `/help` 与 `/help/zh-CN/...`。场景 8—10 因测试自身契约错误而失败。

应统一路由常量或由目录数据生成期望值，所有重复断言使用规范 locale ID `zh-CN`。不要修改产品路由去迎合错误测试。

### RR-P2-11：E2E 造数留下竞争管理员会话

`frontend/tests/e2e/setup/api.ts:69-88` 的 `createEngineer()` 会获取并缓存管理员 token；随后场景 7 又在 UI 中登录同一管理员，单窗口会话接管使其中一个会话失效，最终 reactivation 请求返回 401。

造数应使用生命周期明确的独立 API context，并在被测 UI 登录前登出/关闭管理员会话；或提供测试专用 seed 流程，但不能绕过被测权限逻辑。场景 7 的 UI 操作必须仍由真实管理员会话完成。

### RR-P2-12：Dialog 焦点实现选中了关闭按钮，且没有恢复焦点

`frontend/src/components/ui.tsx:557-631` 打开时查询 DOM 中“第一个可聚焦元素”；由于 header 在 body 之前，第一个元素是关闭按钮，不是项目名称输入框。关闭时也没有保存并恢复触发元素。

应支持明确 `initialFocusRef`，默认优先 body 内的第一个表单控件；打开时在布局完成后转移焦点，关闭时恢复触发元素，并保留焦点圈定。现有键盘断言应保留。

### RR-P2-13：E2E 只等待容器进程启动，没有等待应用 ready

`docker-compose.yml:110-112` 使用 `service_started`；`global-setup.ts:20-27` 又只探测一次。首次构建、迁移或注册稍慢就会产生随机失败。

应给 backend 和 web 配置 healthcheck，web 依赖 backend ready，e2e 依赖 web healthy；全局 setup 仍保留有上限、带退避的 ready 轮询。验收前应重建并重建容器，保证 API、worker、IO worker、web 来自同一 revision。

### RR-P2-14：正式用户指南混入开发过程状态

`manual/user-guide/zh-CN/planning-workflow.md:62` 写入“仍需按审查结论整改”等开发过程文字。这与正式用户指南不得暴露过程争议的文档原则冲突。

当前未完成功能应不出现在正式能力列表，或用面向用户的“当前限制”描述实际行为，不引用审查或整改过程。完成 analysis、端口和任务流程后，同时更新用户指南、开发者契约及帮助中心目录内容。

### RR-P2-15：整改顺序没有先冻结契约和测试，容易让实现被旧测试拉回

当前整改中出现了“先改实现、运行旧测试失败、再围绕旧断言修补实现”的迹象。例如帮助中心测试仍断言旧的 `/help/zh/...`，而规范路由已经是 `/help/zh-CN/...`。如果把历史测试当成最高权威，就会迫使正确实现重新兼容废止契约。

测试的效力低于架构宪法、已批准 contract 和领域规格，但**一旦目标契约确定，应先修改或新增对应模块测试，让它针对目标行为稳定失败，再修改实现直至通过**。这里的“测试先行”不是无条件保留旧测试，而是先把测试迁移到已经批准的新契约。

必须遵守以下域边界和顺序：

1. 先依据宪法、ADR/规格确定目标 contract、失败语义和验收值；
2. 先改后端单元、模块公开协议、HTTP DTO、事务和 Worker 测试，记录预期红灯；
3. 再改后端实现，在 Docker 中把后端测试全部跑绿，并固定 OpenAPI/DTO；
4. 后端相关契约稳定后，才修改前端 contracts、mapper 和前端测试，先得到针对新契约的红灯；
5. 再改前端实现，在 Docker 中通过前端单元、组件和构建测试；
6. 前后端分别通过后，最后运行独立的系统级 Playwright 验收。

测试不得跨域承担实现责任：

- 后端测试只测试后端模块、HTTP contract、数据库、存储和 Worker，不导入或启动前端代码；
- 前端单元/组件测试只测试前端 contract、mapper、状态和 UI，可使用严格 DTO fixture/mock，不直接操纵后端数据库或导入后端内部代码；
- 后端生成的 OpenAPI/schema 是前端契约测试的输入，不复制为两份手工事实源；
- Playwright 是明确的系统验收层，天然跨前后端，但不归属于任何一个业务模块，也不能代替两端各自测试。

建议把当前 `frontend/tests/e2e/` 移到根级 `tests/e2e/`（或明确的 `qa/e2e/`），体现其系统验收身份。Playwright 可以使用 API 做隔离造数和清理，但不能修补、猜测或替代前后端契约。

任何废止测试都必须说明它对应的旧契约及替代测试；禁止为了变绿而删除关键业务断言，也禁止为了让旧测试通过而恢复兼容层、fallback 或旧字段。

## 5. 原审查事项闭环状态

| 原事项 | 当前状态 | 复审结论 |
|---|---|---|
| BE-REG-01 | 部分完成 | modeling 开始消费公开 descriptor，但 facade 过宽，其他消费者未迁移 |
| BE-REG-02 | 未完成且有回归 | 候选构建有副作用、callable 丢失、发布非原子 |
| BE-REG-03 | 部分完成 | 不再吞所有转换异常，但静态回退仍存在 |
| FE-BE-01 | 部分完成 | 新层级优先，但保留旧扁平键兼容 |
| FE-BE-02 | 未完成 | 前端仍硬编码端口，Playwright 合法连接失败 |
| FE-BE-03 | 基本完成 | 人工评估假输入已删除，系统评估语义明确；需保留回归测试 |
| FE-BE-04 | 未完成 | YAML 未贯通 API、项目服务、装配和前端 |
| FE-BE-05 | 部分完成 | 读取对象层级已修正，但仍回退和静默补默认值 |
| FE-BE-06 | 未完成 | analysis 仍无前端类型和入口 |
| STO-01 | 部分完成 | 有统一 storage 实现，但兼容 facade 和双入口仍在 |
| STO-02 | 部分完成 | ObjectRef 已成为设计权威，但需补全所有 owner 的 attach/detach 验收 |
| STO-03 | 未完成 | `Session.rollback()` 仍回滚调用方外层事务 |
| STO-04 | 部分完成 | 已有 reconcile，但需补故障注入和幂等恢复证明 |
| STO-05 | 未完成 | 兼容层、私有 adapter、路径和 ORM 仍泄漏 |
| STO-06 | 部分完成 | 需通过删除/容量故障测试证明无非托管副本和静默放行 |
| STO-07 | 部分完成 | storage DTO 已收敛，但调用仍经兼容服务，清理契约不稳定 |
| FE-DOC-01 | 功能主体完成 | Markdown 帮助中心已落地，路由测试和正式文档内容仍需修正 |
| QA-E2E-01 | 框架完成、验收失败 | 当前报告 13 项中 7 项失败，不能作为通过证据 |

## 6. 建议执行顺序

Claude 应按以下顺序处理，避免继续在错误基础上修补 UI。每一步都先更新该域测试到目标契约并确认红灯，再修改该域实现：

1. 冻结本轮后端公开 contract，并先迁移后端测试到目标语义；
2. 修复 package data，确保全新 Docker 镜像能加载设备目录并 ready；
3. 重写建模命令候选构建与原子快照发布，通过真实命令调用和失败不污染测试；
4. 修复存储 savepoint 事务语义，迁移并删除 `services.objects` 兼容层；
5. 定义 devices 的窄、深度不可变公开 descriptor/DTO，退役 `core.registry`；
6. 迁移后端 API、项目服务、装配和计算，删除设备类型静态映射和回退；
7. 后端 Docker 测试全绿并固定 OpenAPI/DTO 后，再迁移前端测试到新 contract；
8. 修改前端画布、财务 DTO、analysis 和任务结果适配；
9. 前端 Docker 测试全绿后，修复独立 E2E 的会话、帮助路由、Dialog 焦点和 ready 等待；
10. 更新用户指南与开发者指南；
11. 最后运行完整 Playwright 真实用户验收，通过后再暂存、提交。

## 7. 必须达到的验收门禁

### 7.1 后端与注册表

- 全新 `docker compose build --no-cache backend` 后，安装包内可找到并加载 YAML/CSV；
- API ready，compute worker 与 IO worker 使用同一 revision 启动；
- 设备目录、命令注册、装配和项目模型均可接受新增的“仅 YAML”测试设备；
- 任一候选失败时，旧命令与 callable 快照完全不变；
- 至少一个 mechanism、一个 data_repeat、一个 stateful 命令经 `call_command()` 真正执行成功；
- 全仓库业务代码不再导入 `core.registry`，不存在设备类型静态端口表和装配回退。

### 7.2 存储

- 所有业务调用只走 `iesplan.storage` 公开协议；
- 删除 `services.objects` 兼容层及跨模块私有 adapter 导入；
- 唯一键竞争不会回滚调用方事务；
- 每类 owner 的创建/替换/删除都有 attach/detach 对称测试；
- 清理执行绑定已确认的稳定计划，并在执行时重新验证引用；
- 故障注入覆盖落盘后 DB 失败、文件缺失、摘要错误、容量不可测和重复 reconcile。

### 7.3 前端与 Playwright

- 财务配置不读取旧扁平键，不静默补默认基准；
- analysis 可由 UI 提交并查看结果，或从本阶段前后端公开能力中一致删除；
- 画布从服务器 DTO 渲染真实端口，合法连接保存后重新加载仍存在；
- 任务从提交到详情、完成、结果和导出全流程通过；
- 帮助中心 `/help`、`/help/zh-CN/...`、语言切换和刷新深链接通过；
- Dialog 初始焦点、焦点圈定、Esc 关闭和焦点恢复通过；
- Chromium 首批 13 个场景全部通过，失败时保留 trace/screenshot；不能以测试本身已存在为完成标准。

### 7.4 Docker 测试记录

本次复审实际执行：

```text
# 当前镜像重建后定向套件
tests/test_devices.py
tests/test_modeling.py
tests/test_selector.py
tests/test_objects_api.py
tests/test_registry.py
tests/test_tasks_api.py

122 passed, 1 warning

# 设备初始化与包资源套件
tests/test_devices_init.py

12 failed, 23 passed, 28 errors

# 当前 Playwright 报告
13 tests: 6 passed, 7 failed
```

122 个定向测试通过不能抵消设备安装包测试和浏览器主流程失败；当前门禁结论仍为不通过。

## 8. 给下一轮复审的交付要求

下一轮提交复审前，请提供：

- 清晰的 Git 暂存区差异，不要把未暂存工作区称为 staged；
- Docker 镜像构建日志和完整后端测试摘要；
- API、worker、IO worker、web 全部由同一 revision 重建后的 ready 状态；
- Playwright 13/13 通过摘要及失败证据目录（若仍失败则不得申请通过）；
- 原事项与本文件 RR 编号逐项对应的关闭说明；
- 同步更新过的用户指南和开发者指南清单。
