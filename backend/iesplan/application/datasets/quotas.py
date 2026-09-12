"""上传配额统计薄封装用例(application/datasets)。

pIES Wave 4(api/limits.py 迁移): 配额统计的数据读取从 API 层上收至此。
ORM 只经 dataset / project 域公开门面访问, 本模块不导入
``iesplan.models``; API 层(``iesplan.api.limits``)只转发本用例,
依赖方向 api → application → 领域门面。

统计口径与原 API 层实现一致:
- 用户已用 = 其未删除项目下数据集版本文件 ``size_bytes`` 之和
  (逻辑分配量; 共享数据集 ``project_id IS NULL`` 不计入任何用户);
- 项目已用 = 该项目下同口径之和。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from iesplan import dataset as dataset_domain
from iesplan import project as project_domain
from iesplan.config import settings

__all__ = [
    "QuotaError",
    "check_upload_quota",
    "dataset_files_bytes",
    "project_ids_for_user",
]


class QuotaError(Exception):
    """上传配额超限(API 层转换为 413 标准错误信封)。"""

    def __init__(self, *, used_bytes: int, quota_bytes: int, scope: str, owner_id: int) -> None:
        super().__init__(f"上传配额超限: used={used_bytes} quota={quota_bytes}")
        self.used_bytes = used_bytes
        self.quota_bytes = quota_bytes
        self.scope = scope
        self.owner_id = owner_id


def project_ids_for_user(db: Session, user_id: int) -> list[int]:
    """用户拥有的未删除项目 id 列表(游标翻页取全, 不限单页条数)。"""
    owned: list[int] = []
    cursor: int | None = None
    while True:
        page = project_domain.list_projects(
            db, owner_id=user_id, statuses=["active", "archived"],
            limit=200, cursor=cursor,
        )
        owned.extend(item.id for item in page.items)
        cursor = page.next_cursor
        if cursor is None:
            return owned


def dataset_files_bytes(db: Session, project_ids: list[int]) -> int:
    """项目集合内数据集版本文件占用之和(dataset_files.size_bytes)。

    统计口径 = 逻辑分配量: 按 dataset_file 行累计 size_bytes(同一对象被
    多行引用时重复计列) —— 门禁目的(防重复上传刷存储)由逻辑累计即满足。
    共享数据集(project_id 为空)不计入任何项目, 与原 SQL 的
    ``Dataset.project_id IN (...)`` 口径一致(NULL 永不命中 IN)。
    """
    if not project_ids:
        return 0
    total = 0
    for project_id in project_ids:
        for ds in dataset_domain.list_datasets(db, project_id):
            if ds.project_id != project_id:
                continue
            for version in dataset_domain.list_versions(db, ds.id):
                for f in dataset_domain.list_files(db, version.id):
                    total += f.size_bytes
    return total


def check_upload_quota(
    db: Session,
    *,
    user_id: int,
    project_id: int | None = None,
    incoming_bytes: int = 0,
) -> None:
    """上传配额门禁: 用户已用(所属项目数据集文件) + 本次请求大小 > 配额即拒绝。

    只启用显式配置的配额(0 = 不限):
    - ``upload_quota_bytes``: 每用户配额(全局);
    - ``project_quota_bytes``: 每项目配额(叠加)。
    本地开发默认双 0(不限), 不误伤 e2e/本地开发。
    项目包导入无目标项目(新项目身份), 此时 project_id=None 只应用用户级配额。

    参数:
        db: 数据库会话(请求级; 只读统计, 不提交)。
        user_id: 当前用户。
        project_id: 目标项目(可为 None, 如项目包导入)。
        incoming_bytes: 本次上传数据字节数(配额判断包含本次请求, 防逐次小额
            上传逐步逼近上限; 已知大小时传入)。
    异常:
        QuotaError: 超过任一配额(API 层转换为 413)。
    """
    if settings.upload_quota_bytes <= 0 and settings.project_quota_bytes <= 0:
        return

    if settings.upload_quota_bytes > 0:
        used = dataset_files_bytes(db, project_ids_for_user(db, user_id))
        if used + incoming_bytes > settings.upload_quota_bytes:
            raise QuotaError(
                used_bytes=used, quota_bytes=settings.upload_quota_bytes,
                scope="user", owner_id=user_id,
            )
    if settings.project_quota_bytes > 0 and project_id is not None:
        project_used = dataset_files_bytes(db, [project_id])
        if project_used + incoming_bytes > settings.project_quota_bytes:
            raise QuotaError(
                used_bytes=project_used, quota_bytes=settings.project_quota_bytes,
                scope="project", owner_id=project_id,
            )
