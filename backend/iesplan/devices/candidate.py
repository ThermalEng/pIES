"""设备模型候选 YAML 文本入口门禁（归属 devices）。

项目模型候选（``application.models.model_save``）与模板草稿
（``application.model_templates``）共用同一门禁：空文本 → 字节上限 →
yamlmini 安全子集解析 → 顶层 mapping，四项与 devices 2.0 解析前置条件一致。

本模块只返回中性失败描述，不抛域错误、不定诊断码；调用方按各自诊断码
（PROJ-MDL-006 / TPL-MDL-001，集中登记于 core/diagnostics.py）映射失败。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from iesplan.core.yamlmini import YamlParseError
from iesplan.core.yamlmini import load as yaml_load

#: 候选 YAML 上限（2 MiB；项目模型候选与模板草稿共用同一值，单一权威）。
MAX_CANDIDATE_YAML_BYTES: int = 2 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class CandidateTextFailure:
    """候选文本门禁失败（中性描述；调用方映射为各自诊断码）。"""

    #: 失败种类：empty（空文本）/ too_large（超字节上限）/
    #: parse_error（yamlmini 解析拒绝）/ not_mapping（顶层非 mapping）。
    kind: str
    detail: str
    actual_bytes: int | None = None
    line: int | None = None
    actual_type: str | None = None


def parse_candidate_text(
    text: str,
    *,
    limit_bytes: int = MAX_CANDIDATE_YAML_BYTES,
) -> tuple[Mapping[str, Any] | None, CandidateTextFailure | None]:
    """候选 YAML 文本入口门禁：通过返回 (原始映射, None)，失败返回 (None, 失败描述）。

    检查顺序与历史行为一致：空文本 → 字节上限 → 安全解析 → 顶层 mapping。
    """
    if not text or not text.strip():
        return None, CandidateTextFailure(kind="empty", detail="候选 YAML 文本为空")
    size = len(text.encode("utf-8"))
    if size > limit_bytes:
        return None, CandidateTextFailure(
            kind="too_large", detail=f"候选 YAML 超过上限 {limit_bytes} 字节", actual_bytes=size
        )
    try:
        raw = yaml_load(text)
    except YamlParseError as exc:
        return None, CandidateTextFailure(
            kind="parse_error", detail=str(exc), line=exc.line
        )
    if not isinstance(raw, Mapping):
        return None, CandidateTextFailure(
            kind="not_mapping", detail="候选顶层必须是 mapping", actual_type=type(raw).__name__
        )
    return raw, None


__all__ = [
    "CandidateTextFailure",
    "MAX_CANDIDATE_YAML_BYTES",
    "parse_candidate_text",
]
