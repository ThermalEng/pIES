"""ies.device-model 2.0.0 专用非法样例集契约验收（Roadmap 0.8.0 条目 1）。

基于真实合法 v2 文档的最小非法 YAML，每个只违反一个契约。
测试遍历发布目录 backend/iesplan/devices/samples/invalid/2.0/ 下的非法样例，
调用公开 v2 解析入口，断言失败诊断精确指向违规规则/字段。
"""

from __future__ import annotations

import pathlib
from collections.abc import Mapping
from typing import Any, cast

import pytest  # type: ignore[import-untyped]

from iesplan.core.yamlmini import load as yaml_load
from iesplan.devices.parser2 import parse_device_model_v2

# 发布目录（计入 package-data，供外部扩展与契约测试复用）
FIXTURE_DIR = pathlib.Path(__file__).parent.parent / "iesplan" / "devices" / "samples" / "invalid" / "2.0"
VALID_SAMPLE = (
    pathlib.Path(__file__).parent.parent
    / "iesplan"
    / "devices"
    / "samples"
    / "acme.device.heat_pump.v2.device.yaml"
)

# 每个非法样例的稳定诊断期望：code + location.field + params.detail
# 均依据 parser2 实际 Diagnostic 结构（code=SYS-CFG-001，location.field 精确路径，detail 指向违规字段）。
EXPECTED: dict[str, dict[str, str]] = {
    "forbidden-device-version.yaml": {
        "code": "SYS-CFG-001",
        "field": "device",
        "detail_contains": "'version' was unexpected",
    },
    "forbidden-finance-cost.yaml": {
        "code": "SYS-CFG-001",
        "field": "",
        "detail_contains": "'finance_type' was unexpected",
    },
    "forbidden-fidelity.yaml": {
        "code": "SYS-CFG-001",
        "field": "device",
        "detail_contains": "'fidelity' was unexpected",
    },
    "unknown-top-field.yaml": {
        "code": "SYS-CFG-001",
        "field": "",
        "detail_contains": "'parameters' was unexpected",
    },
    "blind-with-source.yaml": {
        "code": "SYS-CFG-001",
        "field": "interfaces.blind_terminal.source",
        "detail_contains": "禁止声明 source",
    },
}


def _parse_file(path: pathlib.Path):
    text = path.read_text(encoding="utf-8")
    raw = cast(Mapping[str, Any], yaml_load(text))
    return parse_device_model_v2(raw, file=str(path))


def _diagnostic_tuples(result):
    out = []
    for d in result.diagnostics:
        detail = ""
        if isinstance(d.params, Mapping):
            detail = str(d.params.get("detail", "") or "")
        field = ""
        if isinstance(d.location, Mapping):
            field = str(d.location.get("field", "") or "")
        out.append((d.code, field, detail))
    return out


def test_valid_sample_still_parseable():
    assert VALID_SAMPLE.exists(), f"合法样例缺失: {VALID_SAMPLE}"
    raw = cast(Mapping[str, Any], yaml_load(VALID_SAMPLE.read_text(encoding="utf-8")))
    result = parse_device_model_v2(raw, file=str(VALID_SAMPLE))
    assert result.ok, f"合法样例应可解析，诊断: {[dict(d.params) for d in result.diagnostics]}"
    assert result.document is not None
    assert result.document.schema_version == "2.0.0"


@pytest.mark.parametrize("filename", sorted(EXPECTED.keys()))
def test_invalid_fixture_rejected_with_precise_diagnosis(filename):
    path = FIXTURE_DIR / filename
    assert path.exists(), f"非法样例缺失: {path}"
    exp = EXPECTED[filename]
    result = _parse_file(path)
    assert not result.ok, f"{filename} 应被拒绝，但解析成功"
    assert result.document is None, f"{filename} 非法时不得产出文档"
    assert result.diagnostics, f"{filename} 诊断为空"

    # 在全部诊断中寻找满足 code+field+detail 精确匹配的条目
    matched = False
    for code, field, detail in _diagnostic_tuples(result):
        if code != exp["code"]:
            continue
        # 字段精确匹配（空字段表示顶层附加属性，parser 以 "" 表示）
        if exp["field"] != field:
            continue
        if exp["detail_contains"] not in detail:
            continue
        # 同时要求 detail 非空、code 稳定
        assert detail, f"{filename} 诊断 detail 为空"
        matched = True
        break

    assert matched, (
        f"{filename} 诊断未精确命中 code={exp['code']!r} field={exp['field']!r} "
        f"detail_contains={exp['detail_contains']!r} 实际: {_diagnostic_tuples(result)}"
    )


def test_all_fixtures_present():
    files = {p.name for p in FIXTURE_DIR.glob("*.yaml")}
    assert files == set(EXPECTED.keys()), f"非法样例集不完整，实际: {files}, 期望: {set(EXPECTED.keys())}"
