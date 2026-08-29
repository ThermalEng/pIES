"""用户自定义模型模板域(切片 dm2: 模板完整生命周期)。

对应格式标准「模板(未实例化阶段)」与 customization-center.md「编写设备模型」:

- ``model_templates``: 模板主表。每行代表当前用户的一个模型模板
  (模板 ID = 模板声明的 ``device.id``, 同一用户内唯一); 保存未发布的
  草稿内容(对象引用 + 摘要)与生命周期状态:
  ``draft``(未发布) / ``published``(已发布且启用) / ``disabled``(已发布但停用);
- ``model_template_revisions``: 不可变发布 revision 表。每次发布把草稿内容
  固化为单调递增 revision, 保存规范 YAML、校验回执与结构摘要的对象引用
  及内容摘要; 相同规范内容的重复发布幂等返回同一 revision;
  revision 一旦发布永不修改或删除(历史项目模型按精确 revision 解释)。

模板稳定 ID、发布 revision、``schema_version`` 与 ``content_sha256`` 共同
固定精确内容; 项目模型使用模板时固定精确 revision 与摘要。

数据库层由版本化迁移 0002 创建(见 ``iesplan/migrations``, 宪法 §11);
ORM 模型与 Base.metadata 注册仅作测试基建(create_all)与运行期读写载体,
发布机制以版本化迁移为准。
"""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from iesplan.db import Base
from iesplan.models.common import HASH64_RE, bigint_pk, regex_check

#: 模板生命周期状态
TEMPLATE_STATUS_DRAFT = "draft"
TEMPLATE_STATUS_PUBLISHED = "published"
TEMPLATE_STATUS_DISABLED = "disabled"
TEMPLATE_STATUSES: tuple[str, ...] = (
    TEMPLATE_STATUS_DRAFT,
    TEMPLATE_STATUS_PUBLISHED,
    TEMPLATE_STATUS_DISABLED,
)


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
    #: 草稿 YAML 对象引用(objects.id; 无草稿时 NULL)
    draft_yaml_object_id: Mapped[int | None] = mapped_column(ForeignKey("objects.id"))
    #: 草稿最近一次校验的聚合诊断 JSON 对象引用(objects.id; 无草稿时 NULL)
    draft_diagnostics_object_id: Mapped[int | None] = mapped_column(ForeignKey("objects.id"))
    #: 草稿内容摘要(小写 64 位十六进制 SHA-256; 无草稿时 NULL)
    draft_sha256: Mapped[str | None] = mapped_column(Text)
    #: 草稿内容是否声明顶层 inputs(列表/表单生成依据)
    draft_has_inputs: Mapped[bool | None] = mapped_column(sa.Boolean)
    #: 草稿乐观锁修订(每次保存草稿 +1; 并发编辑以 expected_revision 拒绝)
    draft_revision: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=sa.text("0"))
    draft_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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
        regex_check(
            f"draft_sha256 IS NULL OR draft_sha256 ~ '{HASH64_RE}'",
            name="ck_model_templates_draft_sha256",
        ),
        #: 同一用户模板 ID 唯一(模板按用户隔离, 不构成全局命名空间)
        UniqueConstraint("owner_id", "template_id", name="uq_model_templates_owner_id"),
        Index("idx_model_templates_owner", "owner_id"),
    )


class ModelTemplateRevision(Base):
    """不可变模板发布 revision(每次发布一行; 永不修改或删除)。

    ``content_sha256`` 为模板规范字节摘要(含顶层 ``inputs``); 相同规范
    内容幂等返回同一 revision(``(template_id, content_sha256)`` 唯一约束
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
    #: 模板规范字节内容摘要(含顶层 inputs; 与 yaml_object_id 内容一致)
    content_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    #: 顶层 inputs 树摘要(模板校验回执追溯用)
    inputs_sha256: Mapped[str | None] = mapped_column(Text)
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
        regex_check("content_sha256 ~ '^[0-9a-f]{64}$'", name="ck_mtr_content_sha256"),
        regex_check(
            "inputs_sha256 IS NULL OR inputs_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_mtr_inputs_sha256",
        ),
        UniqueConstraint("template_id", "revision", name="uq_model_template_revisions_revision"),
        #: 同内容幂等(重复发布相同规范内容返回同一 revision)
        UniqueConstraint("template_id", "content_sha256", name="uq_model_template_revisions_sha"),
        Index("idx_mtr_template", "template_id"),
        Index("idx_mtr_idem_key", "template_id", "idempotency_key"),
    )
