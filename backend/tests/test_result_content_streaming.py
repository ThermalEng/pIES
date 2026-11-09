"""Read/export frozen results without materializing their complete solver tree."""

import io
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from openpyxl import Workbook, load_workbook

from iesplan.application.packages import report_series
from iesplan.application.results import content as result_content
from iesplan.application.results import writes
from iesplan.core.content import GeneratedContent, MemoryContent, freeze_owned_json, json_chunks
from iesplan.core.errors import AppError
from iesplan.core.jsonstream import ObjectFields, project_json, project_value


def document():
    return {
        "schema_version": "1.0.0",
        "financial_evaluation": {"npv": "12.34567890123456789"},
        "metrics": {"objective": 12.5},
        "compute_result": {
            "status": "succeeded",
            "outputs": {
                "solver_summary": {"math_status": "solved", "relative_gap": 0.0},
                "solution": {
                    "objective": 12.5,
                    "variables": [
                        {"name": "step[0]", "value": 3, "unit": "kW"},
                        {"name": '规划.量"', "value": 4.25, "unit": "kWh"},
                    ],
                },
                "step_series": {"bulk": [1, 2, 3]},
                "financial_facts": {"bulk": [4, 5, 6]},
            },
        },
    }


@pytest.mark.parametrize("wrapped", [False, True])
@pytest.mark.parametrize("chunk_size", [1, 7, 65536])
def test_projection_is_key_safe_and_matches_owned_tree(wrapped, chunk_size):
    payload = {"content": document()} if wrapped else document()
    raw = json.dumps(payload, ensure_ascii=False).encode()
    content = GeneratedContent(lambda: (raw[i : i + chunk_size] for i in range(0, len(raw), chunk_size)))
    selection = result_content.evidence_selection(['规划.量"'])
    projected = project_json(content, selection)
    assert projected == project_value(payload, selection)
    inner = projected["content"] if wrapped else projected
    assert inner["financial_evaluation"]["npv"] == "12.34567890123456789"
    outputs = inner["compute_result"]["outputs"]
    assert set(outputs) == {"solution", "solver_summary"}
    assert outputs["solution"]["variables"] == [{"name": '规划.量"', "value": 4.25, "unit": "kWh"}]


def test_existing_evidence_streams_and_cached_projection_does_not_read_raw(monkeypatch):
    payload = document()
    package = SimpleNamespace(id=10, object_id=20, calc_snapshot_id=None)
    monkeypatch.setattr(result_content, "find_refs_by_owner", lambda *a: [])
    monkeypatch.setattr(
        result_content, "get_object_content", lambda *a: MemoryContent(json.dumps(payload).encode())
    )
    monkeypatch.setattr(result_content, "get_object", lambda *a: pytest.fail("full evidence read"))
    assert writes.evidence_content(None, package)["compute_result"]["outputs"]["solution"]["variables"] == []
    projected = project_value(payload, result_content.evidence_selection(()))
    monkeypatch.setattr(result_content, "find_refs_by_owner", lambda *a: [{"object_id": 30}])
    monkeypatch.setattr(result_content, "get_object_content", lambda *a: pytest.fail("raw evidence scan"))
    monkeypatch.setattr(
        result_content, "get_object", lambda db, oid: json.dumps(projected).encode() if oid == 30 else None
    )
    assert writes.evidence_content(None, package) == projected


def hourly_document():
    keys = ['z.功率"', "a_energy"]
    return {
        "data": {key: [i * 0.125 - n for i in range(8200)] for n, key in enumerate(keys)},
        "meta": {
            "units": {key: "kW" for key in keys},
            "series": {
                key: {
                    "series_id": key,
                    "unit": "kW",
                    "kind": "device_step",
                    "instance_id": "设备",
                    "variable_id": key,
                }
                for key in keys
            },
        },
    }


