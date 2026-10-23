# 内部开发、Review 与历史归档

`docs/` 不承载正式产品说明、稳定公共契约或 Roadmap；这些内容统一位于 [`manual/`](../manual/README.md)。`docs/development/` 只保存面向仓库内部 AI/开发者的实施交接与过程控制，效力低于架构宪法和 `manual/` 的生效规范，也不进入帮助中心。已完成、被替代或与现行规范冲突的材料必须在同一次变更中归档，不能继续留在现行入口。

## Development

- [模型与算法实施交接清单](development/model-and-algorithm-ai-requirements.md)：任务范围、权威链接、验证与归档要求的模板；
- [开发、测试与贡献流程](development/development-workflow.md)：内部开发与文档维护流程；
- [版本化与发布流程](development/versioning-and-release-workflow.md)：内部发布操作与版本传播流程。

这些材料不得重复定义产品字段、HTTP API、数据库表、算法参数或架构边界；长期规则必须回到 `manual/` 对应章节。

## Review

[`reviews/`](reviews/) 保存审查快照和整改证据：

- [2026-08-20 架构专题审查](reviews/2026-08-20-architecture/05-architecture-overview.md)；
- [2026-08-20 前后端解耦审查](reviews/fullstack-decoupling-review-2026-08-20.md)；
- [2026-08-21 整改复审](reviews/fullstack-decoupling-rereview-2026-08-21.md)；
- [安全红队审查](reviews/codex-redteam-report.md)；
- [二次审查](reviews/codex-review-round2.md)；
- [人工审查意见](reviews/manual-review-2026-08-20.txt)。

Review 只代表审查日期与当时基线，不能自动证明当前实现状态，也不能覆盖架构宪法和开发者指南。后续 review 应注明日期、对象、提交基线、结论和验证证据。

## Archive

[`archive/`](archive/README.md) 保存已被替代的规格、合同、路线图、设计输入、品牌提案和历史工作流。归档内容停止维护，内部链接和结论可能过时；不得作为新开发输入。

返回[项目入口](../README.md)。
