# B1a 单次解析接线复审

日期2026-10-20，基线af0bc63，候选3a7ca3b。非规范证据；结论：功能反例已通过，单次解析仍未收口。

已独立读取两文件diff与新增断言。必填Profile/Effective节、payment_period拒绝schedule、手册固定路径且缺失失败均正确；已有/tmp/pies_b1a_followup_probe.py在Docker只读挂载当前源码复验6项全OK，未重跑123套件。

唯一续修：_validate_tax_rule_override调用_check_rule_override_shape后丢弃其返回的已解析值，保存的仍是原始字典；merge_effective的bound分支又调用_parse_rule_patch_values。_validate_pool_override同理把Money/PoolRefund转回字典，合并时再次_parse_pool_patch_values。不能仅按_apply函数中没有parse调用判定整条链已单次解析。

实测/tmp/pies_b1a_parse_count.py（Docker，复用pies014b1-backend:latest，--rm、无网络、只读当前源码）正常公开路径FinanceOverrides.from_dict(payload, profile=profile)后merge_effective：所得税rate的_ratio_money调用从1增至2；opening_credit的Money.from_dict同样从1增至2。没有模拟业务故障或引入生产校验。

下一步仅保留并消费输入边界已形成的不可变类型化补丁，绑定过的合并路径不得重解析；无Profile时仍在首次绑定核对权限，未覆盖值复用。修复不得改手册/放松外部检查/引入全局缓存或假想防御，也不重构成本系统。复用数值回归证据，局部验证后追加提交；通过后进入B1的提交跨输入核对/包消费者，税务求解仍归C，不要把包携带测试记成C。

## 收口复审：bf2e25e + 6e507a0

已直接检查70+/23-的接线变更。输入边界保留深冻结类型化结果，合并绑定路径直接使用；原始文本形状仅用于对外序列化，不再从该形状重复解析。复审方直接删除两段可信内部缺值时的重复KeyError包装，提交6e507a0，未改正常业务路径。

独立Docker探针/tmp/pies_b1a_acceptance.py通过：正式示例中的税率/比例共5次解析，合并后仍为5；期初余额1次，合并后仍为1；绑定/无绑定解析及合并结果相等；私有解析元数据不序列化；输入别名和嵌套不可变；Effective YAML往返；逐个必填税务节缺失拒绝；越权范围和无效确认方法仍拒绝。复用Muse 84项局部回归、3a7ca3b中123项邻接及既有真实提交测试证据，不重复跑全套。未创建镜像/卷，无Playwright。

结论：B1a复审通过，分支已提交，尚未合入master；不能宣称B1整体或0.14.5完成。下一项为B1b包内容消费者，正式契约已涵盖来源与Effective的输入边界及确认消费，无需新增指导字段或重新设计文件结构。
