"""ies.assembly 1.0.0 唯一规范化器(roadmap 0.7.0 事项 1)。

规范化是公开纯过程(file-formats.md「人工编写与规范化」):
- 映射键按格式规则稳定排序;有业务顺序的列表(series/metrics)保留声明顺序;
- 时间换算为带 Z 的 UTC(解析失败抛 ValueError,结构阶段已先行拒绝无偏移形态);
- relative_file 已由校验器解析为内容寻址对象(kind: object);本模块对仍为
  relative_file 的输入确定性拒绝,不允许未解析资源进入规范字节;
- 数值使用唯一有限十进制表示(整数与整值浮点同语义 → 同文本,不依赖 locale);
- 注释/显示空白/YAML 表示差异不参与语义摘要(解析期已丢失,字节级稳定);
- 规范文本为紧凑 JSON + LF,UTF-8;对规范字节计算 SHA-256。

相同语义必须得到相同规范文本与摘要。规范化算法 ID/版本由 contracts.py 的
CANON_ALGORITHM_ID/CANON_ALGORITHM_VERSION 声明并写入校验回执;算法语义变化
必须升版本并保留历史解释能力。

本模块只依赖标准库,不导入任何业务模块(公开纯函数边界)。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from iesplan.assembly.contracts import CANON_ALGORITHM_ID, CANON_ALGORITHM_VERSION

# ---------------------------------------------------------------------------
# 键序(格式规则:固定键序 + 其余按键名排序)
# ---------------------------------------------------------------------------

_TOP_ORDER = (
    "schema",
    "schema_version",
    "assembly",
    "time_axis",
    "resources",
    "devices",
    "connections",
    "constraints",
    "calculation",
    "outputs",
    "extensions",
)

_ASSEMBLY_ORDER = ("id", "name")
_TIME_AXIS_ORDER = ("start", "end", "resolution", "endpoint")
_SOURCE_ORDER = ("kind", "object_id", "sha256", "media_type")
_DATASET_ORDER = ("source",)
_DEVICE_ORDER = ("model", "parameters", "data")
_DATA_BINDING_ORDER = ("dataset", "column")
_CONNECTION_ORDER = ("from", "to")
_CONSTRAINT_ORDER = ("type", "expr", "enabled")
_CALCULATION_ORDER = ("mode", "generator", "solver", "options", "random_seed")
_OUTPUTS_ORDER = ("series", "metrics")

#: 规范形态下禁止出现的资源来源(校验器必须先行解析为 object)
_UNRESOLVED_KIND = "relative_file"

# ---------------------------------------------------------------------------
# 时间
# ---------------------------------------------------------------------------

_OFFSET_SUFFIX = re.compile(r"([+-]\d{2}:\d{2})$")


def parse_iso8601_utc(text: str) -> datetime:
    """解析 ISO 8601 时间戳 → UTC datetime(带 Z 或 ±HH:MM 偏移)。

    无偏移的本地时间无法唯一换算,确定性拒绝(结构阶段同样拒绝)。
    """
    value = text.strip()
    if value.endswith("Z") or value.endswith("z"):
        inner = value[:-1]
        try:
            return datetime.fromisoformat(inner).replace(tzinfo=UTC)
        except ValueError:
            raise ValueError(f"非法 UTC 时间戳: {text!r}") from None
    if _OFFSET_SUFFIX.search(value) is not None:
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            raise ValueError(f"非法带偏移时间戳: {text!r}") from None
        if dt.tzinfo is None:
            raise ValueError(f"非法带偏移时间戳: {text!r}") from None
        return dt.astimezone(UTC)
    raise ValueError(f"时间戳必须带 Z 或 ±HH:MM 偏移(无法唯一换算 UTC): {text!r}")


def format_utc_z(dt: datetime) -> str:
    """datetime → 规范 UTC 文本(带 Z,秒精度;亚秒保留)。"""
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _to_utc_z(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError(f"时间戳必须为字符串: {value!r}")
    return format_utc_z(parse_iso8601_utc(value))


# ---------------------------------------------------------------------------
# 数值唯一有限表示
# ---------------------------------------------------------------------------


def format_number(value: Any) -> str:
    """数值 → 唯一有限十进制表示(整数与整值浮点同文本;非有限拒绝)。

    - int → 十进制整数文本;
    - float → 整值(|x| < 1e15)输出整数文本,否则 repr(最短往返);
    - bool 由 JSON 序列化处理(true/false),不在此转换。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"非数值不能进入规范数字表示: {value!r}")
    f = float(value)
    if not math.isfinite(f):
        raise ValueError(f"不可序列化的非有限数值: {value!r}")
    if f.is_integer() and abs(f) < 1e15:
        return str(int(f))
    return repr(f)


