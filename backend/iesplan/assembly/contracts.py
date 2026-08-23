"""ies.assembly 1.0.0 契约:ValidatedAssemblyArtifact 与校验回执(roadmap 0.7.0)。

成功产物是不可变三件套(见 manual/developer-guide/zh-CN/formats/assembly-yaml.md
「ValidatedAssemblyArtifact」节):
1. 规范装配文本(canonical_text):时间统一 UTC、资源为内容 ID、字段稳定排序;
2. assembly_sha256:对规范字节计算的 SHA-256;
3. 校验回执(ValidationReceipt):校验器 ID/版本、schema、规范化算法 ID/版本、
   依赖锁、资源摘要与零阻断诊断。

生成器必须同时验证三者一致(``verify_or_raise``);人工修改规范文本、替换资源
或变更依赖后摘要与回执失效,必须重新装配。产物深度不可变:构造后禁止修改。

本模块只依赖 core(diagnostics/errors)与 assembly 域诊断码目录,不导入
devices/services/数据库。
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from iesplan.assembly.diags import ASM_ART_MISMATCH
from iesplan.core.diagnostics import Diagnostic, make_diag
from iesplan.core.errors import AppError

# ---------------------------------------------------------------------------
# 契约常量
# ---------------------------------------------------------------------------

SCHEMA_ID = "ies.assembly"
SCHEMA_VERSION = "1.0.0"

#: 机器可读 schema 路径(相对本包;与 devices 的 schema/ 布局一致)
ASSEMBLY_SCHEMA_PATH = "schema/assembly-1.0.0.schema.json"

#: 规范化算法 ID 与版本(写入回执;语义变化必须升版本并保留历史解释能力)
CANON_ALGORITHM_ID = "ies.assembly.canonical"
CANON_ALGORITHM_VERSION = "1.0.0"

#: 四阶段校验器 ID 与版本(写入回执)
VALIDATOR_ID = "ies.assembly.validator"
VALIDATOR_VERSION = "1.0.0"


# ---------------------------------------------------------------------------
# 校验回执
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ValidationReceipt:
    """校验回执:校验器/规范化算法/schema/依赖锁/资源摘要/零阻断诊断。

    字段顺序固定,``to_dict()`` 输出确定性 JSON 兼容字典(供持久化与审计)。
    """

    assembly_sha256: str = ""
    schema_id: str = SCHEMA_ID
    schema_version: str = SCHEMA_VERSION
    validator_id: str = VALIDATOR_ID
    validator_version: str = VALIDATOR_VERSION
    canonical_algorithm_id: str = CANON_ALGORITHM_ID
    canonical_algorithm_version: str = CANON_ALGORITHM_VERSION
    dependencies: Mapping[str, object] = field(default_factory=dict)  # 依赖锁(只读视图)
    resources: Mapping[str, object] = field(default_factory=dict)  # 资源摘要 {dataset_id: {sha256, media_type}}
    diagnostics: tuple[Diagnostic, ...] = ()  # 零阻断(可含 warning/info)
    issued_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat().replace("+00:00", "Z"))

    def to_dict(self) -> dict:
        """序列化为 JSON 兼容字典(字段固定顺序,确定性)。"""
        return {
            "schema": self.schema_id,
            "schema_version": self.schema_version,
            "validator": {
                "id": self.validator_id,
                "version": self.validator_version,
            },
            "canonical_algorithm": {
                "id": self.canonical_algorithm_id,
                "version": self.canonical_algorithm_version,
            },
            "assembly_sha256": self.assembly_sha256,
            "dependencies": dict(self.dependencies),
            "resources": dict(self.resources),
            "diagnostics": [d.to_dict() for d in self.diagnostics],
            "issued_at": self.issued_at,
        }


# ---------------------------------------------------------------------------
# 成功产物
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ValidatedAssemblyArtifact:
    """唯一、可签名的成功装配产物(不可变三件套)。

    - canonical_text: 规范装配文本(UTF-8, LF;JSON 规范形态);
    - assembly_sha256: 规范字节 SHA-256;
    - receipt: 校验回执(含相同摘要与依赖锁)。

    ``verify()`` 重新计算摘要并核对三件套一致;任何不一致必须拒绝使用并重新
    装配,禁止带病继续计算。
    """

    canonical_text: str
    assembly_sha256: str
    receipt: ValidationReceipt

    def verify(self) -> bool:
        """重算规范文本摘要并与 assembly_sha256 / 回执摘要核对。"""
        return (
            hashlib.sha256(self.canonical_text.encode("utf-8")).hexdigest() == self.assembly_sha256
            and self.receipt.assembly_sha256 == self.assembly_sha256
        )

    def verify_or_raise(self) -> "ValidatedAssemblyArtifact":
        """一致性校验失败抛 AssemblyValidationError(阻断计算),成功返回自身。"""
        if not self.verify():
            diag = make_diag(
                ASM_ART_MISMATCH,
                severity="error",
                blocking=True,
                params={
                    "expected": self.assembly_sha256,
                    "actual": hashlib.sha256(self.canonical_text.encode("utf-8")).hexdigest(),
                    "reason": "canonical_text_sha256_mismatch",
                },
                location={"object_type": "assembly", "field": "artifact"},
            )
            raise AssemblyValidationError([diag])
        return self

    def to_dict(self) -> dict:
        """序列化(供审计/持久化;内容与 verify 结果一致)。"""
        return {
            "schema": SCHEMA_ID,
            "schema_version": SCHEMA_VERSION,
            "canonical_text": self.canonical_text,
            "assembly_sha256": self.assembly_sha256,
            "receipt": self.receipt.to_dict(),
        }


# ---------------------------------------------------------------------------
# 校验失败
# ---------------------------------------------------------------------------


class AssemblyValidationError(AppError):
    """装配校验未通过(存在阻断诊断),携带完整诊断列表,供任务闸门抛 HTTP 422。

    与 AssemblyCheckError 同构:不产生任何可执行产物,调用方只能看到诊断。
    """

    code = "ASM-VALIDATE-FAILED"
    message_key = "ies.diag.asm.check_failed"
    http_status = 422

    def __init__(self, diagnostics: Sequence[Diagnostic], message: str = "") -> None:
        self.diagnostics = list(diagnostics)
        super().__init__(
            message or f"装配校验未通过:{len(self.diagnostics)} 条诊断",
            code=self.code,
            message_key=self.message_key,
            params={
                "diag_count": len(self.diagnostics),
                "diagnostics": [d.to_dict() for d in self.diagnostics],
            },
        )


__all__ = [
    "SCHEMA_ID",
    "SCHEMA_VERSION",
    "ASSEMBLY_SCHEMA_PATH",
    "CANON_ALGORITHM_ID",
    "CANON_ALGORITHM_VERSION",
    "VALIDATOR_ID",
    "VALIDATOR_VERSION",
    "ValidationReceipt",
    "ValidatedAssemblyArtifact",
    "AssemblyValidationError",
]
