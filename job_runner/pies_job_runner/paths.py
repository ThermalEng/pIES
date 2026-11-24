"""Controlled relative path resolution inside stage and plugin build roots."""

from __future__ import annotations

from pathlib import Path, PurePosixPath


class UnsafePathError(ValueError):
    """Raised when a caller supplies an escaping, absolute, or invalid relative path."""


def resolve_relative_subpath(root: Path, rel: str, *, field_name: str = "path") -> tuple[str, Path]:
    """Resolve ``rel`` strictly inside ``root`` and return ``(normalized_rel, resolved_path)``.

    Rejects empty strings, backslashes, absolute paths, ``.``/``..`` traversal segments,
    and symlinks resolving outside ``root``.
    """
    if not rel or "\\" in rel or rel.startswith("/"):
        raise UnsafePathError(f"{field_name} must be a non-empty relative path: {rel!r}")
    posix = PurePosixPath(rel)
    if posix.is_absolute() or any(part in ("", ".", "..") for part in posix.parts):
        raise UnsafePathError(f"{field_name} contains disallowed path segments: {rel!r}")

    resolved_root = root.resolve()
    candidate = (resolved_root / Path(*posix.parts)).resolve()
    try:
        candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise UnsafePathError(f"{field_name} escapes configured root: {rel!r}") from exc
    return posix.as_posix(), candidate
