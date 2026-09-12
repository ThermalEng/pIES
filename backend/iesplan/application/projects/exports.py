"""项目导出用例(application/projects/exports.py, W2-C)。

Excel 报告导出编排(组合 ``iesplan.package`` 领域公开门面
``export_excel``；旧服务 ``services.package`` 已删除，见纠偏 Wave 1 切片 D):

- 固定引用给定证据包与结果评估, 导出时不重新求解；
- 查看者可导出；标题中英双语。

``export_excel`` 为纯读用例(无 DB 写), 本层不拥有事务提交；组合调用与旧
API 路由行为一致(路由层仅在落盘导出对象后提交, 本用例不落盘)。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from iesplan import package as package_domain
from iesplan.identity.contracts import UserRecord


def export_excel(
    db: Session,
    user: UserRecord,
    project_id: int,
    evidence_package_id: int,
    assessment_id: int,
    lang: str = "zh",
) -> bytes:
    """导出固定模板 Excel 报告(只读用例, 无事务提交)。"""
    return package_domain.export_excel(
        db, user, project_id, evidence_package_id, assessment_id, lang=lang
    )
