# 解耦重构切片 2：公开 contract 与 repository Protocol

> 日期：2026-09-11
> 基线：`12c7465`（切片 1）
> 临时指导：`docs/reviews/architecture-refactor-workflow.md` §2
> 状态：七域公开面已建立，无调用方迁移（调用方迁移在切片 3–7）

## 结论

七个领域包只提供公开 contract（不可变 DTO + 领域错误）与 repository
Protocol，尚无实现。调用方向尚未变化，但替换面已经稳定：后续切片只需
实现协议、迁移调用方，不再搬运内部细节。

## 新增公开面

| 域包 | 表归属 | contract | repository 协议 |
| --- | --- | --- | --- |
| `iesplan.project` | projects/drafts/project_versions/version_refs | 4 记录 + ProjectPage | ProjectRepository（13 方法） |
| `iesplan.identity` | users/roles/user_roles/credentials/window_sessions/app_settings/auth_events | 7 记录（无任何密钥材料） | IdentityRepository（21 方法） |
| `iesplan.dataset` | datasets/dataset_versions/dataset_files | 3 记录 | DatasetRepository（10 方法） |
| `iesplan.configuration` | finance_profiles/finance_overrides/effective_finance_revisions/planning_configs/calc_configs | 5 记录 | ConfigurationRepository（16 方法） |
| `iesplan.tasks` | calc_snapshots/tasks/task_attempts/task_leases/task_progress/task_diagnostics/compute_slots/uncertainty_* | 10 记录 | TasksRepository（19 方法） |
| `iesplan.results` | evidence_packages/result_assessments/result_index/result_selections/reports | 5 记录 | ResultsRepository（13 方法） |
| `iesplan.package` | import_proposals（读写） | 1 记录 | PackageRepository（4 方法） |

命名说明：目标文本称配置域为 config，但 `iesplan.config` 已被项目
settings 模块占用，故包名为 `configuration`；表归属与切片划分不受影响。

归属裁决两则（均以“唯一拥有者”收尾，无双轨）：
- `calc_configs` 物理上在 `models.calc` 中，但语义是用户可编辑的计算配置，
  归 configuration 域读写；tasks 域只消费其快照。
- 导入提议（`import_proposals`）状态机归 package 域；审计日志写入归
  audit facade/application（切片 11），不在本包。

## 协议硬规则（实现者必须遵守，切片 3–5 落实）

- repository 只做查询、写入、flush，必要时 savepoint；绝不 commit/rollback；
- 唯一冲突 → 领域 Conflict 错误，不存在 → 领域 NotFound 错误；
- 不返回 ORM 或懒加载图；不做权限判定（归 application，切片 6）；
- 方法首参一律为调用方事务拥有的 `db: Session`（契约测试强制）。

## 切片 2 五问

- 删除了哪条旧依赖：无（只新增公开面；另含切片 1 门禁文件的纯格式整理）。
- 谁接管了原职责：七域 `contracts.py` 接管“跨模块传递的形状定义”，
  七域 `repository.py` 接管“持久化能力形状定义”；此前形状散落在各
  service 的 dict 返回与 ORM 行中。
- 调用方向如何变化：未变化；变化的是此后新增/迁移代码必须依赖公开协议，
  不得直连 ORM 或 service 实现（门禁 8 + 本切片纯度测试共同约束）。
- 哪个测试或门禁证明目标效果：
  `tests/test_refactor_slice02_contracts.py`（5 测试：门面导出、frozen、
  错误复用基类码、首参 db: Session、源码纯度）+ 门禁 1–8 回归，
  Docker 中 15 passed；ruff check/format 通过；`git diff --check` 通过。
- 是否仍有未迁移调用方：是，全部现状调用方仍走 services/ORM；
  repository 实现与调用方迁移在切片 3–7。
