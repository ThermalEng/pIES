"""application 用例层(宪法 §4.9: 跨模块用例编排)。

当前迁移期: 既有职责仍在 ``services/``, 本包承载新增用例
(目标目录结构见宪法 §6); 每个子包以公开门面导出用例命令与结果。
"""

from __future__ import annotations