def _normalize_number(value: Any) -> int | float:
    """数值 → JSON 安全规范形态(整值统一为 int;非有限拒绝)。

    json.dumps 对 int 输出十进制整数、对 float 输出 repr(最短往返),
    因此 int 800 与 float 800.0 同语义 → 同规范字节("800")。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"非数值不能进入规范数字表示: {value!r}")
    f = float(value)
    if not math.isfinite(f):
        raise ValueError(f"不可序列化的非有限数值: {value!r}")
    if f.is_integer() and abs(f) < 1e15:
        return int(f)
    return f


# ---------------------------------------------------------------------------
# 稳定排序
# ---------------------------------------------------------------------------


def _ordered(value: Any, order: tuple[str, ...] = ()) -> Any:
    """按给定键序重排 dict,其余嵌套 dict 按键名排序(稳定确定性)。

    列表原样保留声明顺序(有业务顺序的列表不排序)。
    """
    if isinstance(value, Mapping):
        ordered: dict[str, Any] = {}
        for key in order:
            if key in value:
                ordered[key] = _ordered(value[key], ())
        for key in sorted(k for k in value if k not in order):
            ordered[key] = _ordered(value[key], ())
        return ordered
    if isinstance(value, list):
        return [_ordered(v, ()) for v in value]
    if isinstance(value, tuple):
        return [_ordered(v, ()) for v in value]
    return value


def _normalize_doc(doc: Mapping) -> dict:
    """规范形态预处理:时间 → UTC Z;数值 → 唯一有限 JSON 形态。

    返回深度复制的 plain dict(JSON 可序列化);任何不可换算/非有限值确定性拒绝。
    """
    out: dict[str, Any] = {}
    for key, value in doc.items():
        if key == "time_axis" and isinstance(value, Mapping):
            axis: dict[str, Any] = {}
            for k, v in value.items():
                if k in ("start", "end"):
                    axis[k] = _to_utc_z(v)
                elif k == "resolution" and isinstance(v, str):
                    axis[k] = v
                elif k == "endpoint" and isinstance(v, str):
                    axis[k] = v
                else:
                    axis[k] = _normalize_value(v)
            out[key] = axis
        elif key == "resources" and isinstance(value, Mapping):
            out[key] = _normalize_resources(value)
        elif key == "calculation" and isinstance(value, Mapping):
            calc: dict[str, Any] = {}
            for k, v in value.items():
                if k == "options" and isinstance(v, Mapping):
                    calc[k] = {
                        str(ok): _normalize_number(ov) for ok, ov in v.items()
                    }
                elif k == "random_seed":
                    calc[k] = _normalize_number(v)
                else:
                    calc[k] = _normalize_value(v)
            out[key] = calc
        else:
            out[key] = _normalize_value(value)
    return out


def _normalize_resources(resources: Mapping) -> dict:
    """resources 规范形态:datasets 内 dataset_id 键排序,source 固定键序。"""
    datasets_raw = resources.get("datasets", {})
    datasets: dict[str, Any] = {}
    if isinstance(datasets_raw, Mapping):
        for ds_id in sorted(datasets_raw):
            entry = datasets_raw[ds_id]
            if not isinstance(entry, Mapping):
                raise ValueError(f"资源数据集条目必须为映射: {ds_id!r}")
            src = entry.get("source")
            if not isinstance(src, Mapping):
                raise ValueError(f"资源数据集 {ds_id!r} 缺少 source 映射")
            if src.get("kind") == _UNRESOLVED_KIND:
                raise ValueError(
                    f"资源数据集 {ds_id!r} 仍是 relative_file: 未解析的资源不得进入规范字节"
                )
            datasets[str(ds_id)] = {
                "source": _ordered({str(k): _normalize_value(v) for k, v in src.items()}, _SOURCE_ORDER)
            }
    return {"datasets": datasets}


def _normalize_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _normalize_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize_value(v) for v in value]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return _normalize_number(value)
    return value


# ---------------------------------------------------------------------------
# 公开纯函数
# ---------------------------------------------------------------------------


def canonicalize_assembly_doc(doc: Mapping) -> tuple[str, str]:
    """唯一规范化:已解析文档 → (规范文本, SHA-256)。

    - doc 必须是已完成结构/模型/数据/资源解析的 ies.assembly 1.0.0 文档
      (resources.datasets.source 均为 object 形态;未知核心字段已在结构阶段拒绝);
    - 规范文本:紧凑 JSON(ensure_ascii=False, separators=(',', ':'), 拒绝 NaN),
      末尾 LF;UTF-8 编码后计算 SHA-256。
    """
    plain = _normalize_doc(doc)
    ordered = _ordered(plain, _TOP_ORDER)
    canonical = json.dumps(
        ordered,
        ensure_ascii=False,
        sort_keys=False,  # 已由 _ordered 保证确定性
        separators=(",", ":"),
        allow_nan=False,
    )
    canonical_text = canonical + "\n"
    digest = hashlib.sha256(canonical_text.encode("utf-8")).hexdigest()
    return canonical_text, digest


def assembly_sha256(canonical_text: str) -> str:
    """规范文本 → SHA-256(供校验回执与产物一致性核对)。"""
    return hashlib.sha256(canonical_text.encode("utf-8")).hexdigest()


def canonical_algorithm_ref() -> str:
    """规范化算法 ID@版本(写入回执与文档)。"""
    return f"{CANON_ALGORITHM_ID}@{CANON_ALGORITHM_VERSION}"


__all__ = [
    "CANON_ALGORITHM_ID",
    "CANON_ALGORITHM_VERSION",
    "parse_iso8601_utc",
    "format_utc_z",
    "format_number",
    "canonicalize_assembly_doc",
    "assembly_sha256",
    "canonical_algorithm_ref",
]
