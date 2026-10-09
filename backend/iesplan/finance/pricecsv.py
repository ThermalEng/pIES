"""`ies.price-series` 1.0.0 连续 step 价格文件契约。

独立于带 ``device_id`` 的 device-data CSV：两列 ``step,price``，价格为
有限 Decimal（正/零/负均可）；不设项目年限要求（全期覆盖在任务提交
边界核对）。本模块只做纯协议解析、数值校验和规范化。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from iesplan.core.diagnostics import Diagnostic, make_diag
from iesplan.core.timeaxis import RESOLUTIONS
from iesplan.core.units import UnitError, normalize_unit

SCHEMA_ID = "ies.price-series"
SCHEMA_VERSION = "1.0.0"

_REQUIRED_META_KEYS = ("schema", "schema_version", "resolution", "unit")


@dataclass(frozen=True, slots=True)
class PriceSeriesMeta:
    """价格文件不可变元数据（分辨率与单位来自文件自描述）。"""

    resolution: str
    unit: str


@dataclass(frozen=True, slots=True)
class ParsedPriceFile:
    """解析后的价格文件（值保持 Decimal 原精度，物化时再换算）。"""

    meta: PriceSeriesMeta
    values: tuple[Decimal, ...]


def _diag(code: str, params: Mapping[str, object]) -> Diagnostic:
    return make_diag(
        code,
        params=params,
        location={"object_type": "price-series", "field": "file"},
        blocking=True,
    )


def is_price_series_csv(data: bytes) -> bool:
    """上传字节是否为价格序列文件（只看 schema 标记，不做完整校验）。"""
    from iesplan.devices.datacontract2 import read_schema_marker

    schema, _ = read_schema_marker(data)
    return schema == SCHEMA_ID


def parse_price_series_file(data: bytes) -> tuple[ParsedPriceFile | None, list[Diagnostic]]:
    """解析价格 CSV，返回（解析结果，诊断）；任一阻断即结果为 None。"""
    diags: list[Diagnostic] = []
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None, [_diag("PRICE-CSV-001", {"detail": "文件无法按 UTF-8 解码"})]
    meta_lines: dict[str, str] = {}
    header: list[str] | None = None
    rows: list[list[str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            body = stripped[1:].strip()
            if ":" not in body:
                continue
            key, _, value = body.partition(":")
            key = key.strip()
            if key in meta_lines:
                return None, [_diag("PRICE-CSV-003", {"detail": f"元数据行重复: {key}"})]
            meta_lines[key] = value.strip()
            continue
        cells = [cell.strip() for cell in stripped.split(",")]
        if header is None:
            header = cells
            continue
        rows.append(cells)
    missing = [key for key in _REQUIRED_META_KEYS if key not in meta_lines]
    if missing:
        return None, [_diag("PRICE-CSV-003", {"detail": f"必需元数据缺失: {sorted(missing)}"})]
    unknown = sorted(set(meta_lines) - set(_REQUIRED_META_KEYS))
    if unknown:
        return None, [_diag("PRICE-CSV-003", {"detail": f"未知元数据键: {unknown}"})]
    if meta_lines["schema"] != SCHEMA_ID or meta_lines["schema_version"] != SCHEMA_VERSION:
        return None, [_diag(
            "PRICE-CSV-002",
            {"schema": meta_lines["schema"], "schema_version": meta_lines["schema_version"]},
        )]
    resolution = meta_lines["resolution"]
    if resolution not in RESOLUTIONS:
        return None, [_diag("PRICE-CSV-004", {"value": resolution, "allowed": sorted(RESOLUTIONS)})]
    try:
        unit = normalize_unit(meta_lines["unit"])
    except UnitError:
        return None, [_diag("PRICE-CSV-005", {"value": meta_lines["unit"]})]
    if header != ["step", "price"]:
        return None, [_diag("PRICE-CSV-006", {"actual": list(header) if header else []})]
    if not rows:
        return None, [_diag("PRICE-CSV-009", {"detail": "无数据行"})]
    values: list[Decimal] = []
    for index, cells in enumerate(rows):
        if len(cells) != 2:
            return None, [_diag("PRICE-CSV-006", {"actual": cells, "step": index})]
        try:
            step = int(cells[0])
        except ValueError:
            return None, [_diag("PRICE-CSV-007", {"step": cells[0], "expected": index})]
        if step != index:
            return None, [_diag("PRICE-CSV-007", {"step": step, "expected": index})]
        try:
            value = Decimal(cells[1])
        except InvalidOperation:
            return None, [_diag("PRICE-CSV-008", {"step": index, "value": cells[1]})]
        if not value.is_finite():
            return None, [_diag("PRICE-CSV-008", {"step": index, "value": cells[1]})]
        values.append(value)
    return (
        ParsedPriceFile(
            meta=PriceSeriesMeta(resolution=resolution, unit=unit),
            values=tuple(values),
        ),
        diags,
    )


def emit_price_series_csv(meta: PriceSeriesMeta, values: tuple[Decimal, ...]) -> bytes:
    """规范价格 CSV 字节（元数据固定顺序 + step,price 行，值按原文）。"""
    lines = [
        f"# schema: {SCHEMA_ID}",
        f"# schema_version: {SCHEMA_VERSION}",
        f"# resolution: {meta.resolution}",
        f"# unit: {meta.unit}",
        "step,price",
    ]
    lines += [f"{index},{value}" for index, value in enumerate(values)]
    return ("\n".join(lines) + "\n").encode("utf-8")


def price_file_to_dict(parsed: ParsedPriceFile) -> dict[str, Any]:
    """解析结果转 JSON 兼容字典（值保持十进制字符串，不转 float）。"""
    return {
        "schema": SCHEMA_ID,
        "schema_version": SCHEMA_VERSION,
        "resolution": parsed.meta.resolution,
        "unit": parsed.meta.unit,
        "values": [str(value) for value in parsed.values],
    }


__all__ = [
    "SCHEMA_ID",
    "SCHEMA_VERSION",
    "PriceSeriesMeta",
    "ParsedPriceFile",
    "is_price_series_csv",
    "parse_price_series_file",
    "emit_price_series_csv",
    "price_file_to_dict",
]
