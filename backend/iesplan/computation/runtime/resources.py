"""Local cgroup v2 facts, independent of generator and solver selection.

Observation is optional: unavailable metrics are unknown, never unlimited or a
reason to reject work. Values describe the shared cgroup, not task reservations
or per-process peaks. No host probing, resource prediction or limit changes.
"""

from dataclasses import dataclass
from pathlib import Path


def _read(root: Path, name: str) -> str | None:
    try:
        return (root / name).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None


def _number(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _limit(value: str | None) -> dict[str, object]:
    amount = _number(value)
    return {
        "status": "limited" if amount is not None else (
            "unlimited" if value == "max" else "unknown"
        ),
        "bytes": amount,
    }


def read_cgroup_oom_kills(root: Path = Path("/sys/fs/cgroup")) -> int | None:
    """Unavailable cgroup v2 events cannot establish an OOM cause."""
    events = _read(root, "memory.events")
    if events is not None:
        for line in events.splitlines():
            fields = line.split()
            if len(fields) == 2 and fields[0] == "oom_kill":
                return _number(fields[1])
    return None


@dataclass(frozen=True, slots=True)
class LocalResourceUsage:
    """Raw limit tokens retain the distinction between unknown and unlimited."""

    memory_max: str | None
    swap_max: str | None
    memory_current_bytes: int | None
    memory_peak_bytes: int | None
    swap_current_bytes: int | None
    oom_kill_count: int | None

    def to_dict(self) -> dict[str, object]:
        return {
            "source": "cgroup_v2",
            "scope": "worker_cgroup",
            "memory_limit": _limit(self.memory_max),
            "swap_limit": _limit(self.swap_max),
            "memory_current_bytes": self.memory_current_bytes,
            "memory_peak_bytes": self.memory_peak_bytes,
            "swap_current_bytes": self.swap_current_bytes,
            "oom_kill_count": self.oom_kill_count,
        }


def observe_local_resources(root: Path = Path("/sys/fs/cgroup")) -> LocalResourceUsage:
    """Read the current deployment boundary; no machine-model adapters needed."""
    return LocalResourceUsage(
        memory_max=_read(root, "memory.max"),
        swap_max=_read(root, "memory.swap.max"),
        memory_current_bytes=_number(_read(root, "memory.current")),
        memory_peak_bytes=_number(_read(root, "memory.peak")),
        swap_current_bytes=_number(_read(root, "memory.swap.current")),
        oom_kill_count=read_cgroup_oom_kills(root),
    )
