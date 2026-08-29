"""项目模型清单域(切片 dm2-A: application/projects 用例持久化)。

对应格式标准「进入项目前的候选模型门禁」与 modules/application.md
「典型示例:保存项目模型」:

- ``project_models``: 项目模型清单表。每行代表一个已保存的项目模型实例
  (最终 ``device.id`` 携带项目内 ``_N`` 后缀); 记录规范内容摘要、模型/
  回执对象引用与来源追溯(直接 YAML / 模板实例化)。
- ``project_model_sequences``: 每项目一个编号计数器行。编号只递增、删除
  不复用; ``UPDATE ... RETURNING``(PostgreSQL 行锁)与
  ``(project_id, suffix)`` 唯一约束共同保证并发分配唯一。

数据库层由版本化 migration 创建(见 ``iesplan/migrations``, 宪法 §11);
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

#: 项目模型来源(直接 YAML 或模板实例化; 两者汇合同一保存用例)
MODEL_SOURCE_DIRECT = "direct_yaml"
MODEL_SOURCE_TEMPLATE = "template"
MODEL_SOURCES: tuple[str, ...] = (MODEL_SOURCE_DIRECT, MODEL_SOURCE_TEMPLATE)


class ProjectModel(Base):
    """项目模型清单表(项目内每个已保存模型实例一行)。

    device_id 为最终带 ``_N`` 后缀的 ID(与模型 YAML 文件一致);
    content_sha256 为规范内容摘要(规范化器 ``ies.device-model.canonical@2.0.0``);
    receipt_object_id 指向校验回执 JSON 对象(回执引用)。
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
    #: 规范内容摘要(小写 64 位十六进制 SHA-256)
    content_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    #: 模型规范 YAML/JSON 文件对象引用(objects.id)
    model_object_id: Mapped[int] = mapped_column(ForeignKey("objects.id"), nullable=False)
    #: 校验回执对象引用(objects.id)
    receipt_object_id: Mapped[int] = mapped_column(ForeignKey("objects.id"), nullable=False)
    #: 来源(direct_yaml | template)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    #: 模板追溯: 模板原始字节摘要与用户 inputs 摘要(模板来源时非空)
    template_sha256: Mapped[str | None] = mapped_column(Text)
    inputs_sha256: Mapped[str | None] = mapped_column(Text)
    #: 幂等键(项目内唯一, 重试返回同一逻辑结果)
    idempotency_key: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )

    __table_args__ = (
        CheckConstraint("suffix >= 1", name="ck_project_models_suffix"),
        CheckConstraint("revision >= 1", name="ck_project_models_revision"),
        regex_check(f"content_sha256 ~ '{HASH64_RE}'", name="ck_project_models_content_sha256"),
        regex_check(
            "template_sha256 IS NULL OR template_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_project_models_template_sha256",
        ),
        regex_check(
            "inputs_sha256 IS NULL OR inputs_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_project_models_inputs_sha256",
        ),
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
