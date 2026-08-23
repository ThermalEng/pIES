"""诊断模块单元测试:字段齐全、消息键推导、新码登记、序列化、深度不可变。"""

import json

import pytest

from iesplan.core.diagnostics import (
    DATA_TS_DUP,
    DATA_TS_LEAP,
    NEW_DIAG_CODES,
    SEVERITY_BLOCKING,
    SEVERITY_ERROR,
    SEVERITY_INFO,
    SEVERITY_WARNING,
    Diagnostic,
    make_diag,
)


class TestDiagnostic:
    def test_fields_present(self):
        d = Diagnostic(
            code=DATA_TS_DUP,
            severity=SEVERITY_ERROR,
            params={"series_name": "load", "count": 2},
        )
        assert d.code == "DATA-TS-001"
        assert d.severity == "error"
        assert d.blocking is False  # error 级不阻断
        assert d.message_key == "ies.diag.data.ts_dup"  # 自动推导
        assert d.fix_hint_key == "ies.fix.data.ts_dup"  # 自动推导
        assert d.params == {"series_name": "load", "count": 2}
        assert d.location is None
        assert d.ref_ids == ()
        assert d.occurred_at  # 产生时间自动填充

    def test_message_key_mapping(self):
        d = make_diag(DATA_TS_LEAP, severity="error")
        assert d.code == "DATA-TS-003"
        assert d.message_key == "ies.diag.data.ts_leap"

    def test_blocking_severity_consistency(self):
        d = make_diag("SEC-REG-001", severity=SEVERITY_BLOCKING)
        assert d.blocking is True
        d2 = make_diag(DATA_TS_DUP, severity=SEVERITY_WARNING)
        assert d2.blocking is False
        # blocking 独立可覆盖(与 severity 正交,04 §5.2)
        d3 = make_diag(DATA_TS_DUP, severity=SEVERITY_WARNING, blocking=True)
        assert d3.blocking is True

    def test_make_diag_all_kwargs(self):
        d = make_diag(
            DATA_TS_DUP,
            severity="error",
            params={"count": 3},
            location={"object_type": "time_series", "object_id": "ts.x", "field": "rows", "row": [1, 2]},
            ref_ids=["help.import.csv_duplicate_rows"],
            source="import.csv",
            trace_id="trc-abc",
            project_id="prj-1",
        )
        assert d.location["object_type"] == "time_series"
        assert d.ref_ids == ("help.import.csv_duplicate_rows",)
        assert d.source == "import.csv"

    def test_new_code_registered(self):
        """新码须在 NEW_DIAG_CODES 集中声明,否则拒绝。"""
        with pytest.raises(ValueError):
            make_diag("DATA-TS-999", severity="info")

    def test_registered_new_code_ok(self):
        d = make_diag("DATA-TS-004", severity="error", params={"expected": 8760, "actual": 8759})
        assert d.code in NEW_DIAG_CODES
        assert d.message_key == "ies.diag.data.ts_row_count"

    def test_invalid_severity(self):
        with pytest.raises(ValueError):
            make_diag(DATA_TS_DUP, severity="fatal")

    def test_to_dict_serializable(self):
        d = make_diag(
            DATA_TS_DUP,
            severity="error",
            params={"count": 2},
            location={"object_type": "time_series", "object_id": "ts.x", "field": "rows"},
        )
        data = d.to_dict()
        assert data["code"] == "DATA-TS-001"
        assert data["message_key"] == "ies.diag.data.ts_dup"
        assert set(data) >= {
            "code",
            "severity",
            "blocking",
            "message_key",
            "params",
            "location",
            "fix_hint_key",
            "ref_ids",
            "occurred_at",
        }
        # 全部字段 JSON 可序列化
        json.dumps(data)

    def test_severity_enum_values(self):
        assert SEVERITY_BLOCKING == "blocking"
        assert SEVERITY_ERROR == "error"
        assert SEVERITY_WARNING == "warning"
        assert SEVERITY_INFO == "info"

    def test_errors_module_integration(self):
        """errors.py 子类携带 code/severity/blocking。"""
        from iesplan.core.errors import ForbiddenError, NotFoundError

        e = ForbiddenError()
        assert e.code == "PERM-DENIED-001"
        assert e.severity == "blocking"
        assert e.blocking is True
        nf = NotFoundError()
        assert nf.code == "RES-MISS-003"
        assert nf.http_status == 404