@pytest.mark.parametrize(
    "start,end,limit,expected_end",
    [(4094, 4100, 3, 4097), (8198, None, 9, 8200), (9000, None, 9, 9000), (7, 7, 9, 7)],
)
def test_hourly_reads_only_requested_window(monkeypatch, start, end, limit, expected_end):
    doc = hourly_document()
    monkeypatch.setattr(writes, "get_object_content", lambda *a: MemoryContent(json.dumps(doc).encode()))
    field = 'z.功率"'
    content = {"hourly_refs": [{"object_id": 1, "fields": list(doc["data"])}]}
    result = writes.read_hourly(None, content, field, start, end, limit)
    assert result["values"] == doc["data"][field][start:expected_end]
    assert result["total_rows"] == 8200
    assert result["instance_id"] == "设备"
    assert result["unit"] == "kW"
    assert result["start"] == start and result["end"] == expected_end
    expected_next = expected_end if expected_end < (8200 if end is None else end) else None
    assert result["next_start"] == expected_next


def test_excel_transpose_crosses_pages_and_keeps_all_values(monkeypatch):
    doc = hourly_document()
    monkeypatch.setattr(
        report_series, "get_object_content", lambda *a: MemoryContent(json.dumps(doc).encode())
    )
    wb = Workbook(write_only=True)
    report_series.write_hourly_sheets(None, wb, [{"object_id": 1}])
    out = io.BytesIO()
    wb.save(out)
    result = load_workbook(io.BytesIO(out.getvalue()), read_only=True, data_only=True)
    rows = iter(result["步序列值"].values)
    ordered = sorted(doc["data"])
    assert next(rows) == ("step", *ordered)
    for step in range(8200):
        assert next(rows) == (step, *(doc["data"][key][step] for key in ordered))
    assert next(rows, None) is None
    assert list(result["步序列目录"].values)[1][-1] == 8200
    result.close()


def test_stream_parse_failure_remains_visible(monkeypatch):
    monkeypatch.setattr(result_content, "find_refs_by_owner", lambda *a: [])
    monkeypatch.setattr(result_content, "get_object_content", lambda *a: MemoryContent(b'{"metrics":['))
    with pytest.raises(AppError) as caught:
        writes.evidence_content(None, SimpleNamespace(id=1, object_id=2, calc_snapshot_id=None))
    assert caught.value.code == "SYS-STORE-004"


def test_frozen_financial_facts_remain_objects_in_stored_projection():
    payload = freeze_owned_json(document())
    projected = project_value(payload, result_content.evidence_selection(['规划.量"']))
    decoded = json.loads(b"".join(json_chunks(projected)))
    assert decoded == project_json(
        MemoryContent(json.dumps(document()).encode()), result_content.evidence_selection(['规划.量"'])
    )


def test_explicit_index_publishes_only_projection_and_is_repeatable(monkeypatch):
    package = SimpleNamespace(id=10, object_id=20, calc_snapshot_id=None)
    db = Mock()
    refs = []
    saved = []
    monkeypatch.setattr(result_content, "get_evidence", lambda *a: package)
    monkeypatch.setattr(result_content, "find_refs_by_owner", lambda *a: refs)
    monkeypatch.setattr(
        result_content, "get_object_content", lambda *a: MemoryContent(json.dumps(document()).encode())
    )

    def save(db, content, media_type, **kwargs):
        saved.append(json.loads(b"".join(content.iter_chunks())))
        assert media_type == "application/json" and kwargs["source_category"] == "evidence_projection"
        return SimpleNamespace(id=30)

    monkeypatch.setattr(result_content, "put_content", save)
    monkeypatch.setattr(result_content, "add_ref", lambda *a, **kw: refs.append({"object_id": 30}))
    assert result_content.index_evidence_projection(db, 10) == 30
    assert result_content.index_evidence_projection(db, 10) == 30
    assert len(saved) == 1
    assert saved[0] == project_value(document(), result_content.evidence_selection(()))
    db.commit.assert_called_once()
    db.rollback.assert_not_called()


def test_skipped_large_arrays_are_not_materialized():
    def chunks():
        yield b'{"bulk":['
        block = b"0," * 32768
        for _ in range(256):
            yield block
        yield b'1],"summary":{"value":42}}'

    assert project_json(GeneratedContent(chunks), ObjectFields({"summary": True})) == {
        "summary": {"value": 42}
    }
