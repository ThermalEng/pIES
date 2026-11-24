"""Cgroup v2 / Docker resource observation and /work/out/iterations.jsonl parsing."""

from __future__ import annotations

import json
import os
from collections import deque
from pathlib import Path
from typing import Any

from docker.errors import DockerException

from pies_job_runner.models import (
    IterationsSummary,
    LimitValue,
    MetricValue,
    StageResources,
)

_STATS_SNAPSHOT_FILE = ".job_runner_stats.json"


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None


def _parse_int(raw: str | None) -> int | None:
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _find_container_cgroup_dir(cgroup_root: Path, container_id: str) -> Path | None:
    if not cgroup_root.is_dir():
        return None
    candidates = [
        cgroup_root / "system.slice" / f"docker-{container_id}.scope",
        cgroup_root / "docker" / container_id,
        cgroup_root / f"docker-{container_id}.scope",
    ]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return None


def _read_cgroup_v2_stats(cgroup_dir: Path | None) -> dict[str, int | str | None]:
    if cgroup_dir is None:
        return {}
    result: dict[str, int | str | None] = {}
    mem_max = _read_text(cgroup_dir / "memory.max")
    if mem_max is not None:
        result["memory_max_raw"] = mem_max
    mem_cur = _parse_int(_read_text(cgroup_dir / "memory.current"))
    if mem_cur is not None:
        result["memory_current_bytes"] = mem_cur
    mem_peak = _parse_int(_read_text(cgroup_dir / "memory.peak"))
    if mem_peak is not None:
        result["memory_peak_bytes"] = mem_peak
    mem_events = _read_text(cgroup_dir / "memory.events")
    if mem_events is not None:
        for line in mem_events.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] == "oom_kill":
                oom_count = _parse_int(parts[1])
                if oom_count is not None:
                    result["oom_kill_count"] = oom_count
                break
    cpu_stat = _read_text(cgroup_dir / "cpu.stat")
    if cpu_stat is not None:
        for line in cpu_stat.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] == "usage_usec":
                usec = _parse_int(parts[1])
                if usec is not None:
                    result["cpu_usage_ns"] = usec * 1000
                break
    return result


def _read_docker_live_stats(container: Any) -> dict[str, int | None]:
    """Fetch a non-blocking one-shot stats payload from Docker Engine if container is running."""
    try:
        raw = container.client.api.stats(container.id, stream=False, one_shot=True)
    except TypeError:
        try:
            raw = container.stats(stream=False)
        except DockerException:
            return {}
    except DockerException:
        return {}
    if not isinstance(raw, dict):
        return {}

    parsed: dict[str, int | None] = {}
    mem_stats = raw.get("memory_stats") or {}
    if isinstance(mem_stats, dict):
        usage = mem_stats.get("usage")
        max_usage = mem_stats.get("max_usage")
        stats_detail = mem_stats.get("stats") or {}
        if isinstance(usage, int) and usage > 0:
            parsed["memory_current_bytes"] = usage
        if isinstance(max_usage, int) and max_usage > 0:
            parsed["memory_peak_bytes"] = max_usage
        if isinstance(stats_detail, dict):
            oom = stats_detail.get("oom_kill") or stats_detail.get("oom_kills")
            if isinstance(oom, int):
                parsed["oom_kill_count"] = oom

    cpu_stats = raw.get("cpu_stats") or {}
    if isinstance(cpu_stats, dict):
        cpu_usage = cpu_stats.get("cpu_usage") or {}
        if isinstance(cpu_usage, dict):
            total_ns = cpu_usage.get("total_usage")
            if isinstance(total_ns, int) and total_ns >= 0:
                parsed["cpu_usage_ns"] = total_ns
    return parsed


