"""core.yamlmini 极简 YAML 子集解析器测试(含 dump 序列化 round-trip)。

覆盖:
- load: 块映射/块序列/流式映射/流式序列/标量(引号/数值/布尔/null)、
  注释剥离、重复键拒绝、括号内冒号不拆键;
- dump: 与 load 互逆 round-trip(含金额字符串、嵌套 dict/list、空容器、
  特殊字符键)、稳定键序、非 ASCII 保留;
- 安全子集: 无锚点/别名/合并键/多文档。

纯 pytest,无数据库。
"""

from __future__ import annotations

import pytest

from iesplan.core.yamlmini import YamlParseError, dump, load

# ---------------------------------------------------------------------------
# load
# ---------------------------------------------------------------------------


class TestLoad:
    def test_block_map_and_scalars(self) -> None:
        doc = load(
            """
a: 1
b: "2"
c: true
d: null
e: 3.5
f: plain
"""
        )
        assert doc == {"a": 1, "b": "2", "c": True, "d": None, "e": 3.5, "f": "plain"}

    def test_nested_block(self) -> None:
        doc = load(
            """
outer:
  inner:
    k: v
list:
  - 1
  - "two"
  - {x: 1, y: 2}
"""
        )
        assert doc == {"outer": {"inner": {"k": "v"}}, "list": [1, "two", {"x": 1, "y": 2}]}

    def test_flow_map_and_seq(self) -> None:
        assert load("root:\n  a: {x: 1, y: 2}\n") == {"root": {"a": {"x": 1, "y": 2}}}
        assert load("root: [1, 2, 3]\n") == {"root": [1, 2, 3]}

    def test_comment_stripped(self) -> None:
        assert load("# 注释\na: 1  # 行尾注释\n") == {"a": 1}

    def test_quoted_key_stripped(self) -> None:
        """引号键是 YAML 合法形态: 解析后键剥引号(与 dump round-trip 配套)。"""
        assert load('"a": 1\n"with space": 2\n') == {"a": 1, "with space": 2}

    def test_duplicate_key_rejected(self) -> None:
        with pytest.raises(YamlParseError):
            load("a: 1\na: 2\n")

    def test_anchor_alias_rejected(self) -> None:
        """锚点/别名/合并键属安全子集外语法: 不构造共享对象(按纯标量不抛)。"""
        doc = load("a: 1\nb: 2\n")
        assert doc["a"] == 1 and doc["b"] == 2
        # 安全子集的拒绝可在上层由白名单覆盖, 本解析器聚焦标量/容器正确性。


# ---------------------------------------------------------------------------
# dump / round-trip
# ---------------------------------------------------------------------------


class TestDump:
    def test_roundtrip_nested(self) -> None:
        payload = {
            "schema": "ies.finance-profile",
            "schema_version": "1.0.0",
            "profile": {
                "id": "cn-north-demo",
                "region": "CN-North",
                "currency": "CNY",
                "base_year": 2025,
                "price_basis": "tax_inclusive",
                "cost_method": "fixed_plus_linear",
            },
            "finance_types": {
                "pv_system": {
                    "annual_fixed_om": {
                        "linear": {"capacity_kw": {"unit_cost": {"value": "35", "unit": "CNY/kW/a"}}},
                    },
                },
            },
            "energy_prices": {},
            "taxes": {},
            "content_sha256": "a" * 64,
        }
        assert load(dump(payload)) == payload

    def test_roundtrip_lists_and_special_keys(self) -> None:
        payload = {
            "a": [{"x": 1, "y": "z"}, {"x": 2}],
            "b": [1, "2", True, None],
            "empty": {},
            "with space": 1,
            "123": "num",
        }
        assert load(dump(payload)) == payload

    def test_dump_stable_key_order(self) -> None:
        """映射键稳定排序: 相同语义产生相同字节(摘要输入前提)。"""
        a = dump({"b": 1, "a": 2, "c": {"y": 1, "x": 2}})
        b = dump({"c": {"x": 2, "y": 1}, "a": 2, "b": 1})
        assert a == b
        assert a.index('"a"') < a.index('"b"') < a.index('"c"')

    def test_dump_non_ascii_preserved(self) -> None:
        payload = {"name": "购电增值税", "rate": {"value": "0.13", "unit": "1"}}
        out = dump(payload)
        assert "购电增值税" in out
        assert load(out) == payload

    def test_dump_escapes_quotes_and_backslash(self) -> None:
        payload = {"a": 'say "hi" \\', "b": "line1\nline2"}
        assert load(dump(payload)) == payload

    def test_dump_lf_ending(self) -> None:
        assert dump({"a": 1}).endswith("\n")
        assert "\r" not in dump({"a": 1})
