"""模型域 repository SQL 实现（用户模型模板/项目模型表，归属 model）。

项目建模事实只在项目草稿内容 model 节（归属 project 域），本模块不再拥有
系统图/设备/端口/连接表。

实现规则：
- 查询、写入、flush 由本模块完成；绝不 commit/rollback；
- 唯一冲突（模板稳定 ID/用户 slug 重复等）转为 ModelConflictError，
  由调用方决定重试或拒绝。
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    Text,
    UniqueConstraint,
    delete,
    select,
    text,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column

from iesplan.db import (
    Base,
    JSONB,
    bigint_pk,
)

from iesplan.model.contracts import (
    ModelConflictError,
    ModelNotFoundError,
    ModelTemplateRecord,
    ModelTemplateRevisionRecord,
    ProjectModelRecord,
    TemplateDraftRevisionRecord,
)



def _iso(value: datetime | None) -> str | None:
    """ORM 时间 → 记录字符串（原样 isoformat，不增减时区后缀）。"""
    return value.isoformat() if value is not None else None




# ---------------------------------------------------------------------------
# 模板主表
# ---------------------------------------------------------------------------


def _row_to_template(row: ModelTemplate) -> ModelTemplateRecord:
    return ModelTemplateRecord(
        id=row.id,
        template_id=row.template_id,
        owner_id=row.owner_id,
        status=row.status,
        description=row.description,
        slug=row.slug,
        public_namespace=row.public_namespace,
        draft_yaml_object_id=row.draft_yaml_object_id,
        draft_diagnostics_object_id=row.draft_diagnostics_object_id,
        draft_has_inputs=row.draft_has_inputs,
        draft_revision=row.draft_revision,
        draft_updated_at=_iso(row.draft_updated_at),
        current_draft_revision_id=row.current_draft_revision_id,
        current_published_revision_id=row.current_published_revision_id,
        published_revision=row.published_revision,
        published_at=_iso(row.published_at),
        created_at=_iso(row.created_at),
        updated_at=_iso(row.updated_at),
    )


def _row_to_template_revision(row: ModelTemplateRevision) -> ModelTemplateRevisionRecord:
    return ModelTemplateRevisionRecord(
        id=row.id,
        template_id=row.template_id,
        revision=row.revision,
        schema_version=row.schema_version,
        input_count=row.input_count,
        diagnostics_object_id=row.diagnostics_object_id,
        idempotency_key=row.idempotency_key,
        yaml_object_id=row.yaml_object_id,
        receipt_object_id=row.receipt_object_id,
        summary_object_id=row.summary_object_id,
        published_by=row.published_by,
        published_at=_iso(row.published_at),
    )


def _row_to_draft_revision(row: ModelTemplateDraftRevision) -> TemplateDraftRevisionRecord:
    return TemplateDraftRevisionRecord(
        id=row.id,
        entry_id=row.entry_id,
        revision=row.revision,
        yaml_object_id=row.yaml_object_id,
        source=row.source,
        created_by=row.created_by,
        created_at=_iso(row.created_at),
        diagnostics_object_id=row.diagnostics_object_id,
    )


def _row_to_project_model(row: ProjectModel) -> ProjectModelRecord:
    return ProjectModelRecord(
        id=row.id,
        project_id=row.project_id,
        suffix=row.suffix,
        base_device_id=row.base_device_id,
        device_id=row.device_id,
        revision=row.revision,
        project_revision=row.project_revision,
        model_object_id=row.model_object_id,
        receipt_object_id=row.receipt_object_id,
        source=row.source,
        template_id=row.template_id,
        template_revision=row.template_revision,
        idempotency_key=row.idempotency_key,
        created_by=row.created_by,
        created_at=_iso(row.created_at),
    )


def get_owned_template(db: Session, owner_id: int, template_id: str) -> ModelTemplateRecord | None:
    """按稳定模板 ID 取当前用户的模板；无/非属返回 None。"""
    row = db.execute(
        select(ModelTemplate).where(
            ModelTemplate.owner_id == owner_id,
            ModelTemplate.template_id == template_id,
        )
    ).scalar_one_or_none()
    return _row_to_template(row) if row is not None else None


def list_owned_templates(db: Session, owner_id: int) -> list[ModelTemplateRecord]:
    """当前用户模板（更新时间降序，id 降序）。"""
    rows = (
        db.execute(
            select(ModelTemplate)
            .where(ModelTemplate.owner_id == owner_id)
            .order_by(ModelTemplate.updated_at.desc(), ModelTemplate.id.desc())
        )
        .scalars()
        .all()
    )
    return [_row_to_template(row) for row in rows]


def list_owned_templates_by_status(
    db: Session, owner_id: int, status: str
) -> list[ModelTemplateRecord]:
    """当前用户指定状态模板（更新时间降序，id 降序）。"""
    rows = (
        db.execute(
            select(ModelTemplate)
            .where(ModelTemplate.owner_id == owner_id, ModelTemplate.status == status)
            .order_by(ModelTemplate.updated_at.desc(), ModelTemplate.id.desc())
        )
        .scalars()
        .all()
    )
    return [_row_to_template(row) for row in rows]


def create_template(
    db: Session,
    *,
    template_id: str,
    slug: str,
    public_namespace: str,
    owner_id: int,
    status: str,
    description: str | None,
    draft_yaml_object_id: int,
    draft_has_inputs: bool,
    draft_revision: int,
    draft_updated_at: Any,
) -> ModelTemplateRecord:
    """创建模板主表行；稳定 ID/用户 slug 冲突抛 ModelConflictError。"""
    row = ModelTemplate(
        template_id=template_id,
        slug=slug,
        public_namespace=public_namespace,
        owner_id=owner_id,
        status=status,
        description=description,
        draft_yaml_object_id=draft_yaml_object_id,
        draft_has_inputs=draft_has_inputs,
        draft_revision=draft_revision,
        draft_updated_at=draft_updated_at,
        published_revision=0,
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError as exc:
        raise ModelConflictError(
            "模板已存在(同一用户模板 ID 唯一)",
            params={"template_id": template_id},
        ) from exc
    return _row_to_template(row)


def update_template(db: Session, template_row_id: int, **fields: Any) -> ModelTemplateRecord:
    """更新模板主表字段（仅提供字段）并返回新视图；缺失抛 ModelNotFoundError。"""
    row = db.get(ModelTemplate, template_row_id)
    if row is None:
        raise ModelNotFoundError("模板不存在", params={"template_row_id": template_row_id})
    for name, value in fields.items():
        setattr(row, name, value)
    db.flush()
    return _row_to_template(row)


def delete_template(db: Session, template_row_id: int) -> None:
    """硬删除模板主表行；缺失抛 ModelNotFoundError。"""
    row = db.get(ModelTemplate, template_row_id)
    if row is None:
        raise ModelNotFoundError("模板不存在", params={"template_row_id": template_row_id})
    db.delete(row)
    db.flush()


# ---------------------------------------------------------------------------
# 模板草稿/发布 revision
# ---------------------------------------------------------------------------


def create_draft_revision(
    db: Session,
    *,
    entry_id: int,
    revision: int,
    yaml_object_id: int,
    source: str,
    created_by: int,
    diagnostics_object_id: int | None,
) -> TemplateDraftRevisionRecord:
    """新增不可变草稿 revision 行；冲突抛 ModelConflictError。"""
    row = ModelTemplateDraftRevision(
        entry_id=entry_id,
        revision=revision,
        yaml_object_id=yaml_object_id,
        source=source,
        created_by=created_by,
        diagnostics_object_id=diagnostics_object_id,
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError as exc:
        raise ModelConflictError(
            "草稿 revision 冲突",
            params={"entry_id": entry_id, "revision": revision},
        ) from exc
    return _row_to_draft_revision(row)


def list_draft_revisions(db: Session, entry_id: int) -> list[TemplateDraftRevisionRecord]:
    """模板草稿 revision 历史（revision 升序）。"""
    rows = (
        db.execute(
            select(ModelTemplateDraftRevision)
            .where(ModelTemplateDraftRevision.entry_id == entry_id)
            .order_by(ModelTemplateDraftRevision.revision)
        )
        .scalars()
        .all()
    )
    return [_row_to_draft_revision(row) for row in rows]


def get_draft_revision(
    db: Session, entry_id: int, revision: int
) -> TemplateDraftRevisionRecord | None:
    """取精确草稿 revision；无返回 None。"""
    row = db.execute(
        select(ModelTemplateDraftRevision).where(
            ModelTemplateDraftRevision.entry_id == entry_id,
            ModelTemplateDraftRevision.revision == revision,
        )
    ).scalar_one_or_none()
    return _row_to_draft_revision(row) if row is not None else None


def get_published_revision(
    db: Session, template_row_id: int, revision: int
) -> ModelTemplateRevisionRecord | None:
    """取精确发布 revision；无返回 None。"""
    row = db.execute(
        select(ModelTemplateRevision).where(
            ModelTemplateRevision.template_id == template_row_id,
            ModelTemplateRevision.revision == revision,
        )
    ).scalar_one_or_none()
    return _row_to_template_revision(row) if row is not None else None


def find_revision_by_idempotency(
    db: Session, template_row_id: int, idempotency_key: str
) -> ModelTemplateRevisionRecord | None:
    """按幂等键取发布 revision（重放用）；无返回 None。"""
    row = db.execute(
        select(ModelTemplateRevision).where(
            ModelTemplateRevision.template_id == template_row_id,
            ModelTemplateRevision.idempotency_key == idempotency_key,
        )
    ).scalar_one_or_none()
    return _row_to_template_revision(row) if row is not None else None


def create_published_revision(
    db: Session,
    *,
    template_row_id: int,
    revision: int,
    schema_version: str,
    input_count: int,
    yaml_object_id: int,
    receipt_object_id: int,
    summary_object_id: int,
    diagnostics_object_id: int | None,
    idempotency_key: str | None,
    published_by: int,
) -> ModelTemplateRevisionRecord:
    """新增不可变发布 revision 行；冲突抛 ModelConflictError。"""
    row = ModelTemplateRevision(
        template_id=template_row_id,
        revision=revision,
        schema_version=schema_version,
        input_count=input_count,
        yaml_object_id=yaml_object_id,
        receipt_object_id=receipt_object_id,
        summary_object_id=summary_object_id,
        diagnostics_object_id=diagnostics_object_id,
        idempotency_key=idempotency_key,
        published_by=published_by,
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError as exc:
        raise ModelConflictError(
            "模板发布冲突(并发)",
            params={"template_row_id": template_row_id, "revision": revision},
        ) from exc
    return _row_to_template_revision(row)


# ---------------------------------------------------------------------------
# 项目模型清单
# ---------------------------------------------------------------------------


def get_project_model(db: Session, model_id: int) -> ProjectModelRecord | None:
    """按主键取清单行；不存在返回 None。"""
    row = db.get(ProjectModel, model_id)
    return _row_to_project_model(row) if row is not None else None


def find_project_model_by_idempotency(
    db: Session, project_id: int, idempotency_key: str
) -> ProjectModelRecord | None:
    """按幂等键取清单行（重放用）；无返回 None。"""
    row = db.execute(
        select(ProjectModel).where(
            ProjectModel.project_id == project_id,
            ProjectModel.idempotency_key == idempotency_key,
        )
    ).scalar_one_or_none()
    return _row_to_project_model(row) if row is not None else None


def create_project_model(
    db: Session,
    *,
    project_id: int,
    suffix: int,
    base_device_id: str,
    device_id: str,
    project_revision: int,
    model_object_id: int,
    receipt_object_id: int,
    source: str,
    template_id: str | None,
    template_revision: int | None,
    idempotency_key: str | None,
    created_by: int,
) -> ProjectModelRecord:
    """新增清单行；编号/最终 ID 冲突抛 ModelConflictError。"""
    row = ProjectModel(
        project_id=project_id,
        suffix=suffix,
        base_device_id=base_device_id,
        device_id=device_id,
        revision=1,
        project_revision=project_revision,
        model_object_id=model_object_id,
        receipt_object_id=receipt_object_id,
        source=source,
        template_id=template_id,
        template_revision=template_revision,
        idempotency_key=idempotency_key,
        created_by=created_by,
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError as exc:
        raise ModelConflictError(
            "项目模型保存冲突(编号或最终 ID 唯一性)",
            params={"project_id": project_id, "device_id": device_id},
        ) from exc
    return _row_to_project_model(row)


def update_project_model(db: Session, model_id: int, **fields: Any) -> ProjectModelRecord:
    """更新清单行字段（仅提供字段）并返回新视图；缺失抛 ModelNotFoundError。"""
    row = db.get(ProjectModel, model_id)
    if row is None:
        raise ModelNotFoundError("项目模型不存在", params={"model_id": model_id})
    for name, value in fields.items():
        setattr(row, name, value)
    db.flush()
    return _row_to_project_model(row)


def delete_project_model(db: Session, model_id: int) -> ProjectModelRecord:
    """硬删除清单行并返回删除前视图；缺失抛 ModelNotFoundError。"""
    row = db.get(ProjectModel, model_id)
    if row is None:
        raise ModelNotFoundError("项目模型不存在", params={"model_id": model_id})
    snapshot = _row_to_project_model(row)
    db.delete(row)
    db.flush()
    return snapshot


def list_project_models(
    db: Session, project_id: int, *, newest_first: bool = False
) -> list[ProjectModelRecord]:
    """项目清单行（默认编号升序；newest_first 时编号降序）。"""
    order = (
        (ProjectModel.suffix.desc(), ProjectModel.id.desc())
        if newest_first
        else (ProjectModel.suffix, ProjectModel.id)
    )
    rows = (
        db.execute(
            select(ProjectModel).where(ProjectModel.project_id == project_id).order_by(*order)
        )
        .scalars()
        .all()
    )
    return [_row_to_project_model(row) for row in rows]


def allocate_project_model_suffix(db: Session, project_id: int) -> int:
    """项目内分配下一个 _N 编号（只递增、删除不复用）。

    ``UPDATE ... RETURNING`` 在数据库内原子完成（PostgreSQL 行锁 +
    SQLite 写锁串行化）；计数器行缺失时以 savepoint 插入并重查；
    重试耗尽抛 ModelConflictError（即 ConflictError，由调用方决定重试）。
    """

    for _attempt in range(3):
        row = db.execute(
            text(
                "UPDATE project_model_sequences SET next_suffix = next_suffix + 1 "
                "WHERE project_id = :pid RETURNING next_suffix - 1"
            ),
            {"pid": project_id},
        ).first()
        if row is not None:
            return int(row[0])
        try:
            with db.begin_nested():  # 只回滚嵌套 savepoint, 不触调用方外层事务
                db.execute(
                    text(
                        "INSERT INTO project_model_sequences (project_id, next_suffix) "
                        "VALUES (:pid, 2)"
                    ),
                    {"pid": project_id},
                )
                db.flush()
            return 1
        except IntegrityError:
            continue  # 并发竞争者已插入计数器行: 下一轮 UPDATE 原子重试
    raise ModelConflictError(
        "项目模型编号分配失败(并发冲突), 请重试",
        params={"project_id": project_id},
        location={"object_type": "project_model", "project_id": str(project_id)},
    )


# ORM 表定义: Wave2A 由 iesplan.models.model_template 迁入, 表真相归本域所有。
# ---------------------------------------------------------------------------

class ModelTemplate(Base):
    """用户模型模板主表(草稿区 + 生命周期状态)。

    ``template_id`` 为稳定公开 ID(模板 YAML 声明的 ``device.id``), 同一
    用户内唯一; 创建后不可变更。``draft_*`` 列保存未发布的草稿内容
    (对象引用 + 规范摘要 + 乐观锁 revision); ``published_revision`` 为
    最新已发布 revision(0 表示尚未发布)。
    """

    __tablename__ = "model_templates"

    id: Mapped[int] = bigint_pk()
    #: 稳定模板 ID(命名空间字符串, 同一用户内唯一; 不可变更)
    template_id: Mapped[str] = mapped_column(Text, nullable=False)
    #: 所有者(只有所有者可见/可编辑自己的模板)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    #: 生命周期状态: draft(未发布) / published(已发布且启用) / disabled(已发布但停用)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa.text("'draft'"))
    #: 简短说明(用户自述, 不参与引用与计算)
    description: Mapped[str | None] = mapped_column(Text)
    #: 客户端提交的 slug（与稳定 ID 的 slug 部分一致）
    slug: Mapped[str | None] = mapped_column(Text)
    #: 公开命名空间快照（分配时命名空间，终身不变）
    public_namespace: Mapped[str | None] = mapped_column(Text)
    #: 草稿 YAML 对象引用(objects.id; 无草稿时 NULL) — 兼容字段，权威为 draft_revisions
    draft_yaml_object_id: Mapped[int | None] = mapped_column(ForeignKey("objects.id"))
    #: 草稿最近一次校验的聚合诊断 JSON 对象引用(objects.id; 无草稿时 NULL)
    draft_diagnostics_object_id: Mapped[int | None] = mapped_column(ForeignKey("objects.id"))
    #: 草稿内容是否声明顶层 inputs(列表/表单生成依据)
    draft_has_inputs: Mapped[bool | None] = mapped_column(sa.Boolean)
    #: 草稿乐观锁修订(每次保存草稿 +1; 并发编辑以 expected_revision 拒绝)
    draft_revision: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=sa.text("0"))
    draft_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: 当前草稿 revision 行指针（不可变历史）
    current_draft_revision_id: Mapped[int | None] = mapped_column(ForeignKey("model_template_draft_revisions.id"))
    #: 最新发布 revision 行指针
    current_published_revision_id: Mapped[int | None] = mapped_column(ForeignKey("model_template_revisions.id"))
    #: 最新已发布 revision(0 = 尚未发布)
    published_revision: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=sa.text("0"))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )

    __table_args__ = (
        CheckConstraint("status IN ('draft','published','disabled')", name="ck_model_templates_status"),
        CheckConstraint("draft_revision >= 0", name="ck_model_templates_draft_revision"),
        CheckConstraint("published_revision >= 0", name="ck_model_templates_published_revision"),
        #: 稳定 ID 全局唯一（新命名空间全局唯一）
        UniqueConstraint("template_id", name="uq_model_templates_template_id"),
        #: 同一用户 slug 唯一（避免重复）
        UniqueConstraint("owner_id", "slug", name="uq_model_templates_owner_slug"),
        Index("idx_model_templates_owner", "owner_id"),
    )

class ModelTemplateRevision(Base):
    """不可变模板发布 revision(每次发布一行; 永不修改或删除)。

    相同规范内容幂等返回同一 revision
    兜底并发)。``yaml_object_id`` 保存规范 YAML 字节, ``receipt_object_id``
    保存校验回执, ``summary_object_id`` 保存结构摘要 JSON。
    """

    __tablename__ = "model_template_revisions"

    id: Mapped[int] = bigint_pk()
    #: 模板主表行(模板删除草稿时 revision 不删除; 引用保留)
    template_id: Mapped[int] = mapped_column(ForeignKey("model_templates.id"), nullable=False)
    #: 单调递增发布序号(从 1 开始; 同模板内唯一)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    schema_version: Mapped[str] = mapped_column(Text, nullable=False)
    #: 顶层 inputs 叶子数量(表单生成规模提示; 无 inputs 为 0)
    input_count: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=sa.text("0"))
    #: 校验诊断 JSON 对象引用(objects.id; 发布前最后校验的聚合诊断)
    diagnostics_object_id: Mapped[int | None] = mapped_column(ForeignKey("objects.id"))
    #: 幂等键(发布重复提交返回同一逻辑结果; 同模板内唯一)
    idempotency_key: Mapped[str | None] = mapped_column(Text)
    #: 规范 YAML 对象引用(objects.id)
    yaml_object_id: Mapped[int] = mapped_column(ForeignKey("objects.id"), nullable=False)
    #: 校验回执对象引用(objects.id)
    receipt_object_id: Mapped[int] = mapped_column(ForeignKey("objects.id"), nullable=False)
    #: 结构摘要 JSON 对象引用(objects.id)
    summary_object_id: Mapped[int] = mapped_column(ForeignKey("objects.id"), nullable=False)
    published_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )

    __table_args__ = (
        CheckConstraint("revision >= 1", name="ck_model_template_revisions_revision"),
        CheckConstraint("input_count >= 0", name="ck_model_template_revisions_input_count"),

        UniqueConstraint("template_id", "revision", name="uq_model_template_revisions_revision"),
        #: 同内容幂等(重复发布相同规范内容返回同一 revision)

        Index("idx_mtr_template", "template_id"),
        Index("idx_mtr_idem_key", "template_id", "idempotency_key"),
    )


# ---------------------------------------------------------------------------
# ORM 表定义: Wave2A 由 iesplan.models.draft_revision 迁入, 表真相归本域所有。
# ---------------------------------------------------------------------------

class ModelTemplateDraftRevision(Base):
    """不可变草稿 revision（每次保存草稿新增一行，永不覆盖）。

    - entry_id：模板主表行
    - revision：严格递增（1 开始）
    - yaml_object_id：规范 YAML 对象引用（按对象 id 寻址）
    - 规范文本与回执
    - source：创建来源（form/yaml_editor/upload/derived/migration）
    - created_by/created_at：创建者与时间
    - diagnostics_object_id：校验报告/诊断对象引用
    """

    __tablename__ = "model_template_draft_revisions"

    id: Mapped[int] = bigint_pk()
    entry_id: Mapped[int] = mapped_column(ForeignKey("model_templates.id"), nullable=False)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    yaml_object_id: Mapped[int] = mapped_column(ForeignKey("objects.id"), nullable=False)

    source: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    diagnostics_object_id: Mapped[int | None] = mapped_column(ForeignKey("objects.id"))

    __table_args__ = (
        CheckConstraint("revision >= 1", name="ck_mtdr_revision"),

        CheckConstraint(
            "source IN ('form','yaml_editor','upload','derived','migration')",
            name="ck_mtdr_source",
        ),
        UniqueConstraint("entry_id", "revision", name="uq_mtdr_entry_revision"),
        Index("idx_mtdr_entry", "entry_id"),
    )


# ---------------------------------------------------------------------------
# ORM 表定义: Wave2A 由 iesplan.models.project_model 迁入, 表真相归本域所有。
# ---------------------------------------------------------------------------

class ProjectModel(Base):
    """项目模型清单表(项目内每个已保存模型实例一行)。

    device_id 为最终带 ``_N`` 后缀的 ID(与模型 YAML 文件一致);
    receipt_object_id 指向校验回执 JSON 对象。文本文件只校验字头。
    """

    __tablename__ = "project_models"

    id: Mapped[int] = bigint_pk()
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False)
    #: 项目内编号(_1、_2……; 只递增, 删除不复用)
    suffix: Mapped[int] = mapped_column(BigInteger, nullable=False)
    #: 基础设备 ID(无后缀, 如 acme.device.heat_pump)
    base_device_id: Mapped[str] = mapped_column(Text, nullable=False)
    #: 最终设备 ID(带后缀, 如 acme.device.heat_pump_1)
    device_id: Mapped[str] = mapped_column(Text, nullable=False)
    #: 清单修订号(模型实例被重新保存时递增; 本切片恒为 1)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=sa.text("1"))
    #: 本次保存产生的项目草稿 revision（用于幂等重放返回同一结果）
    project_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    #: 模型规范 YAML/JSON 文件对象引用(objects.id)
    model_object_id: Mapped[int] = mapped_column(ForeignKey("objects.id"), nullable=False)
    #: 校验回执对象引用(objects.id)
    receipt_object_id: Mapped[int] = mapped_column(ForeignKey("objects.id"), nullable=False)
    #: 来源(direct_yaml | template)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    #: 模板稳定 ID(模板来源时非空; 固定精确 revision 解释历史项目模型)
    template_id: Mapped[str | None] = mapped_column(Text)
    #: 模板发布 revision(模板来源时非空; 模板更新不影响已保存项目模型)
    template_revision: Mapped[int | None] = mapped_column(BigInteger)
    #: 模板追溯: 模板来源时非空
    #: 幂等键(项目内唯一, 重试返回同一逻辑结果)
    idempotency_key: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )

    __table_args__ = (
        CheckConstraint("suffix >= 1", name="ck_project_models_suffix"),
        CheckConstraint("revision >= 1", name="ck_project_models_revision"),
        CheckConstraint("project_revision >= 2", name="ck_project_models_project_revision"),

        CheckConstraint(
            "source IN ('direct_yaml','template')", name="ck_project_models_source"
        ),
        #: 编号并发唯一兜底(行锁主路径 + 唯一约束兜底)
        UniqueConstraint("project_id", "suffix", name="uq_project_models_suffix"),
        UniqueConstraint("project_id", "device_id", name="uq_project_models_device_id"),
        Index("idx_project_models_project", "project_id"),
        Index("idx_project_models_object", "model_object_id"),
    )

class ProjectModelSequence(Base):
    """项目模型编号计数器(每项目一行, 只递增不复用)。

    行锁(``SELECT ... FOR UPDATE`` / ``UPDATE ... RETURNING``)串行化同项目
    并发分配; 行不存在时插入 ``next_suffix=2`` 并返回 1。
    """

    __tablename__ = "project_model_sequences"

    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), primary_key=True)
    next_suffix: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=sa.text("1"))

    __table_args__ = (
        CheckConstraint("next_suffix >= 1", name="ck_project_model_sequences_next"),
    )
