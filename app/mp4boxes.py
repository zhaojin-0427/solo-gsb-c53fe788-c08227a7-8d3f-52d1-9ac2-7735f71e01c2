"""Minimal ISO/IEC 14496-12 (ISOBMFF) box parser.

Only the boxes needed to inspect fragmented MP4 init segments and movie
fragments are understood; everything else can still be walked generically.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

CONTAINER_BOXES = {
    b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts",
    b"mvex", b"moof", b"traf", b"mfra",
}


class BoxParseError(ValueError):
    """Raised when a box structure cannot be parsed reliably."""


@dataclass
class Box:
    type: bytes
    start: int          # offset of the size field
    header_size: int    # 8 (32-bit size) or 16 (largesize)
    data_start: int     # first payload byte (for FullBox: version+flags)
    size: int           # declared total box size (size==0 -> to buffer end)
    end: int            # declared end offset (may exceed the real buffer end)
    complete: bool

    @property
    def payload(self) -> tuple[int, int]:
        """Payload range (start, end), clamped to what is available."""
        return self.data_start, min(self.end, self._buffer_end)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Box({self.type!r}, start={self.start}, size={self.size})"


def iter_boxes(data: bytes, start: int, end: int):
    """Yield top-level boxes in ``data[start:end]``.

    The walk stops after the first box whose declared size runs past ``end``
    (a truncated tail); that box is yielded last with ``complete=False``.
    """
    pos = start
    while pos + 8 <= end:
        size = struct.unpack_from(">I", data, pos)[0]
        btype = data[pos + 4:pos + 8]
        header = 8
        if size == 1:
            if pos + 16 > end:
                raise BoxParseError(
                    f"truncated largesize header for box {btype!r} at {pos}"
                )
            size = struct.unpack_from(">Q", data, pos + 8)[0]
            header = 16
        elif size == 0:
            size = end - pos
        if size < header:
            raise BoxParseError(
                f"invalid size {size} for box {btype!r} at offset {pos}"
            )
        declared_end = pos + size
        complete = declared_end <= end
        box = Box(
            type=btype,
            start=pos,
            header_size=header,
            data_start=pos + header,
            size=size,
            end=declared_end,
            complete=complete,
        )
        object.__setattr__(box, "_buffer_end", end)
        yield box
        if not complete:
            return
        pos = declared_end
    # trailing garbage smaller than a header is left to the caller to notice


def children(data: bytes, box: Box):
    """Iterate the child boxes of a container box."""
    if not box.complete:
        raise BoxParseError(f"cannot read children of truncated {box.type!r}")
    yield from iter_boxes(data, box.data_start, box.end)


def find_child(data: bytes, box: Box, wanted: bytes) -> Box | None:
    for child in children(data, box):
        if child.type == wanted:
            return child
    return None


def find_path(data: bytes, root: Box | None, *path: bytes) -> Box | None:
    box = root
    for name in path:
        if box is None:
            return None
        box = find_child(data, box, name)
    return box


def full_box_header(data: bytes, box: Box) -> tuple[int, int]:
    """Return (version, flags) of a FullBox; consumes the first 4 payload bytes."""
    if box.data_start + 4 > min(box.end, len(data)):
        raise BoxParseError(f"truncated full box {box.type!r}")
    version = data[box.data_start]
    flags = struct.unpack_from(">I", data, box.data_start)[0] & 0x00FFFFFF
    return version, flags


def iter_full_boxes(data: bytes, box: Box):
    """Like :func:`children` but yielding (child, version, flags)."""
    for child in children(data, box):
        version, flags = full_box_header(data, child)
        yield child, version, flags


def build_box(btype: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload) + 8) + btype + payload


def build_full_box(btype: bytes, version: int, flags: int,
                   payload: bytes) -> bytes:
    header = bytes([version]) + struct.pack(">I", flags)[1:]
    return build_box(btype, header + payload)
