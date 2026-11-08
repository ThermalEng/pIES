"""Ordinary synthetic result-size measurement; no claim of a solved engineering case.

Run each count/repetition in a fresh Docker container. The synthetic primals are
known constants; only adaptation, publication and archive costs are measured.
"""
from __future__ import annotations

import argparse
import gc
import io
import json
import resource
import tempfile
import time
import zipfile
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.orm import Session
from test_highs_result_adapter import _bundle, _facts, _receipt
from worker_testkit import setup_environment

from iesplan.application.worker.compute_cases import build_hourly_files, seal_output_objects
from iesplan.bootstrap import collect_immutable_tables
from iesplan.computation import SolverBundle
from iesplan.computation.result_adapters.highs import HIGHS_ADAPTER
from iesplan.db import Base
from iesplan.storage import get_object, put_object


@dataclass(frozen=True)
class FileContent:
    filename: Path

    def iter_chunks(self):
        with self.filename.open("rb") as source:
            while chunk := source.read(65536):
                yield chunk


def inputs(count, folder, baseline):
    names = [f"c{index:07d}" for index in range(count)]
    amount = {"constant": 0.0, "terms": dict.fromkeys(names, 1.0)}
    facts = _facts()
    facts["construction_total"] = amount
    components = {"energy": dict(amount, unit="CNY", caliber="period")}
    units = dict.fromkeys(names, "1")
    series = [{"series_id": "dev.flow", "instance_id": "dev", "variable_id": "flow",
               "unit": "1", "columns": names}]
    sources = {"schema": "ies.model-sources", "schema_version": "1.0.0",
               "sources": {}, "groups": [], "outputs": []}
    header = {"objective_offset": 0.0, "objective_unit": "CNY", "outputs": ["solution", "metrics"]}
    raw_solution = ("HIGHS_SOLUTION_V1\nmodel_status: HighsModelStatus.kOptimal\n"
                    "has_feasible_solution: true\n"
                    f"objective_value: {float(count)}\nbest_bound: {float(count)}\nrelative_gap: 0.0\n"
                    f"num_columns: {count}\nprimal:\n")
    solution_path = folder / "solution.sol"
    with solution_path.open("w") as target:
        target.write(raw_solution)
        target.writelines(f"{index}: 1.0\n" for index in range(count))
    if baseline:
        metadata = dict(header, unit_map=units, components=components, step_series=series,
                        system_series=[], financial_facts=facts, model_sources=sources)
        problem = ("NAME TEST\nROWS\n N OBJ\nCOLUMNS\n"
                   + "".join(f" {name} OBJ 1\n" for name in names)
                   + "RHS\nBOUNDS\nENDATA\n").encode()
        files = {"input/parameters.json": json.dumps(metadata, sort_keys=True).encode(),
                 "input/problem.mps": problem}
        solution = solution_path.read_bytes()
    else:
        from iesplan.computation.generators.parameters import RECORDS_PATH, parameter_chunks
        from iesplan.core.content import MemoryContent

        class Positions:
            def column_names(self):
                return iter(names)

            def row_names(self):
                return iter(())

            def column_position(self, name):
                return int(name[1:])

        record_path = folder / "parameters.jsonl"
        with record_path.open("wb") as target:
            for chunk in parameter_chunks(Positions(), units, components, series, facts, sources):
                target.write(chunk)
        files = {"input/parameters.json": MemoryContent(json.dumps(
                     dict(header, column_count=count, records=RECORDS_PATH)).encode()),
                 "input/problem.mps": MemoryContent(b"not read by result adapter"),
                 RECORDS_PATH: FileContent(record_path)}
        solution = FileContent(solution_path)
    manifest = replace(_bundle().manifest, inputs=[
        {"path": name, "media_type": "application/octet-stream"} for name in files
    ])
    return SolverBundle(manifest, files), {"output/solution.sol": solution}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, required=True)
    parser.add_argument("--baseline-bytes", action="store_true")
    args = parser.parse_args()
    timings, pauses = {}, []
    gc_start = {}

    def collection(phase, info):
        generation = info["generation"]
        if phase == "start":
            gc_start[generation] = time.perf_counter()
        else:
            pauses.append(time.perf_counter() - gc_start.pop(generation))

    def measured(name, function):
        start = time.perf_counter()
        answer = function()
        timings[name] = time.perf_counter() - start
        return answer

    collect_immutable_tables()
    engine = sa.create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with tempfile.TemporaryDirectory(prefix="pies-result-measure-") as folder, Session(engine) as db:
        root = Path(folder)
        setup_environment(db, root, with_dataset=False)
        bundle, outputs = inputs(args.count, root, args.baseline_bytes)
        gc.callbacks.append(collection)
        try:
            result = measured("adapt", lambda: HIGHS_ADAPTER.adapt(bundle, _receipt(), outputs))
            assert result.outputs["solution"]["components"]["energy"]["value"] == args.count
            assert result.outputs["financial_facts"]["construction_total"] == str(float(args.count))
            if args.baseline_bytes:
                payload = measured("project_result", result.to_dict)
                blob = measured("serialize", lambda: json.dumps(
                    payload, sort_keys=True, separators=(",", ":")).encode())
                handle = measured("persist_result", lambda: put_object(db, blob, "application/json", "test"))
            else:
                from iesplan.core.content import GeneratedContent, json_chunks
                from iesplan.storage import get_object_content, put_content

                payload = measured("project_result", result.to_mapping)
                handle = measured("persist_result", lambda: put_content(db, GeneratedContent(
                    partial(json_chunks, payload)), "application/json", "test"))
            hourly = measured("persist_hourly", lambda: seal_output_objects(
                db, build_hourly_files(result.outputs), actor_id=None))

            def archive():
                if args.baseline_bytes:
                    target = io.BytesIO()
                    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as package:
                        package.writestr("result.json", get_object(db, handle.id))
                    return len(target.getvalue())
                from iesplan.application.packages.archive import ArchiveBuilder

                builder = ArchiveBuilder()
                builder.writestr("result.json", get_object_content(db, handle.id))
                return sum(map(len, builder.content().iter_chunks()))

            archive_bytes = measured("archive", archive)
        finally:
            gc.callbacks.remove(collection)
        print(json.dumps({
            "scope": "synthetic constant primals; adapter→publication→archive; no solve",
            "implementation": "baseline_bytes" if args.baseline_bytes else "content_records",
            "columns": args.count, "timings_seconds": timings,
            "process_peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "cgroup_memory_peak_bytes": int(Path("/sys/fs/cgroup/memory.peak").read_text()),
            "cgroup_swap_peak_bytes": int(Path("/sys/fs/cgroup/memory.swap.peak").read_text()),
            "gc_seconds": sum(pauses), "gc_max_pause_seconds": max(pauses, default=0),
            "gc_collections": len(pauses), "result_bytes": handle.size_bytes,
            "hourly_bytes": sum(item["size_bytes"] for item in hourly.values()),
            "archive_bytes": archive_bytes, "instrumentation": "ordinary run; no tracemalloc",
        }, sort_keys=True))
    engine.dispose()


if __name__ == "__main__":
    main()
