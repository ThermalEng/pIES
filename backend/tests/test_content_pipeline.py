"""M3/M4: bounded content, positional records and no full-result copies."""
from __future__ import annotations

import io
import json
import weakref
import zipfile
from array import array
from functools import partial

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from iesplan.application.packages.archive import ArchiveBuilder
from iesplan.application.worker.compute_cases import build_hourly_files
from iesplan.computation import ComputeResult
from iesplan.computation.errors import ComputationUnavailableError
from iesplan.computation.generators.matrix import SparseModel
from iesplan.computation.generators.parameters import parameter_chunks, parameter_records
from iesplan.computation.result_adapters.declarations import recover_declarations
from iesplan.config import settings
from iesplan.core.content import (
    CHUNK_BYTES,
    FrozenSequence,
    GeneratedContent,
    freeze_owned_json,
    json_chunks,
    read_content,
    text_lines,
)
from iesplan.db import Base
from iesplan.storage import ObjectCorruptError, get_object_content, put_content


@pytest.mark.parametrize("ending", ["\n", "\r\n", "\r"])
def test_utf8_and_newlines_across_every_byte_boundary(ending):
    raw = ending.join(["光伏🙂", "", "末行"]).encode()
    source = GeneratedContent(lambda: (raw[n:n + 1] for n in range(len(raw))))
    assert list(text_lines(source)) == ["光伏🙂", "", "末行"]
    assert list(text_lines(source)) == ["光伏🙂", "", "末行"]


def test_streamed_json_shares_frozen_array_and_keeps_public_serialization():
    values = array("d", [0.0, -0.0, 1.0 / 3.0] * 10000)
    reference = weakref.ref(values)
    outputs = freeze_owned_json({"series": {"a": {
        "instance_id": "dev", "variable_id": "flow", "unit": "kW", "values": values,
    }}, "nested": ({"x": 1},)})
    del values
    result = ComputeResult(bundle_id="bundle", receipt_id="receipt",
                           execution_status="completed", math_status="solved", outputs=outputs)
    assert result.outputs is outputs
    assert reference() is not None
    assert isinstance(outputs["series"]["a"]["values"], FrozenSequence)
    with pytest.raises(TypeError):
        outputs["series"]["a"]["values"][0] = 2
    with pytest.raises(TypeError):
        outputs["nested"][0]["x"] = 2
    view = result.to_mapping()
    assert view["outputs"] is result.outputs
    chunks = list(json_chunks(view))
    assert max(map(len, chunks)) <= CHUNK_BYTES
    assert json.loads(b"".join(chunks)) == result.to_dict()
    hourly, _media = build_hourly_files(result.outputs)["hourly/series.json"]
    assert json.loads(read_content(hourly))["data"]["a"] == list(reference())
    assert read_content(hourly) == read_content(hourly)


def test_json_batches_preserve_nested_boundaries_and_exact_public_bytes():
    value = {"items": [{"name": f"列-{index}", "value": -0.0} for index in range(1025)],
             "mixed": [None, {"nested": [1, "a|b"]}, []],
             "array": array("d", [0.0, 1.0 / 3.0] * 513)}
    public = {**value, "array": list(value["array"])}
    expected = json.dumps(public, ensure_ascii=False, sort_keys=True, allow_nan=False,
                          separators=(",", ":")).encode()
    assert b"".join(json_chunks(freeze_owned_json(value))) == expected


def test_storage_content_outlives_session_and_failed_write_publishes_nothing(tmp_path, monkeypatch):
    from iesplan.bootstrap import collect_immutable_tables

    collect_immutable_tables()
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    engine = sa.create_engine("sqlite://")
    Base.metadata.create_all(engine)
    total = 0

    def chunks():
        nonlocal total
        for index in range(32):
            part = bytes([index]) * CHUNK_BYTES
            total += len(part)
            yield part

    with Session(engine) as db:
        handle = put_content(db, GeneratedContent(chunks), "application/octet-stream", "test")
        db.commit()
        stored = get_object_content(db, handle.id)
        assert handle.size_bytes == total == 32 * CHUNK_BYTES
    # No DB or open file handle is retained in the reader.
    assert [chunk[0] for chunk in stored.iter_chunks()] == list(range(32))
    assert [chunk[0] for chunk in stored.iter_chunks()] == list(range(32))
    before = set((tmp_path / "objects").iterdir())

    def interrupted():
        yield b"partial"
        raise OSError("actual write source failure")

    with Session(engine) as db, pytest.raises(OSError, match="actual write"):
        put_content(db, GeneratedContent(interrupted), "application/octet-stream", "test")
    assert set((tmp_path / "objects").iterdir()) == before
    assert not list((tmp_path / "objects/tmp").iterdir())
    # Exercise the real read error, not an integrity/size precheck.
    (tmp_path / "objects" / handle.oid).unlink()
    with pytest.raises(ObjectCorruptError):
        list(stored.iter_chunks())
    engine.dispose()


