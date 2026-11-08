"""Docker-only public generate→checkpoint→restore→runtime-preparation measurement.

No solver is started. Run each size/repetition in a fresh, equally limited container.
Only the implementation in the mounted checkout is measured; there is no baseline mode.
For old/new comparisons, run each actual implementation separately and record its
Git commit, any working-tree changes, command, inputs and container limits with the output.
"""
from __future__ import annotations

import argparse
import gc
import json
import os
import resource
import tempfile
import time
from dataclasses import replace
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.orm import Session
from test_highs_milp_provider import _campus_inputs
from worker_testkit import claim_for_direct_run, setup_environment

from iesplan.application.worker import restore_round, save_bundle_checkpoint
from iesplan.bootstrap import collect_immutable_tables
from iesplan.computation import (
    HIGHS_MILP_PROVIDER,
    AttemptContext,
    DeploymentPolicy,
    ExecutorDescriptor,
    SolverRuntime,
    build_executor_registry,
)
from iesplan.core.contracts import horizon_point_count
from iesplan.db import Base


class Prepared(Exception):
    pass


class PreparationExecutor:
    descriptor = ExecutorDescriptor(
        executor_id="ies.executor.local_process", version="1.0.0",
        executables=("ies.solver.highs@1.7.2",),
    )
    ref = descriptor.ref

    def __init__(self):
        self.input_bytes = 0

    def available(self):
        return True

    def execute(self, command, workdir, *, stop=None):
        self.input_bytes = sum(path.stat().st_size for path in Path(workdir, "input").rglob("*")
                               if path.is_file())
        raise Prepared("runtime inputs copied; no solver launched")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--years", type=int)
    source.add_argument("--snapshot-id", type=int)
    parser.add_argument("--project-id", type=int, default=1)
    args = parser.parse_args()
    timings = {}
    stages = {}
    gc_started = {}
    gc_times = []

    def collection(phase, info):
        generation = info["generation"]
        if phase == "start":
            gc_started[generation] = time.perf_counter()
        else:
            gc_times.append(time.perf_counter() - gc_started.pop(generation))

    def measured(name, function):
        start = time.perf_counter()
        result = function()
        timings[name] = time.perf_counter() - start
        stages[name] = {
            "process_peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "process_resident_bytes": (int(Path("/proc/self/statm").read_text().split()[1])
                                       * os.sysconf("SC_PAGE_SIZE")),
        }
        return result

    if args.snapshot_id is not None:
        from iesplan.application import worker
        from iesplan.db import SessionLocal

        with SessionLocal() as source_db:
            snapshot = worker.get_snapshot_record(source_db, args.snapshot_id)
            inputs = list(worker.load_compute_inputs(source_db, args.project_id, snapshot))
    else:
        inputs = list(_campus_inputs())
        materialized, evaluation = inputs[2], inputs[-1]
        baseline = json.loads(inputs[0].canonical_text)["project_baseline"]
        points = horizon_point_count(baseline["resolution"], baseline["start_year"], args.years)

        def repeated(item):
            values = (item.values * ((points + len(item.values) - 1) // len(item.values)))[:points]
            return replace(item, point_count=points, values=values)

        inputs[2] = replace(materialized, operation_years=args.years,
                            series=tuple(map(repeated, materialized.series)),
                            prices=tuple(map(repeated, materialized.prices)))
        inputs[-1] = replace(evaluation, operation_years=args.years, evaluation_years=args.years)
    collect_immutable_tables()
    engine = sa.create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with tempfile.TemporaryDirectory(prefix="pies-content-measure-") as folder, Session(engine) as db:
        env = setup_environment(db, Path(folder), with_dataset=False)
        gc.callbacks.append(collection)
        started = time.perf_counter()
        try:
            bundle = measured("generate", lambda: HIGHS_MILP_PROVIDER.generate(*inputs))
            claim = claim_for_direct_run(db, env["task"].id, "content-measure")
            context = AttemptContext(attempt_id=str(claim.attempt_id), bundle_id="measurement",
                                     assembly_id="public-campus", calculation_config_id="measurement")
            _, descriptors = measured("persist_generated", lambda: save_bundle_checkpoint(
                db, claim, 1, bundle, context))
            file_bytes = sum(item["size_bytes"] for item in descriptors.values())
            bundle = None
            # Same short-session recovery API as a restarted Worker.
            with Session(engine) as restore_db:
                saved = measured("restore", lambda: restore_round(restore_db, claim, 1))
            executor = PreparationExecutor()
            policy = DeploymentPolicy(
                executables=executor.descriptor.executables,
                max_timeout_seconds=saved.bundle.manifest.command["timeout_seconds"],
                max_memory_mib=8192, max_cpu_cores=2, max_output_files=10,
                max_output_bytes=1024 * 1024,
            )
            runtime = SolverRuntime(registry=build_executor_registry([executor]), policy=policy)

            def prepare():
                try:
                    runtime.run(saved.bundle, context)
                except Prepared:
                    return
                raise AssertionError("preparation-only executor was not called")

            measured("runtime_prepare", prepare)
            assert executor.input_bytes == file_bytes
            total = time.perf_counter() - started
        finally:
            gc.callbacks.remove(collection)
    engine.dispose()
    cgroup = Path("/sys/fs/cgroup")

    def metric(name):
        path = cgroup / name
        return int(path.read_text()) if path.exists() else None

    print(json.dumps({
        "scope": "frozen input" if args.snapshot_id is not None else "public campus",
        "stages": "generate→persist→release→restore→runtime input copy; no solve",
        "implementation": "content_records",
        "years": args.years, "timings_seconds": timings, "total_seconds": total,
        "stage_resources": stages,
        "persisted_input_bytes": file_bytes,
        "process_peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "cgroup_memory_peak_bytes": metric("memory.peak"),
        "cgroup_swap_current_bytes": metric("memory.swap.current"),
        "cgroup_swap_peak_bytes": metric("memory.swap.peak"),
        "gc_seconds": sum(gc_times), "gc_max_pause_seconds": max(gc_times, default=0),
        "gc_collections": len(gc_times),
        "instrumentation": "boundary timers and GC callbacks, no tracemalloc",
    }, sort_keys=True))


if __name__ == "__main__":
    main()
