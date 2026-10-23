# 0.14.5 C2 方案裁决

日期：2026-10-21。基线：执行分支 `15216e4` 的只读盘点。类型：本轮实施决策与证据，长期字段已提炼至manual。

采用同次主解恢复仿射事实、复用现有评价算法的方向。以下修正已入正式指导：

- 不采用application按业务键或LP命名反解税务状态、按价格系数重新gross化及重组税后CF。生成器与目标共用现金表达式并声明结构化financial_facts，Adapter恢复同次主解，application只投影和评价。
- 必需结构化财务映射及业务输出改变配套Adapter输入协议，定为generator3.4.0、Adapter3.0.0；不是因核心仿射求值可复用就维持Adapter2.0.0。通用Bundle/ComputeResult顶层/快照/配置输入未变，不整齐升版。
- financial_evaluation标识ies.financial-evaluation@1.0.0；字段唯一正文位于formats/solver-bundle.md，模块页定义职责与封存投影。
- 无适用税则数组为空，适用且零金额显式保留；不造无所有者的虚拟规则。VAT carry为扣确认退款后的期末留抵，期后到账另保留明确退款计划，非现金余额不冒充回款。
- loss_balances显式保留rule与不透明vintage_id；不能把不同税则/批次混成匿名年份余额。taxable_base为抵扣亏损前的有符号应税基础，折旧和亏损使用不再次扣现金。

C2可连续分内部映射/Adapter和封存消费两笔提交，但本轮交付必须接通真实消费者；不能只加无人消费字段。007保持，不进行D浏览器验收，不宣告版本完成。正式指导先提交后由执行者普通merge，不授权修改manual。
