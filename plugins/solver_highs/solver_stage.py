"""Official built-in `ies.solver.highs@1.7.2` stage plugin entrypoint."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from pies_sdk import StageContext, check_relative_path, read_solver_bundle, write_json_file

HIGHS_EXECUTABLE_PATH = "/usr/local/bin/ies-highs-solver"
_OUTPUT_SOL_NAME = "solution.sol"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _rewrite_arguments(raw_args: list[str], sol_rel_path: str, threads: int | None) -> list[str]:
    """Rewrite `--solution_file` to point at `output/<solution.*>` while preserving solver args."""
    args = list(raw_args)
    rewritten: list[str] = []
    idx = 0
    saw_sol = False
    saw_threads = False
    while idx < len(args):
        token = str(args[idx])
        if token == "--solution_file" and idx + 1 < len(args):
            rewritten.extend(["--solution_file", sol_rel_path])
            saw_sol = True
            idx += 2
            continue
        if token == "--threads":
            saw_threads = True
        rewritten.append(token)
        idx += 1
    if not saw_sol:
        rewritten.extend(["--solution_file", sol_rel_path])
    if threads is not None and not saw_threads:
        rewritten.extend(["--threads", str(threads)])
    return rewritten


def solve(ctx: StageContext) -> None:
    """Execute `/usr/local/bin/ies-highs-solver` against read-only `/work/in/bundle/`."""
    manifest, bundle_dir = read_solver_bundle(ctx.work_in)
    command = manifest["command"]
    raw_args = [str(item) for item in command["arguments"]]

    outputs = manifest.get("outputs") or []
    sol_name = _OUTPUT_SOL_NAME
    if outputs:
        declared = check_relative_path(outputs[0]["path"], "bundle.outputs[0].path")
        candidate = Path(declared).name
        if candidate.startswith("solution."):
            sol_name = candidate

    threads_raw = ctx.parameters.get("threads")
    threads = int(threads_raw) if threads_raw is not None else None
    arguments = _rewrite_arguments(raw_args, f"output/{sol_name}", threads)

    # Create a zero-copy symlink workspace in writable /tmp so read-only /work/in/bundle/input
    # and writable /work/out map directly to `input/` and `output/` relative paths.
    run_dir = Path(tempfile.mkdtemp(prefix="ies-highs-run-"))
    try:
        input_dir = bundle_dir / "input"
        if input_dir.is_dir():
            os.symlink(str(input_dir.resolve()), str(run_dir / "input"))
        os.symlink(str(ctx.work_out.resolve()), str(run_dir / "output"))

        started_at = _utc_now()
        proc = subprocess.run(
            [HIGHS_EXECUTABLE_PATH, *arguments],
            cwd=run_dir,
            env={
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "HOME": "/tmp",
            },
            capture_output=True,
            text=True,
            check=False,
        )
        ended_at = _utc_now()
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)

    success_codes = tuple(int(c) for c in command.get("success_exit_codes", [0]))
    write_json_file(
        ctx.work_out / "solver_meta.json",
        {
            "exit_code": proc.returncode,
            "termination_reason": "exited",
            "started_at": started_at,
            "ended_at": ended_at,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "executable": str(command.get("executable") or "ies.solver.highs@1.7.2"),
            "arguments": arguments,
        },
    )
    if proc.returncode not in success_codes:
        raise RuntimeError(
            f"ies-highs-solver failed with exit code {proc.returncode}: {proc.stderr.strip()}"
        )