class TestDiagnosticImmutability:
    """0.3.0 C1: Diagnostic 深度不可变收敛。"""

    def test_frozen_field_assignment_raises(self):
        d = make_diag(DATA_TS_DUP)
        with pytest.raises(Exception) as exc_info:
            d.code = "DATA-TS-002"
        assert type(exc_info.value).__name__ == "FrozenInstanceError"

    def test_all_scalar_fields_frozen(self):
        d = make_diag(DATA_TS_DUP, params={"a": 1}, ref_ids=["x"])
        for name in (
            "severity",
            "blocking",
            "message_key",
            "fix_hint_key",
            "occurred_at",
            "source",
            "trace_id",
            "project_id",
            "task_id",
            "suppressed",
            "params",
            "ref_ids",
        ):
            with pytest.raises(Exception):
                setattr(d, name, None)

    def test_params_is_readonly_mapping(self):
        d = make_diag(DATA_TS_DUP, params={"count": 2})
        assert isinstance(d.params, type(__import__("types").MappingProxyType({})))
        with pytest.raises(TypeError):
            d.params["count"] = 3  # type: ignore[index]

    def test_ref_ids_is_immutable_tuple(self):
        d = make_diag(DATA_TS_DUP, ref_ids=["a", "b"])
        assert isinstance(d.ref_ids, tuple)
        with pytest.raises(AttributeError):
            d.ref_ids.append("c")  # type: ignore[attr-defined]

    def test_location_is_readonly_mapping(self):
        d = make_diag(DATA_TS_DUP, location={"object_type": "time_series"})
        with pytest.raises(TypeError):
            d.location["object_type"] = "device"  # type: ignore[index]

    def test_constructor_containers_copied(self):
        """传入的 dict/list 在构造时被复制冻结, 后续修改外部对象不影响诊断。"""
        params = {"count": 1}
        ref_ids = ["a"]
        d = make_diag(DATA_TS_DUP, params=params, ref_ids=ref_ids)
        params["count"] = 999
        ref_ids.append("b")
        assert d.params == {"count": 1}
        assert d.ref_ids == ("a",)

    def test_to_dict_json_serializable_and_detached(self):
        d = make_diag(
            DATA_TS_DUP,
            params={"count": 2},
            location={"object_type": "time_series"},
            ref_ids=["r1"],
        )
        data = d.to_dict()
        payload = json.dumps(data, ensure_ascii=False)  # MappingProxyType/tuple 不入库
        assert json.loads(payload)["ref_ids"] == ["r1"]
        # to_dict 返回副本: 修改返回值不影响诊断对象
        data["params"]["count"] = 42
        data["ref_ids"].append("x")
        assert d.params == {"count": 2}
        assert d.ref_ids == ("r1",)

    def test_replace_creates_new_instance(self):
        import dataclasses

        d = make_diag(DATA_TS_DUP, project_id="")
        d2 = d.replace(project_id="prj-1", blocking=True)
        assert d2 is not d
        assert d.project_id == ""
        assert d2.project_id == "prj-1"
        assert d2.blocking is True
        assert d2.code == d.code
        assert d2.message_key == d.message_key
        # dataclasses.replace 与 replace 方法等价
        d3 = dataclasses.replace(d2, task_id="t-9")
        assert d3.task_id == "t-9" and d2.task_id == ""

    def test_with_context_fills_empty_fields_only(self):
        d = make_diag(DATA_TS_DUP)
        d2 = d.with_context(project_id="prj-1", task_id="t-1", trace_id="tr-1", source="unit")
        assert (d2.project_id, d2.task_id, d2.trace_id, d2.source) == ("prj-1", "t-1", "tr-1", "unit")
        # 已有字段不被静默覆盖
        d3 = d2.with_context(project_id="prj-2", task_id="t-2")
        assert d3.project_id == "prj-1"
        assert d3.task_id == "t-1"
        # 无新增上下文 → 返回等价对象
        d4 = d2.with_context()
        assert d4 == d2
        assert d4 is not d2

    def test_frozen_equality(self):
        d1 = make_diag(DATA_TS_DUP, params={"a": 1})
        # occurred_at 默认取构造时刻, 派生对象继承原值 → 同一诊断的派生副本相等
        d2 = d1.replace(blocking=True)
        assert d2 != d1  # blocking 不同
        d3 = d1.replace(blocking=False)
        assert d3 == d1
        assert d3 is not d1
        assert d1 != make_diag(DATA_TS_DUP, params={"a": 2})
