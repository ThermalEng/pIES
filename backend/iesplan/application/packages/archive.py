"""ZIP serialization over content references, with disk-backed bounded working buffers."""
from __future__ import annotations

import tempfile
import zipfile
from dataclasses import dataclass

from iesplan.core.content import CHUNK_BYTES, Content, MemoryContent


class ArchiveBuilder:
    def __init__(self):
        self._entries = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def writestr(self, name, content):
        self._entries.append((name, MemoryContent(content) if isinstance(content, bytes) else content))

    def content(self) -> ArchiveContent:
        return ArchiveContent(tuple(self._entries))


@dataclass(frozen=True, slots=True)
class ArchiveContent:
    entries: tuple[tuple[str, Content], ...]

    def iter_chunks(self):
        # Handles never cross the content boundary; each read owns its temporary archive.
        with tempfile.TemporaryFile() as target:
            with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
                for name, content in self.entries:
                    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                    info.compress_type = zipfile.ZIP_DEFLATED
                    with archive.open(info, "w", force_zip64=True) as entry:
                        for chunk in content.iter_chunks():
                            entry.write(chunk)
            target.seek(0)
            while chunk := target.read(CHUNK_BYTES):
                yield chunk
