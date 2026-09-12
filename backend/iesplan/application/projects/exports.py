"""项目导出用例(application/projects/exports.py, W2-C)。

Excel 报告导出编排(组合 ``services.package.export_excel``; 旧服务只读保留,
待 Wave 3 API 迁移后由协调者删除):

- 固定引用给定证据包与结果评估, 导出时不重新求解；
- 查看者可导出；标题中英双语。

``export_excel`` 为纯读用例(无 DB 写), 本层不拥有事务提交；组合调用与旧
API 路由行为一致(路由层仅在落盘导出对象后提交, 本用例不落盘)。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from iesplan.identity.contracts import UserRecord
from iesplan.services import package as package_service


def export_excel(
    db: Session,
    user: UserRecord,
    project_id: int,
    evidence_package_id: int,
    assessment_id: int,
    lang: str = "zh",
) -> bytes:
    """导出固定模板 Excel 报告(只读用例, 无事务提交)。"""
    return package_service.export_excel(
        db, user, project_id, evidence_package_id, assessment_id, lang=lang
    )