def test_storage_reconcile_uses_metadata_without_reading_content(tmp_path, monkeypatch):
    from iesplan.bootstrap import collect_immutable_tables
    from iesplan.storage import reconcile
    from iesplan.storage.adapters.filesystem import FileSystemBlobStore

    collect_immutable_tables()
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    engine = sa.create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        handle = put_content(db, GeneratedContent(lambda: iter((b"kept",))), "text/plain", "test")
        db.commit()
        # A sparse large orphan exercises metadata size without allocating its contents.
        orphan = tmp_path / "objects" / ("a" * 64)
        with orphan.open("wb") as target:
            target.truncate(3 * 1024 ** 3)

        def unreadable(*_args):
            raise AssertionError("reconcile must not read object contents")

        monkeypatch.setattr(FileSystemBlobStore, "get_content", unreadable)
        report = reconcile(db, dry_run=True)
        assert report["orphan_reported"] == [{"oid": orphan.name, "size_bytes": 3 * 1024 ** 3}]
        assert report["corrupt_reported"] == []
        assert reconcile(db, dry_run=False)["orphan_registered"] == [f"objects/{orphan.name}"]
        (tmp_path / "objects" / handle.oid).unlink()
        assert reconcile(db)["corrupt_reported"] == [
            {"object_id": handle.id, "oid": handle.oid, "reason": "missing_file"}]
    engine.dispose()


def test_parameter_records_keep_solver_positions_identity_and_sum_order():
    from test_compact_final_matrix import _public_model

    model = SparseModel.from_public(_public_model())
    # Same frozen terms are shared; merely numerically equal terms are not interned.
    terms = {"fixed": 1e16, "x": -1e16, "z": 1.0}
    node = {"constant": 0.0, "terms": terms}
    other = {"constant": 0.0, "terms": dict(terms)}
    components = {"a": dict(node, unit="CNY", caliber="cash"),
                  "b": dict(node, unit="CNY", caliber="cash"),
                  "c": dict(other, unit="CNY", caliber="cash")}
    series = ({"series_id": "state", "instance_id": "dev", "variable_id": "s",
               "unit": "1", "columns": ("z", "x", "fixed")},)
    facts = {"amount": node, "same": node}
    sources = {"groups": [], "sources": {}, "outputs": []}
    units = {"fixed": "1", "x": "1", "z": "1"}
    records = list(parameter_records(model, units, components, series, facts, sources))
    assert not any(row[0] in ("epigraph", "epigraph_flows", "epigraph_end") for row in records)
    assert len([row for row in records if row[0] == "expression"]) == 2
    references = [row[-1] for row in records if row[0] == "component"]
    assert references == [0, 0, 1]
    positions = next(row[1] for row in records if row[0] == "positions")
    assert positions == [5, 4, 0]  # lexical solver order differs from declarations
    primals = array("d", [1.0] * len(model.columns))

    def fail(message):
        raise AssertionError(message)

    result = recover_declarations(GeneratedContent(partial(
        parameter_chunks, model, units, components, series, facts, sources)), primals, fail)
    assert result["components"]["a"]["constant"] == 1.0  # left-to-right, not reassociated
    assert result["financial_facts"] == {"amount": 1.0, "same": 1.0}
    assert list(result["series"]["state"]["values"]) == [1.0, 1.0, 1.0]
    assert [entry["name"] for entry in result["variables"]] == list(units)
    assert result["epigraph_inflated"] is False


def test_parameter_records_epigraph_tightness_check():
    from test_compact_final_matrix import _public_model

    model = SparseModel.from_public(_public_model())
    epigraphs = (("fixed", ("z", "x")),)
    records = list(parameter_records(
        model, {"fixed": "1"}, {}, (), {}, {"groups": []}, epigraphs=epigraphs,
    ))
    tags = [row[0] for row in records]
    assert ["epigraph", "epigraph_flows", "epigraph_end"] == [
        tag for tag in tags if tag.startswith("epigraph")
    ]
    pos = {name: model.column_position(name) for name in ("fixed", "z", "x")}
    assert next(row for row in records if row[0] == "epigraph") == ["epigraph", pos["fixed"]]
    assert next(row for row in records if row[0] == "epigraph_flows") == [
        "epigraph_flows", [pos["z"], pos["x"]],
    ]

    def fail(message):
        raise AssertionError(message)

    content = GeneratedContent(partial(
        parameter_chunks, model, {"fixed": "1"}, {}, (), {}, {"groups": []}, epigraphs=epigraphs,
    ))
    tight = array("d", [0.0] * len(model.columns))
    tight[pos["x"]] = 5.0
    tight[pos["fixed"]] = 5.0 + 1e-9
    for include_solution in (True, False):
        res = recover_declarations(content, tight, fail, include_solution=include_solution)
        assert res["epigraph_inflated"] is False

    inflated = array("d", tight)
    inflated[pos["fixed"]] = 6.0
    for include_solution in (True, False):
        res = recover_declarations(content, inflated, fail, include_solution=include_solution)
        assert res["epigraph_inflated"] is True


def test_parameter_record_emission_cooperates_with_existing_stop_probe():
    from test_compact_final_matrix import _public_model

    model = SparseModel.from_public(_public_model())
    with pytest.raises(ComputationUnavailableError) as error:
        list(parameter_chunks(model, {}, {}, (), {}, {"groups": []}, stop=lambda: "cancelled"))
    assert error.value.reason == "cancelled"


def test_archive_streams_entries_and_is_byte_repeatable():
    builder = ArchiveBuilder()
    with builder as archive:
        archive.writestr("small.json", b'{"ok":true}')
        archive.writestr("large.bin", GeneratedContent(lambda: (
            bytes([index]) * CHUNK_BYTES for index in range(20)
        )))
    source = builder.content()
    raw = read_content(source)
    assert raw == read_content(source)
    assert max(map(len, source.iter_chunks())) <= CHUNK_BYTES
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        assert archive.read("small.json") == b'{"ok":true}'
        assert archive.read("large.bin") == b"".join(
            bytes([index]) * CHUNK_BYTES for index in range(20))
