"""项目 API 路由(U02 权限 / U03 项目写入单元, prefix /api/projects)。

认证说明: 统一使用 U01 身份单元提供的窗口会话认证
(iesplan.api.auth.CurrentUser, 校验窗口凭证; 未认证 401, 权限不足 403)。

路由清单:
- POST   /api/projects                         创建项目(创建者=所有者)
- GET    /api/projects                         我可见的项目列表(所有者+查看者)
- GET    /api/projects/{id}                    项目视图(项目+草稿摘要+版本列表)
- PUT    /api/projects/{id}/draft              草稿语义命令批量(乐观锁)
- POST   /api/projects/{id}/versions           从当前草稿创建不可变版本
- GET    /api/projects/{id}/versions           版本列表
- GET    /api/projects/{id}/versions/{vid}     版本详情
- POST   /api/projects/{id}/versions/{vid}/restore   恢复版本(新版本+新草稿)
- POST   /api/projects/{id}/apply-result       应用选定结果(参数差异补丁→新草稿+新版本)
- POST   /api/projects/{id}/archive|unarchive  归档/撤销归档
- DELETE /api/projects/{id}                    删除(须 confirm: true)
- POST   /api/projects/{id}/duplicate          复制为独立候选方案
- POST   /api/projects/{id}/transfer           转移所有权(原所有者→viewer)
- PUT    /api/projects/{id}/viewers            添加/移除查看者
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, File, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from iesplan.api.auth import CurrentUser
from iesplan.core.errors import ForbiddenError, http_error
from iesplan.db import get_db
from iesplan.services import package as package_service
from iesplan.services import project as project_service

router = APIRouter(prefix="/api/projects", tags=["projects"])


# ---------------------------------------------------------------------------
# 请求/响应模型
# ---------------------------------------------------------------------------


class ProjectCreateRequest(BaseModel):
    """创建项目请求体。"""

    name: str = Field(min_length=1, max_length=200)
    currency: Literal["CNY", "USD"] = "CNY"
    utc_offset_minutes: int = Field(default=480, ge=-720, le=840)
    description: str | None = Field(default=None, max_length=2000)


class DraftUpdateRequest(BaseModel):
    """草稿语义命令批量请求体(RPD 20.3)。"""

    expected_revision: int = Field(ge=1)
    commands: list[dict[str, Any]] = Field(default_factory=list)


class VersionCreateRequest(BaseModel):
    """创建版本请求体。"""

    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    reason: str = Field(default="manual_save", max_length=500)


class DeleteConfirmRequest(BaseModel):
    """删除确认请求体(RPD 5.3: 必须明确确认)。"""

    confirm: bool = False


class TransferRequest(BaseModel):
    """所有权转移请求体。"""

    target_user_id: int


class ViewerRequest(BaseModel):
    """查看者管理请求体。"""

    user_id: int
    action: Literal["add", "remove"]


class DuplicateRequest(BaseModel):
    """复制项目请求体(名称可选, 缺省 "<原名> 副本")。"""

    name: str | None = Field(default=None, max_length=200)


class RestoreRequest(BaseModel):
    """恢复版本请求体。"""

    name: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=2000)


class ApplyResultRequest(BaseModel):
    """应用结果请求体(RPD 20.12: 参数差异补丁 + 可选来源版本/结果标识)。"""

    diff_patch: dict[str, Any]
    version_id: int | None = None
    name: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    source_result_id: str | None = Field(default=None, max_length=200)


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------


@router.post("", status_code=201, summary="创建项目")
def create_project_endpoint(
    payload: ProjectCreateRequest,
    db: Annotated[Session, Depends(get_db)],
    user: CurrentUser,
) -> dict:
    """创建项目: 创建者即所有者, 并创建初始草稿(revision=1)。"""
    project = project_service.create_project(
        db, user,
        name=payload.name,
        currency=payload.currency,
        utc_offset_minutes=payload.utc_offset_minutes,
        description=payload.description,
    )
    db.commit()
    return {"project": project_service.project_to_dict(project), "my_role": "owner"}


@router.get("", summary="我可见的项目列表")
def list_projects_endpoint(
    db: Annotated[Session, Depends(get_db)],
    user: CurrentUser,
) -> dict:
    """我可见的项目列表(所有者 + 查看者, 不含已删除)。"""
    return {"projects": project_service.list_visible_projects(db, user)}


@router.get("/admin-visible", summary="全部项目整体视图(管理员)")
def list_all_projects_endpoint(
    db: Annotated[Session, Depends(get_db)],
    admin: CurrentUser,
) -> dict:
    """管理员整体管理入口: 全部项目(含已删除), 不含项目内容细节(草稿/版本)。

    未获项目所有者授权的项目只暴露整体管理字段(name/status/owner), 供删除管理;
    细节(草稿内容)与管理员隔离, 经 GET /projects/{id} 访问未授权项目返回 403。
    """
    if not project_service._is_admin(db, admin):
        raise ForbiddenError()
    projects = project_service.list_all_projects(db)
    return {"projects": projects}


@router.get("/{project_id}", summary="项目视图")
def get_project_endpoint(
    project_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: CurrentUser,
) -> dict:
    """项目视图: 项目 + 草稿摘要(含内容) + 版本列表。"""
    return project_service.get_project_view(db, user, project_id)


@router.put("/{project_id}/draft", summary="草稿语义命令批量")
def update_draft_endpoint(
    project_id: int,
    payload: DraftUpdateRequest,
    db: Annotated[Session, Depends(get_db)],
    user: CurrentUser,
) -> dict:
    """应用草稿语义命令(乐观锁: 预期修订不符 → 409; 整批重试幂等)。"""
    result = project_service.update_draft(
        db, user, project_id, payload.commands, payload.expected_revision
    )
    db.commit()
    return result


@router.post("/{project_id}/versions", status_code=201, summary="从当前草稿创建版本")
def create_version_endpoint(
    project_id: int,
    payload: VersionCreateRequest,
    db: Annotated[Session, Depends(get_db)],
    user: CurrentUser,
) -> dict:
    """从当前草稿创建不可变项目版本。"""
    version = project_service.create_version(
        db, user, project_id, payload.name, payload.description, payload.reason
    )
    db.commit()
    return {"version": project_service.version_to_dict(version)}


@router.get("/{project_id}/versions", summary="版本列表")
def list_versions_endpoint(
    project_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: CurrentUser,
) -> dict:
    """版本列表(新版本在前)。"""
    project_service.ensure_access(db, user, project_id, "view")
    versions = project_service.list_versions(db, project_id)
    return {"versions": [project_service.version_to_dict(v) for v in versions]}


@router.get("/{project_id}/versions/{version_id}", summary="版本详情")
def get_version_endpoint(
    project_id: int,
    version_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: CurrentUser,
) -> dict:
    """版本详情。"""
    project_service.ensure_access(db, user, project_id, "view")
    version = project_service.get_version(db, project_id, version_id)
    return {"version": project_service.version_to_dict(version)}


@router.post("/{project_id}/versions/{version_id}/restore", summary="恢复历史版本")
def restore_version_endpoint(
    project_id: int,
    version_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: CurrentUser,
    payload: RestoreRequest | None = None,
) -> dict:
    """恢复历史版本: 创建新版本 + 新草稿, 不倒写历史(REQ-PROJ-002)。"""
    result = project_service.restore_version(
        db, user, project_id, version_id,
        name=payload.name if payload else None,
        description=payload.description if payload else None,
    )
    db.commit()
    return result


@router.post("/{project_id}/apply-result", summary="应用选定规划结果")
def apply_result_endpoint(
    project_id: int,
    payload: ApplyResultRequest,
    db: Annotated[Session, Depends(get_db)],
    user: CurrentUser,
) -> dict:
    """应用选定结果: 参数差异补丁应用到新草稿并创建新版本, 来源版本不变。"""
    result = project_service.apply_result(
        db, user, project_id, payload.diff_patch,
        version_id=payload.version_id,
        name=payload.name,
        description=payload.description,
        source_result_id=payload.source_result_id,
    )
    db.commit()
    return result


@router.post("/{project_id}/archive", summary="归档项目")
def archive_project_endpoint(
    project_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: CurrentUser,
) -> dict:
    """归档项目(归档后只读, 不能编辑/提交计算)。"""
    project = project_service.archive_project(db, user, project_id)
    db.commit()
    return {
        "project": project_service.project_to_dict(project),
        "my_role": project_service.get_role(db, user, project_id),
    }


@router.post("/{project_id}/unarchive", summary="撤销归档")
def unarchive_project_endpoint(
    project_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: CurrentUser,
) -> dict:
    """撤销归档(恢复为 active)。"""
    project = project_service.unarchive_project(db, user, project_id)
    db.commit()
    return {
        "project": project_service.project_to_dict(project),
        "my_role": project_service.get_role(db, user, project_id),
    }


class AdminAccessRequest(BaseModel):
    """管理员访问授权切换请求体(仅所有者)。"""

    enabled: bool


@router.put("/{project_id}/admin-access", summary="切换管理员访问授权(所有者)")
def set_admin_access_endpoint(
    project_id: int,
    payload: AdminAccessRequest,
    db: Annotated[Session, Depends(get_db)],
    user: CurrentUser,
) -> dict:
    """所有者授权管理员查看项目细节并转移所有权; 关闭后管理员与项目细节隔离。"""
    project = project_service.set_admin_access(db, user, project_id, payload.enabled)
    db.commit()
    return {
        "project": project_service.project_to_dict(project),
        "my_role": project_service.get_role(db, user, project_id),
    }


@router.delete("/{project_id}", status_code=204, summary="删除项目")
def delete_project_endpoint(
    project_id: int,
    payload: DeleteConfirmRequest,
    db: Annotated[Session, Depends(get_db)],
    user: CurrentUser,
) -> None:
    """删除项目(RPD 5.3): 必须携带 {"confirm": true} 显式确认。"""
    project_service.delete_project(db, user, project_id, confirm=payload.confirm)
    db.commit()


@router.post("/{project_id}/duplicate", status_code=201, summary="复制为独立候选方案")
def duplicate_project_endpoint(
    project_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: CurrentUser,
    payload: DuplicateRequest | None = None,
) -> dict:
    """复制项目为独立候选方案(复制者成为新项目所有者)。"""
    project = project_service.duplicate_project(
        db, user, project_id, name=payload.name if payload else None
    )
    db.commit()
    return {"project": project_service.project_to_dict(project), "my_role": "owner"}


@router.post("/{project_id}/transfer", summary="转移项目所有权")
def transfer_ownership_endpoint(
    project_id: int,
    payload: TransferRequest,
    db: Annotated[Session, Depends(get_db)],
    user: CurrentUser,
) -> dict:
    """转移所有权: 原所有者默认成为查看者(RPD 3.2)。"""
    project = project_service.transfer_ownership(db, user, project_id, payload.target_user_id)
    db.commit()
    return {
        "project": project_service.project_to_dict(project),
        "my_role": project_service.get_role(db, user, project_id),
    }


@router.put("/{project_id}/viewers", summary="添加/移除查看者")
def viewers_endpoint(
    project_id: int,
    payload: ViewerRequest,
    db: Annotated[Session, Depends(get_db)],
    user: CurrentUser,
) -> dict:
    """添加/移除查看者(仅所有者), 返回当前有效成员清单。"""
    if payload.action == "add":
        project_service.add_viewer(db, user, project_id, payload.user_id)
    else:
        project_service.remove_viewer(db, user, project_id, payload.user_id)
    db.commit()
    return {"members": project_service.list_members(db, project_id)}


@router.get("/{project_id}/viewers", summary="查看者/成员清单")
def list_viewers_endpoint(
    project_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: CurrentUser,
) -> dict:
    """当前有效成员清单(所有者/查看者; 需项目 view 权限)。"""
    project_service.ensure_access(db, user, project_id, "view")
    return {"members": project_service.list_members(db, project_id)}


# ---------------------------------------------------------------------------
# 项目包导入(U14/RPD 6: 校验提案 → 确认导入, 每次导入新项目身份)
# ---------------------------------------------------------------------------


@router.post("/import", status_code=201, summary="导入项目包(创建导入提案)")
def import_package_endpoint(
    db: Annotated[Session, Depends(get_db)],
    user: CurrentUser,
    file: Annotated[UploadFile, File(description="项目包 zip")],
    idempotency_key: str | None = None,
) -> dict:
    """上传项目包并创建导入提案(校验 → 暂存对象 → 拟创建项目快照)。

    大小门禁(H-07): 以 (上限+1) 字节封顶流式读取, 超限立即拒绝
    (压缩包字节上限 MAX_PACKAGE_BYTES, 与 Nginx client_max_body_size 对齐),
    完整解压前的条目/单文件/总解压大小预检在 services/package._parse_package。
    相同源文件同一提议人幂等返回既有提案; 校验失败 400 + 校验报告。
    """
    # 封顶流式读取: 最多读 (上限+1) 字节, 超出即拒绝(内存占用有界)
    data = file.file.read(package_service.MAX_PACKAGE_BYTES + 1)
    if not data:
        raise http_error(400, "API-REQ-001", "ies.error.empty_file", filename=file.filename or "")
    if len(data) > package_service.MAX_PACKAGE_BYTES:
        raise http_error(
            413, "PKG-SIZE-001", "ies.diag.pkg.too_large",
            reason="package_too_large", max_bytes=package_service.MAX_PACKAGE_BYTES,
        )
    proposal = package_service.import_proposal(
        db, user, data, idempotency_key=idempotency_key
    )
    db.commit()
    return {"proposal": _proposal_to_dict(proposal)}


@router.post("/import/{proposal_id}/confirm", status_code=201, summary="确认导入提案")
def confirm_import_endpoint(
    proposal_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: CurrentUser,
) -> dict:
    """确认导入: 创建新项目身份(导入者即所有者), 历史结果作为证据来源保留。"""
    project = package_service.confirm_import(db, user, proposal_id)
    db.commit()
    return {
        "project": project_service.project_to_dict(project),
        "my_role": project_service.get_role(db, user, project_id=project.id),
    }


def _proposal_to_dict(proposal) -> dict:
    """导入提案序列化(API 展示)。"""
    return {
        "id": proposal.id,
        "project_id": proposal.project_id,
        "proposer_id": proposal.proposer_id,
        "status": proposal.status,
        "source_hash": proposal.source_hash,
        "review_summary": proposal.review_summary,
        "review_errors": proposal.review_errors,
        "created_at": proposal.created_at.isoformat() if proposal.created_at else None,
    }
