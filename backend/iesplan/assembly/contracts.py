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
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType

from iesplan.assembly.diags import ASM_ART_MISMATCH
from iesplan.assembly.diags import make_asm_diag as make_diag
from iesplan.core.diagnostics import Diagnostic
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


def _freeze_value(value: object) -> object:
    """校验并递归冻结 JSON 值，阻断外部引用修改或非确定性对象混入。"""
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("回执映射键须为字符串")
        return MappingProxyType({key: _freeze_value(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("回执数值须为有限值")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"回执字段须为 JSON 值，得到 {type(value).__name__}")


def _thaw_value(value: object) -> object:
    """将只读容器递归复制为 JSON 兼容的普通 dict/list。"""
    if isinstance(value, Mapping):
        return {str(key): _thaw_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_value(item) for item in value]
    return value


def _stable_diagnostic_dict(diag: Diagnostic) -> dict:
    """回执仅保留确定性诊断语义，不写入时间戳、trace/task 等运行上下文。"""
    data = diag.to_dict()
    return {
        "code": data["code"],
        "severity": data["severity"],
        "blocking": data["blocking"],
        "message_key": data["message_key"],
        "params": data["params"],
        "location": data["location"],
        "fix_hint_key": data["fix_hint_key"],
        "ref_ids": data["ref_ids"],
        "suppressed": data["suppressed"],
    }


@dataclass(frozen=True, slots=True)
class ValidationReceipt:
    """校验回执:校验器/规范化算法/schema/依赖锁/资源摘要/零阻断诊断。

    字段顺序固定，``to_dict()`` 输出确定性 JSON 兼容字典。回执是输入内容的
    可复现证明，不包含签发时间或 trace/task 上下文；运行审计时间由快照/任务表
    自身记录。所有容器在构造时递归冻结，调用方持有的原始 dict/list 后续变化
    不会影响回执。
    """

    assembly_sha256: str = ""
    schema_id: str = SCHEMA_ID
    schema_version: str = SCHEMA_VERSION
    validator_id: str = VALIDATOR_ID
    validator_version: str = VALIDATOR_VERSION
    canonical_algorithm_id: str = CANON_ALGORITHM_ID
    canonical_algorithm_version: str = CANON_ALGORITHM_VERSION
    dependencies: Mapping[str, object] = field(default_factory=dict)
    resources: Mapping[str, object] = field(default_factory=dict)
    diagnostics: tuple[Diagnostic, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.dependencies, Mapping):
            raise TypeError("dependencies 须为 Mapping")
        if not isinstance(self.resources, Mapping):
            raise TypeError("resources 须为 Mapping")
        diagnostics = tuple(self.diagnostics)
        if not all(isinstance(diag, Diagnostic) for diag in diagnostics):
            raise TypeError("diagnostics 须仅包含 Diagnostic")
        object.__setattr__(self, "dependencies", _freeze_value(self.dependencies))
        object.__setattr__(self, "resources", _freeze_value(self.resources))
        object.__setattr__(self, "diagnostics", diagnostics)

    def to_dict(self) -> dict:
        """序列化为 JSON 兼容独立副本（字段固定顺序、确定性）。"""
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
            "dependencies": _thaw_value(self.dependencies),
            "resources": _thaw_value(self.resources),
            "diagnostics": [_stable_diagnostic_dict(diag) for diag in self.diagnostics],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> ValidationReceipt:
        """从持久化 JSON 严格恢复回执；畸形字段直接拒绝，不做兼容回退。"""
        if not isinstance(payload, Mapping):
            raise TypeError("receipt 须为 Mapping")
        expected_keys = {
            "schema",
            "schema_version",
            "validator",
            "canonical_algorithm",
            "assembly_sha256",
            "dependencies",
            "resources",
            "diagnostics",
        }
        if set(payload) != expected_keys:
            raise ValueError("receipt 字段集合与当前契约不一致")
        validator = payload.get("validator")
        canonical = payload.get("canonical_algorithm")
        if not isinstance(validator, Mapping) or not isinstance(canonical, Mapping):
            raise TypeError("receipt.validator/canonical_algorithm 须为 Mapping")
        if set(validator) != {"id", "version"} or set(canonical) != {"id", "version"}:
            raise ValueError("receipt validator/canonical_algorithm 字段集合不一致")
        dependencies = payload.get("dependencies", {})
        resources = payload.get("resources", {})
        raw_diags = payload.get("diagnostics", [])
        if not isinstance(dependencies, Mapping) or not isinstance(resources, Mapping):
            raise TypeError("receipt dependencies/resources 须为 Mapping")
        if not isinstance(raw_diags, (list, tuple)):
            raise TypeError("receipt diagnostics 须为数组")
        diagnostics: list[Diagnostic] = []
        for raw in raw_diags:
            if not isinstance(raw, Mapping):
                raise TypeError("receipt diagnostic 须为 Mapping")
            expected_diag_keys = {
                "code",
                "severity",
                "blocking",
                "message_key",
                "params",
                "location",
                "fix_hint_key",
                "ref_ids",
                "suppressed",
            }
            if set(raw) != expected_diag_keys:
                raise ValueError("receipt diagnostic 字段集合不一致")
            params = raw["params"]
            location = raw["location"]
            ref_ids = raw["ref_ids"]
            if not isinstance(params, Mapping):
                raise TypeError("receipt diagnostic.params 须为 Mapping")
            if location is not None and not isinstance(location, Mapping):
                raise TypeError("receipt diagnostic.location 须为 Mapping 或 null")
            if not isinstance(ref_ids, (list, tuple)):
                raise TypeError("receipt diagnostic.ref_ids 须为数组")
            if not isinstance(raw["blocking"], bool) or not isinstance(raw["suppressed"], bool):
                raise TypeError("receipt diagnostic 布尔字段类型非法")
            for key in ("code", "severity", "message_key", "fix_hint_key"):
                if not isinstance(raw[key], str) or not raw[key]:
                    raise TypeError(f"receipt diagnostic.{key} 须为非空字符串")
            if not all(isinstance(item, str) for item in ref_ids):
                raise TypeError("receipt diagnostic.ref_ids 仅允许字符串")
            diagnostics.append(
                Diagnostic(
                    code=raw["code"],
                    severity=raw["severity"],
                    blocking=raw["blocking"],
                    message_key=raw["message_key"],
                    params=params,
                    location=location,
                    fix_hint_key=raw["fix_hint_key"],
                    ref_ids=tuple(ref_ids),
                    suppressed=raw["suppressed"],
                )
            )
        string_fields = {
            "assembly_sha256": payload["assembly_sha256"],
            "schema": payload["schema"],
            "schema_version": payload["schema_version"],
            "validator.id": validator["id"],
            "validator.version": validator["version"],
            "canonical_algorithm.id": canonical["id"],
            "canonical_algorithm.version": canonical["version"],
        }
        for name, value in string_fields.items():
            if not isinstance(value, str) or not value:
                raise TypeError(f"receipt.{name} 须为非空字符串")
        return cls(
            assembly_sha256=payload["assembly_sha256"],
            schema_id=payload["schema"],
            schema_version=payload["schema_version"],
            validator_id=validator["id"],
            validator_version=validator["version"],
            canonical_algorithm_id=canonical["id"],
            canonical_algorithm_version=canonical["version"],
            dependencies=dependencies,
            resources=resources,
            diagnostics=tuple(diagnostics),
        )


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
        """重算摘要并核对三件套及其 schema/算法/校验器版本。"""
        return (
            hashlib.sha256(self.canonical_text.encode("utf-8")).hexdigest() == self.assembly_sha256
            and self.receipt.assembly_sha256 == self.assembly_sha256
            and self.receipt.schema_id == SCHEMA_ID
            and self.receipt.schema_version == SCHEMA_VERSION
            and self.receipt.validator_id == VALIDATOR_ID
            and self.receipt.validator_version == VALIDATOR_VERSION
            and self.receipt.canonical_algorithm_id == CANON_ALGORITHM_ID
            and self.receipt.canonical_algorithm_version == CANON_ALGORITHM_VERSION
            and not any(diag.blocking for diag in self.receipt.diagnostics)
        )

    @classmethod
    def from_persisted(
        cls,
        canonical_text: str,
        assembly_sha256: str,
        receipt: Mapping[str, object],
    ) -> ValidatedAssemblyArtifact:
        """严格恢复并验证持久化三件套，供 Worker/审计入口使用。"""
        artifact = cls(
            canonical_text=canonical_text,
            assembly_sha256=assembly_sha256,
            receipt=ValidationReceipt.from_dict(receipt),
        )
        return artifact.verify_or_raise()

    def verify_or_raise(self) -> ValidatedAssemblyArtifact:
        """一致性校验失败抛 AssemblyValidationError(阻断计算),成功返回自身。"""
        if not self.verify():
            diag = make_diag(
                ASM_ART_MISMATCH,
                severity="error",
                blocking=True,
                params={
                    "expected": self.assembly_sha256,
                    "actual": hashlib.sha256(self.canonical_text.encode("utf-8")).hexdigest(),
                    "reason": "artifact_or_receipt_contract_mismatch",
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
