# 后端解耦重构 Wave 1–5 收尾记录（0.8 开发前）

> **纠偏注（fe3d83b 审查裁决）**：本文件所称“Wave 1–5 已完成”结论已被审查否定，
> 仅保留为历史记录，不作为完成依据。真实完成状态以纠偏 goal 验收为准，
> 见 `architecture-refactor-workflow.md` 顶部“fe3d83b 审查裁决与纠偏目标/纠偏波次”。

基线链：`ca41eff`（Slice 1–9）→ `5c40b01`（Wave 1）→ `5dd34bf`（Wave 2）
→ `f9ecab7`（Wave 3）→ `6a18714`（Wave 4）→ `71f5c6b`/`088c255`/`22335fc`/`fbb0823`
→ `9368852`/`406109b`/`ba098e7`（Wave 5）。

- Wave 1：领域 services 收敛（storage/package/validations 等）。
- Wave 2：application 用例族建立（A 配置/数据集/校验、B 身份、C 任务/结果/包）。
- Wave 3：API 全面改经 application（项目/数据集/管理/任务/结果/导出/配置/校验/模型）
  + Worker 改经 `application.worker`；门禁 4/5/6 白名单大幅清理。
- Wave 4：auth/health/objects/limits 收尾；门禁 5 扇出白名单清空。
- Wave 5：admin ORM/commit 消除、`LEGACY_SERVICE_CALLS` 清零、同域私有提升公开、
  Worker ORM 查询上收（新增 worker 无 ORM 门禁）、`project/content` 转纯函数、
  测试缝线跟随（500 注入点改 application，OWNED_MODELS 补 model 三表）。

## 最终验收矩阵证据

- API：无 `services`/`storage`/`models` 直引（除 `models.common` 共享基元常量）、
  无路由层 commit、无多 service 编排——门禁 3/4/5 全绿且白名单已空。
- application：跨域事务唯一所有者；新增 `application.worker` 与
  `application.projects.content_objects` 边界。
- Worker：无 services、无业务 ORM 直引/查询——门禁 6 空 + 新增 worker 无 ORM 门禁，全绿。
- analysis：无 engines/services/`assembly.plan` 依赖——门禁 7 空，全绿。
- assembly：无跨模块私有导入——门禁 2 全绿且白名单已空。
- storage：只经 audit 域门面记录对象自身生命周期，不导入 audit ORM
 （门禁 8 无 storage→audit 建模）。
- 旧入口：services 全数仍有 application 层消费者，无可删模块；1.0 运行链此前已删，
  无兼容双轨、无别名回退。
- 门禁 8 跨表访问白名单与 TABLE_OWNERS 为同域持久化建模（指导文件明确保留），
  非迁移白名单。

## 最终全量测试

Docker 全量：1451 通过，1 失败——
`test_integration.py::test_full_business_chain`（有证据的既有失败：
1.0 计算链在波次开始前已删除（`bfa92b6` 前已有 `NotImplementedError`），
且 claim 未提交即回滚的租约语义新旧一致；修复=实现 0.8 计算，不属本窗口）。

## 备注

- 本窗口未实现 0.8 计算业务，未新增非必要校验/hash/完整性复核/防御分支。
- 临时指导文件 `docs/reviews/architecture-refactor-workflow.md` 按其 Wave 5 步骤 5 删除。
