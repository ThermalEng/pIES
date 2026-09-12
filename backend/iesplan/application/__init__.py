"""application 用例层(宪法 §4.9: 跨模块用例编排)。

旧 ``services/`` 已删除; 本包拥有全部用例的事务、步骤顺序与跨领域协调
(目标目录结构见宪法 §6); 每个子包以公开门面导出用例命令与结果。
"""

from __future__ import annotations
