"""ies.assembly 1.0.0 契约:ValidatedAssemblyArtifact 与校验回执(roadmap 0.7.0)。

成功产物是不可变二件套(见 manual/developer-guide/zh-CN/formats/assembly-yaml.md
「ValidatedAssemblyArtifact」节):
1. 规范装配文本(canonical_text):时间统一 UTC、资源为内容 ID、字段稳定排序;
2. 校验回执(ValidationReceipt):校验器 ID/版本、schema、规范化算法 ID/版本、
   依赖锁、资源记录与零阻断诊断。

产物深度不可变，构造后禁止修改。

本模块只依赖 core(diagnostics/errors)与 assembly 域诊断码目录,不导入
devices/services/数据库。
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType

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
    """校验回执:校验器/规范化算法/schema/依赖锁/资源记录/零阻断诊断。

    字段顺序固定，``to_dict()`` 输出确定性 JSON 兼容字典。回执是输入内容的
    可复现证明，不包含签发时间或 trace/task 上下文；运行审计时间由快照/任务表
    自身记录。所有容器在构造时递归冻结，调用方持有的原始 dict/list 后续变化
    不会影响回执。
    """

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
            "dependencies": _thaw_value(self.dependencies),
            "resources": _thaw_value(self.resources),
            "diagnostics": [_stable_diagnostic_dict(diag) for diag in self.diagnostics],
        }

# ---------------------------------------------------------------------------
# 成功产物
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ValidatedAssemblyArtifact:
    """唯一、可签名的成功装配产物(不可变二件套)。

    - canonical_text: 规范装配文本(UTF-8, LF;JSON 规范形态);
    - receipt: 校验回执。

    产物由签发边界一次性校验后构造，调用方直接消费，不做重复复核。
    """

    canonical_text: str
    receipt: ValidationReceipt = None  # type: ignore

    def to_dict(self) -> dict:
        """序列化(供审计/持久化)。"""
        return {
            "schema": SCHEMA_ID,
            "schema_version": SCHEMA_VERSION,
            "canonical_text": self.canonical_text,
            "receipt": self.receipt.to_dict(),
        }


# ---------------------------------------------------------------------------
# 校验失败
# ---------------------------------------------------------------------------


class AssemblyValidationError(AppError):
    """装配校验未通过(存在阻断诊断),携带完整诊断列表,供任务闸门抛 HTTP 422。

    不产生任何可执行产物,调用方只能看到诊断。
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
