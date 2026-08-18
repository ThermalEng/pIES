"""诊断模块单元测试:字段齐全、消息键推导、新码登记、序列化。"""

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
        assert d.ref_ids == []
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
        assert d.ref_ids == ["help.import.csv_duplicate_rows"]
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
        import json

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