def _load_snapshot(workspace_dir: Path | None) -> dict[str, Any]:
    if workspace_dir is None:
        return {}
    snap_path = workspace_dir / _STATS_SNAPSHOT_FILE
    try:
        data = json.loads(snap_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_stats_snapshot(workspace_dir: Path | None, resources: StageResources) -> None:
    """Persist observed peak/CPU metrics so they survive container cgroup teardown."""
    if workspace_dir is None or not workspace_dir.is_dir():
        return
    existing = _load_snapshot(workspace_dir)
    peak = resources.memory_peak_bytes.value
    prev_peak = existing.get("memory_peak_bytes")
    if isinstance(peak, int) and (not isinstance(prev_peak, int) or peak > prev_peak):
        existing["memory_peak_bytes"] = peak
    cpu_ns = resources.cpu_usage_ns.value
    prev_cpu = existing.get("cpu_usage_ns")
    if isinstance(cpu_ns, int) and (not isinstance(prev_cpu, int) or cpu_ns > prev_cpu):
        existing["cpu_usage_ns"] = cpu_ns
    oom_cnt = resources.oom_kill_count.value
    prev_oom = existing.get("oom_kill_count")
    if isinstance(oom_cnt, int) and (not isinstance(prev_oom, int) or oom_cnt > prev_oom):
        existing["oom_kill_count"] = oom_cnt
    if not existing:
        return
    tmp_path = workspace_dir / f"{_STATS_SNAPSHOT_FILE}.tmp.{os.getpid()}"
    try:
        tmp_path.write_text(json.dumps(existing), encoding="utf-8")
        tmp_path.replace(workspace_dir / _STATS_SNAPSHOT_FILE)
    except OSError:
        tmp_path.unlink(missing_ok=True)


def collect_stage_resources(
    container: Any,
    attrs: dict[str, Any],
    *,
    cgroup_root: Path,
    workspace_dir: Path | None,
    running: bool,
    include_stats: bool,
) -> StageResources:
    """Assemble container limits and cgroup v2 / Docker runtime metrics.

    Unreadable metrics are reported explicitly with ``status="unknown"`` and ``value=None``
    without blocking or fabricating values.
    """
    host_config = attrs.get("HostConfig") or {}
    state = attrs.get("State") or {}

    mem_limit_raw = host_config.get("Memory")
    if isinstance(mem_limit_raw, int) and mem_limit_raw > 0:
        memory_limit = LimitValue(status="limited", value=mem_limit_raw)
    elif mem_limit_raw == 0:
        memory_limit = LimitValue(status="unlimited", value=None)
    else:
        memory_limit = LimitValue(status="unknown", value=None)

    nano_cpus = host_config.get("NanoCpus")
    if isinstance(nano_cpus, int) and nano_cpus > 0:
        cpu_limit = LimitValue(status="limited", value=nano_cpus / 1_000_000_000)
    elif nano_cpus == 0:
        cpu_limit = LimitValue(status="unlimited", value=None)
    else:
        cpu_limit = LimitValue(status="unknown", value=None)

    pids_raw = host_config.get("PidsLimit")
    if isinstance(pids_raw, int) and pids_raw > 0:
        pids_limit = LimitValue(status="limited", value=pids_raw)
    elif pids_raw in (0, -1):
        pids_limit = LimitValue(status="unlimited", value=None)
    else:
        pids_limit = LimitValue(status="unknown", value=None)

    oom_killed_raw = state.get("OOMKilled")
    if isinstance(oom_killed_raw, bool):
        oom_killed = MetricValue(status="known", value=oom_killed_raw)
    else:
        oom_killed = MetricValue(status="unknown", value=None)

    cg_stats = _read_cgroup_v2_stats(_find_container_cgroup_dir(cgroup_root, container.id))
    live_stats = _read_docker_live_stats(container) if (running and include_stats) else {}
    snapshot = _load_snapshot(workspace_dir)

    mem_cur_val = cg_stats.get("memory_current_bytes")
    if not isinstance(mem_cur_val, int):
        mem_cur_val = live_stats.get("memory_current_bytes")
    mem_cur = (
        MetricValue(status="known", value=mem_cur_val)
        if isinstance(mem_cur_val, int)
        else MetricValue(status="unknown", value=None)
    )

    peak_candidates = [
        v
        for v in (
            cg_stats.get("memory_peak_bytes"),
            live_stats.get("memory_peak_bytes"),
            snapshot.get("memory_peak_bytes"),
        )
        if isinstance(v, int) and v >= 0
    ]
    mem_peak = (
        MetricValue(status="known", value=max(peak_candidates))
        if peak_candidates
        else MetricValue(status="unknown", value=None)
    )

    cpu_candidates = [
        v
        for v in (
            cg_stats.get("cpu_usage_ns"),
            live_stats.get("cpu_usage_ns"),
            snapshot.get("cpu_usage_ns"),
        )
        if isinstance(v, int) and v >= 0
    ]
    cpu_usage = (
        MetricValue(status="known", value=max(cpu_candidates))
        if cpu_candidates
        else MetricValue(status="unknown", value=None)
    )

    oom_candidates = [
        v
        for v in (
            cg_stats.get("oom_kill_count"),
            live_stats.get("oom_kill_count"),
            snapshot.get("oom_kill_count"),
        )
        if isinstance(v, int) and v >= 0
    ]
    oom_kill_count = (
        MetricValue(status="known", value=max(oom_candidates))
        if oom_candidates
        else MetricValue(status="unknown", value=None)
    )

    resources = StageResources(
        memory_limit_bytes=memory_limit,
        memory_current_bytes=mem_cur,
        memory_peak_bytes=mem_peak,
        cpu_limit_cores=cpu_limit,
        cpu_usage_ns=cpu_usage,
        pids_limit=pids_limit,
        oom_killed=oom_killed,
        oom_kill_count=oom_kill_count,
    )
    if running:
        save_stats_snapshot(workspace_dir, resources)
    return resources


def read_iterations_summary(out_dir: Path, tail_limit: int = 20) -> IterationsSummary:
    """Read line count and tail entries from ``/work/out/iterations.jsonl`` if present."""
    iterations_path = out_dir / "iterations.jsonl"
    if not iterations_path.is_file():
        return IterationsSummary(present=False, count=0, tail=[])

    count = 0
    tail_raw: deque[str] = deque(maxlen=max(0, tail_limit))
    try:
        with iterations_path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped:
                    continue
                count += 1
                if tail_limit > 0:
                    tail_raw.append(stripped)
    except OSError:
        return IterationsSummary(present=True, count=0, tail=[])

    tail: list[Any] = []
    for item in tail_raw:
        try:
            tail.append(json.loads(item))
        except ValueError:
            tail.append(item)
    return IterationsSummary(present=True, count=count, tail=tail)
