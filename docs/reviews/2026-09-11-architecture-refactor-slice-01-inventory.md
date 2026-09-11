# 解耦重构切片 1：依赖盘点与架构门禁

> 日期：2026-09-11
> 基线：`bfa92b6`（后继提交见本切片提交说明）
> 临时指导：`docs/reviews/architecture-refactor-workflow.md` §1（仅本次重构窗口有效）
> 状态：基线门禁已建立，业务行为零变更

## 结论

后端调用方向已盘点完毕，现状与目标的差距全部转化为可执行的架构门禁。
本切片不改变任何业务行为，只新增 5 个门禁测试（门禁 4–8）与表归属清单。

## 盘点结果（调用方清单）

### API 层：36 处事务提交，10 个模块跨 service 编排

- `db.commit()` 共 36 处：tasks(3)、admin(1)、auth(6)、datasets(1)、
  objects(3)、validation(2)、projects(10)、config_revisions(5)、exports(2)、
  results(3)；`.rollback()` 0 处。
- 同时依赖 ≥2 个 `services.*` 子模块的路由文件共 10 个
  （tasks/model/config/admin/auth/datasets/validation/projects/
  config_revisions/results）；`exports`/`health` 为单依赖，
  `model_templates`/`project_models` 已示范目标方向（只调 application 用例）。
- ORM 直接导入仍沿用门禁 3 白名单，未新增。

### application 层：事务已部分归位，但仍直调 services/ORM

- `application.model_templates.service`、`application.projects.model_save`
  已拥有 `commit/rollback`（目标方向正确），但仍直接 import
  `services.project` 与 models ORM（切片 6 改调领域公开接口）。
- 其余跨模块流程（项目/数据集/配置/任务/结果/项目包）仍由 services 统包，
  application 覆盖率不足（切片 6 处理）。

### 领域与 persistence：表归属明确，跨表访问集中在 services

- 14 个 models 子模块归属见 `TABLE_OWNERS`
 （`test_architecture_gates.py`）；`models.common` 仅为共享基元，无业务表。
- 跨表访问共 62 对 `(访问方, 表)`，全部登记在
  `WHITELIST_CROSS_MODEL_IMPORTS`；最重的是 `services.package`(7 表) 与
  `services.tasks`(7 表)，收敛顺序按 workflow §3
 （project → identity → dataset → config → task → result → package）。
- `services.project` 被 3 个 service 函数级反向 import
 （`config_revisions`），属后续切片消除的 service 间穿透。

### Worker：7 处 services 直调 + 多领域 ORM 直读

- `lease → queue/tasks`、`main → queue`、`runner → dataset/project`、
  `executors → queue/tasks`（切片 8 移入 `application.worker`）。
- 另直读 `models.calc/result/dataset/project/uncertainty`，
  直调 `engines`、`analysis.wrapper`、`finance`（切片 8/10 处理）。

### analysis：4 处计算执行直调

- `wrapper → engines.eval_run`、`wrapper → assembly.plan`、
  `sensitivity → services.tasks/identity`（切片 10 处理）。
- `wrapper → finance` 为纯函数复用，暂接受，不在门禁内。

### assembly：rules 反向依赖 checker 已确认

- `rules/completeness.py`、`rules/connection.py`、`rules/solvability.py`
  import checker 内部符号（`_split_model`、`_PEAK_PARAM_BY_LOAD`、`_to_watts`、
  `ensure_ports`、`units_compatible` 等），门禁 2 白名单已有 4 条对应登记，
  切片 9 提升为公开 contract 后移除。

### storage/audit：storage 直写业务审计 ORM

- `storage.service` 直接 import `models.audit`（`AuditLog`、`RetentionRule`），
  切片 11 改由 audit facade/application 记录；`storage.persistence` 仅用
  `models.common` 基元，无业务表访问。

## 本切片新增门禁（证明目标效果的测试）

在 `backend/tests/test_architecture_gates.py`（纯 stdlib AST，不 import 业务模块）：

| 门禁 | 断言 | 现状基线 |
| --- | --- | --- |
| 4 `test_api_no_transaction_commit` | api 无 `.commit()/.rollback()` | 36 处白名单 |
| 5 `test_api_no_multi_service_fanout` | 每个 api 模块至多 1 个 services 依赖 | 10 模块白名单 |
| 6 `test_worker_no_direct_domain_services` | worker 无 services 导入 | 7 对白名单 |
| 7 `test_analysis_no_direct_engine_or_services` | analysis 无 engines/services/assembly.plan 导入 | 4 对白名单 |
| 8 `test_table_ownership_no_new_cross_imports` | 跨表 ORM 访问不新增 | 62 对白名单 + `TABLE_OWNERS` |

任一新增越界依赖都会使对应测试失败；后续切片每迁移一条就从白名单移除一项，
白名单清空后门禁转为硬强制。

## 切片 1 五问

- 删除了哪条旧依赖：无（本切片只建基线，不删业务代码）。
- 谁接管了原职责：5 个新门禁接管“发现越界依赖”的职责，此前只靠人工审查。
- 调用方向如何变化：未变化；变化的是约束——此后任何新代码都必须符合目标方向。
- 哪个测试或门禁证明目标效果：门禁 4–8（本文件上表）；Docker 全量测试待沙箱放行后补跑。
- 是否仍有未迁移调用方：是，见上文各节；按切片 2–13 顺序迁移。
