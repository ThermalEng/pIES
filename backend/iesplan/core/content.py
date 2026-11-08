"""Read-only repeatable content, independent of storage and operating-system handles."""

from __future__ import annotations

import codecs
import json
from array import array
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from io import IncrementalNewlineDecoder
from itertools import islice
from typing import Protocol, runtime_checkable

CHUNK_BYTES = 65_536


@runtime_checkable
class Content(Protocol):
    """Each iteration starts from the beginning; chunk boundaries have no semantics."""

    def iter_chunks(self) -> Iterator[bytes]: ...


class SizedContent(Content, Protocol):
    """Stored content carries the byte count recorded by its successful write."""

    size_bytes: int


class MemoryContent(bytes):
    """Immutable in-memory implementation for small documents."""

    def iter_chunks(self) -> Iterator[bytes]:
        for offset in range(0, len(self), CHUNK_BYTES):
            yield self[offset:offset + CHUNK_BYTES]


@dataclass(frozen=True, slots=True)
class GeneratedContent:
    """Deterministic content over frozen generator-owned data; no I/O dependency."""

    chunks: Callable[[], Iterator[bytes]]

    def iter_chunks(self) -> Iterator[bytes]:
        return self.chunks()


def read_content(content: Content) -> bytes:
    """Explicit whole-document convenience for small documents, not asset handoff."""
    return b"".join(content.iter_chunks())


def text_lines(content: Content) -> Iterator[str]:
    """Incremental UTF-8 decoding, including characters/newlines split across chunks."""
    decoder = IncrementalNewlineDecoder(codecs.getincrementaldecoder("utf-8")(), True)
    pending = ""
    for chunk in content.iter_chunks():
        pieces = (pending + decoder.decode(chunk)).split("\n")
        pending = pieces.pop()
        for line in pieces:
            yield line.removesuffix("\r")
    pending += decoder.decode(b"", final=True)
    if pending:
        yield pending.removesuffix("\r")


def json_chunks(value: object) -> Iterator[bytes]:
    """Serialize a trusted JSON value without a second tree or full text buffer."""
    buffer = bytearray()

    def small_native(node):
        if node is None or isinstance(node, (str, int, float, bool)):
            return True, node
        if isinstance(node, Mapping) and len(node) <= 16 and all(
            item is None or isinstance(item, (str, int, float, bool)) for item in node.values()
        ):
            return True, node._values if isinstance(node, FrozenMap) else dict(node)
        return False, node

    def pieces(node):
        if isinstance(node, array) or (isinstance(node, FrozenSequence)
                                       and isinstance(node._values, array)):
            values = node._values if isinstance(node, FrozenSequence) else node
            yield "["
            for start in range(0, len(values), 512):
                if start:
                    yield ","
                yield json.dumps(values[start:start + 512].tolist(), allow_nan=False,
                                 separators=(",", ":"))[1:-1]
            yield "]"
        elif isinstance(node, Mapping):
            yield "{"
            for index, key in enumerate(sorted(node)):
                if index:
                    yield ","
                yield json.dumps(key, ensure_ascii=False)
                yield ":"
                yield from pieces(node[key])
            yield "}"
        elif isinstance(node, (Sequence, set)) and not isinstance(node, (str, bytes)):
            yield "["
            values = iter(node)
            first = True
            while batch := list(islice(values, 512)):
                if not first:
                    yield ","
                first = False
                native = [small_native(item) for item in batch]
                if all(ok for ok, _item in native):
                    yield json.dumps([item for _ok, item in native], sort_keys=True,
                                     ensure_ascii=False, allow_nan=False, separators=(",", ":"))[1:-1]
                else:
                    for index, item in enumerate(batch):
                        if index:
                            yield ","
                        yield from pieces(item)
            yield "]"
        else:
            yield json.dumps(node.isoformat() if isinstance(node, datetime) else node,
                             ensure_ascii=False, allow_nan=False, separators=(",", ":"))

    for piece in pieces(value):
        raw = memoryview(piece.encode("utf-8"))
        for start in range(0, len(raw), CHUNK_BYTES):
            part = raw[start:start + CHUNK_BYTES]
            if len(buffer) + len(part) > CHUNK_BYTES:
                yield bytes(buffer)
                buffer.clear()
            buffer.extend(part)
    if buffer:
        yield bytes(buffer)


class FrozenMap(Mapping):
    """Ownership-transferred JSON mapping; no copy of the result tree."""

    __slots__ = ("_values",)

    def __init__(self, values):
        self._values = values

    def __getitem__(self, key):
        return self._values[key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)


class FrozenSequence(Sequence):
    __slots__ = ("_values",)

    def __init__(self, values):
        self._values = values

    def __getitem__(self, index):
        return tuple(self._values[index]) if isinstance(index, slice) else self._values[index]

    def __len__(self):
        return len(self._values)

    def __iter__(self):
        return iter(self._values)

    def __eq__(self, other):
        if not isinstance(other, Sequence):
            return NotImplemented
        return len(self) == len(other) and all(a == b for a, b in zip(self, other, strict=True))


def freeze_owned_json(value):
    """Transfer a newly built internal result; caller must relinquish all mutable aliases."""
    if isinstance(value, dict):
        for key in value:
            value[key] = freeze_owned_json(value[key])
        return FrozenMap(value)
    if isinstance(value, list):
        for index in range(len(value)):
            value[index] = freeze_owned_json(value[index])
        return FrozenSequence(value)
    if isinstance(value, array):
        return FrozenSequence(value)
    if isinstance(value, tuple):
        return tuple(freeze_owned_json(item) for item in value)
    return value
