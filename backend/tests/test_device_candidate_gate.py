"""devices.candidate 入口门禁纯函数测试（Wave 3-D）。

覆盖：空文本 / 字节上限 / 安全解析拒绝 / 顶层非 mapping / 通过；
上限值为 devices 域单一权威（项目模型候选与模板草稿共用）。
"""

from __future__ import annotations

from iesplan.devices.candidate import (
    MAX_CANDIDATE_YAML_BYTES,
    parse_candidate_text,
)


def test_limit_is_single_authority() -> None:
    """候选 YAML 上限唯一权威值（2 MiB）。"""
    assert MAX_CANDIDATE_YAML_BYTES == 2 * 1024 * 1024


def test_ok_mapping_passthrough() -> None:
    """合法 mapping 通过，返回原始映射且无失败。"""
    raw, failure = parse_candidate_text("device:\n  id: ies.device.pv\n")
    assert failure is None
    assert raw is not None
    assert raw["device"]["id"] == "ies.device.pv"


def test_empty_rejected() -> None:
    """空文本拒绝（kind=empty）。"""
    for text in ("", "   ", "\n"):
        raw, failure = parse_candidate_text(text)
        assert raw is None
        assert failure is not None and failure.kind == "empty"


def test_too_large_rejected() -> None:
    """超上限拒绝（kind=too_large，携带实际字节数）。"""
    raw, failure = parse_candidate_text("x: 1", limit_bytes=3)
    assert raw is None
    assert failure is not None and failure.kind == "too_large"
    assert failure.actual_bytes == len("x: 1".encode("utf-8"))


def test_parse_error_rejected() -> None:
    """重复键等解析拒绝（kind=parse_error，携带行号）。"""
    raw, failure = parse_candidate_text("a: 1\na: 2\n")
    assert raw is None
    assert failure is not None and failure.kind == "parse_error"
    assert failure.detail != ""
    assert failure.line == 2


def test_not_mapping_rejected() -> None:
    """顶层非 mapping 拒绝（kind=not_mapping，携带实际类型名）。"""
    raw, failure = parse_candidate_text("- a\n- b\n")
    assert raw is None
    assert failure is not None and failure.kind == "not_mapping"
    assert failure.actual_type == "list"
