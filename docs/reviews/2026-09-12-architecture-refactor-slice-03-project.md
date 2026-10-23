# 解耦重构切片 3：project persistence

> 日期：2026-09-12
> 基线：`4453eb7`（切片 2）
> 临时指导：`docs/reviews/architecture-refactor-workflow.md` §3（project 在首位）
> 状态：projects 系 4 张表的 ORM 查询已全部收敛到 project 域 repository

## 结论

`iesplan/project/persistence.py` 是 `ProjectRepository` 协议的实现，
`services.project` 自身不再直连 projects 系表；model/config/identity/
tasks/validation/results/config_revisions 对 project 表的读写一并收敛，
8 个文件的 `models.project` 导入已删除。package 的重型导入/导出路径
延至切片 5（package persistence），其提议创建与初始草稿行已先行收敛。

## 迁移明细（调用方 → repository）

- `services.project`：建/查/列出/计数/状态机/指针/草稿/版本/引用全部委托；
  删除 `_new_draft_row`、`_next_version_no` 两个私有没有调用后；公开函数签名
  不变，行类型由 ORM 改为记录（字段同名；时间改为 ISO 字符串，JSON 输出一致）。
- `services.model`：工作图建图/同步经 `get/create_draft`、`update_draft_content_ref`。
- `services.config`：存在性/币种/指针/修订号经 repository；`_sync_draft_config`
  改传 `ProjectRecord`。
- `services.identity`：删账号级联与预告经 `list/set_project_status`（cursor 分页取全）。
- `services.tasks`：快照组装经 `get_version`；行类型改记录。
- `services.validation` / `services.results` / `services.config_revisions`：
  存在性/指针经 repository；config_revisions 的 8 处指针写改为一次
  `update_revision_pointers`（finance 四指针同行移动）。
- `services.package`（子集）：提议建项目、初始草稿行经 repository；
  确认导入的版本重建与导出收集延至切片 5。

## Protocol 演进（切片 2 → 3，有记录的调整）

- `create_project`：只建裸行（初始草稿需对象存储，调用方随后补建）+ `schema_version` 参数。
- `create_draft`：revision 改内部 max+1 计算（调用方不再传）；先翻转旧 current
  再插新行（`uq_drafts_current` 部分唯一索引要求）。
- `create_version`：+ `parent_version_id` 参数（空时沿用当前指针）；
  版本内容对象引用行由实现内建。
- `list_projects`：`status` 改 `statuses` 列表 + id 倒序 cursor（替代 created_at 排序，
  更稳定；列表输出等价）。
- 新增：`get_draft`（跨域指针解引用）、`update_draft_content_ref`（内容指针维护）、
  `count_projects_by_owner`（GROUP BY read model）。

## 两个实测发现（已处理 / 已记录）

1. 草稿/版本的 `created_at`：drafts 表本无该列，旧 ORM 缺失属性读值为 None，
   旧 API 输出该键为 null。`DraftRecord.created_at` 恒 None（兼容形状），
   `ProjectVersionRecord.created_at` 如实映射。切片 3 测试已锁定。
2. SA 2.0 下 flush 失败后会话不可继续（savepoint 也保不住可用性，已用最小
   对照实验证伪）。repository 不再包 savepoint，冲突转领域错误后调用方回滚
   （与旧 services 契约一致）。persistence 蓝图建议 savepoint 的部分与实测不符，
   属长期指南外事项，不修改宪法/指南；application 层（切片 6）须按“回滚重试”
   设计。storage 的 savepoint 模式是否同样受影响，切片 11 复核。

## 切片 3 五问

- 删除了哪条旧依赖：services 层对 projects 系表的直接 ORM 访问
  （9 文件 `models.project` 导入删除其 8；`_new_draft_row`/`_next_version_no` 删除）。
- 谁接管了原职责：`iesplan/project/persistence.py`（16 个协议方法实现 + 行→记录映射）。
- 调用方向如何变化：services → project 域门面（公开函数），不再直连 ORM；
  跨域写草稿/指针改走 `create_draft`/`update_draft_content_ref`/
  `update_revision_pointers`。
- 哪个测试或门禁证明目标效果：`test_refactor_slice03_project.py`（10 测试：
  增删查改/冲突回滚/软删隔离/指针/草稿不变量/版本链）+ 门禁 8 白名单移除
  8 对 project 跨表条目并新增 `project.persistence` 归属条目；
  Docker 全量 1336 passed（仅 1 个预存失败除外，见下）。
- 是否仍有未迁移调用方：package 确认导入/导出收集（切片 5）、api/health/limits
  的计数查询（切片 7）、dataset.require_project（切片 4，连同 dataset 服务整体）。

## 测试结果

- Docker 全量：1336 passed；`test_integration.py::test_full_business_chain`
  失败系预存（基线 `4453eb7` 同样失败：旧计算执行链已删，任务无法跑完，
  与本切片无关，已用 stash 对照验证）。
- ruff check：本切片新增/修改行无新违规（预存 E501 等未动）；
  ruff format clean；`git diff --check` 通过。
