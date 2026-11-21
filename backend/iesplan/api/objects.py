"""管理员对象存储状态与清理 API。"""

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from iesplan.api.auth import CurrentAdmin
from iesplan.api.deps import get_request_db
from iesplan.application import objects as objects_app

router = APIRouter(prefix="/api/admin", tags=["admin-storage"])
DbSession = Annotated[Session, Depends(get_request_db)]


class CleanupRequest(BaseModel):
    dry_run: bool = True


@router.get("/storage", summary="存储状态")
def admin_storage(db: DbSession, _admin: CurrentAdmin) -> dict:
    return objects_app.get_storage_view(db)


@router.post("/objects/cleanup", summary="预览或清理无引用对象")
def admin_cleanup(req: CleanupRequest, db: DbSession, _admin: CurrentAdmin) -> dict:
    if req.dry_run:
        return objects_app.preview_cleanup(db)
    return objects_app.execute_cleanup(db, actor_id=_admin.id)


@router.get("/storage/health", summary="存储模块健康")
def admin_storage_health(db: DbSession, _admin: CurrentAdmin) -> dict:
    return objects_app.get_storage_health(db)
