# B1a 追加修正复审与手册挂载核查

日期：2026-10-20；基线0dc1830，候选af0bc63；结论：暂不合并。非规范审查记录。

## 手册问题实证

backend/Dockerfile以backend为构建上下文，仅COPY后端代码、tests和deploy，不包含仓库根manual。终端原文是“手册在镜像外，我用只读挂载供给测试”，没有Docker拒绝bind mount的证据。正确目录为manual，不是mannual。

独立Docker复现同一test_manual_examples_parse_and_merge：只读挂载backend到/app、不挂manual时1 skipped；追加当前工作树manual到/manual:ro时1 passed。工作树manual与主线2f73da4无差异。这不是指导版本丢失或目录未同步；最终报告的带挂载命令能够真实执行该测试。

但新增源码树搜索与pytest.skip会掩盖日后的测试挂载遗漏。该测试应以明确手册路径运行，资料不存在就失败，不应跳过或搜索另一份手册。无需改变生产后端镜像来打包整份手册。

## 代码复审

确认已修：真实正式维护覆盖示例可合并；维护嵌套事实不可改；无Profile解析后禁改所得税范围得到拒绝；死TaxRefCtx已删除；真实提交入口测试加入非空拒绝与空规则201对照。

独立最小Docker探针/tmp/pies_b1a_followup_probe.py实测残留：

1. Profile/Effective的tax_rules、credit_pools从必填改成可省略，遗漏两节仍接受；违背正式格式。Overrides稀疏节可省略不等于前两者可省略。新增test_missing_tax_sections_default_to_empty错误锁定了该回退。
2. payment_period规则可接受Overrides.confirmation.schedule，合并后对象内有schedule但to_dict不输出，用户声明被静默丢弃。应仅允许explicit_schedule规则覆盖已有schedule，payment_period公开覆盖边界明确拒绝。
3. 虽删全规则重解析，补丁仍在_check_rule_override_shape和_apply_rule_patch/_apply_pool_patch两处重复解析比例/Money/整表。应复用边界已经形成的类型化值，而不是仅换一个助手名称继续双解析。不要求重构旧成本分量。

只复现以上局部场景，没有重跑120项或全仓；复用现有镜像、--rm、无网络、只读挂载、无新镜像/卷、无Playwright。正式指南不修改以迁就错误实现。
