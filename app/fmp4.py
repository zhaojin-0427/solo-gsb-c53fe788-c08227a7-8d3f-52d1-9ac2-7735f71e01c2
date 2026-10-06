"""Parsing and rebuilding of fragmented MP4 (fMP4) media.

The recovery flow works without demuxing or transcoding:

* the init segment keeps its original ``ftyp``/``moov`` boxes (trex defaults
  and edit lists stay untouched);
* every ``moof``/``mdat`` pair of every uploaded segment is parsed down to the
  individual samples using ``tfhd``/``tfdt``/``trun`` and the trex defaults;
* samples keep their original bytes and their original per-track ``tfdt``
  decode timestamps, so B-frame composition offsets and audio/video sync are
  preserved and missing pieces never shift later media;
* the output is a freshly muxed sequence of single-track
  ``moof``+``mdat`` fragments.
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Optional

from . import mp4boxes as boxes
from .mp4boxes import (
    Box,
    BoxParseError,
    build_box,
    build_full_box,
    children,
    find_child,
    find_path,
    full_box_header,
    iter_boxes,
)


class Fmp4Error(ValueError):
    """Structural fMP4 error (maps to HTTP 422)."""

    def __init__(self, message: str, *, code: str = "unparseable"):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# Init segment: track metadata
# ---------------------------------------------------------------------------

@dataclass
class Edit:
    segment_duration: int   # in movie timescale (0 == until end)
    media_time: int         # media timescale, -1 = empty edit
    media_rate: Fraction


@dataclass
class Track:
    track_id: int
    handler: str            # 'vide' / 'soun'
    codec: str              # 'h264' / 'aac'
    timescale: int
    duration: int           # declared media duration in timescale units
    language: str
    width: float
    height: float
    channel_count: int
    sample_rate: int
    edit_list: list[Edit] = field(default_factory=list)

    @property
    def kind(self) -> str:
        return "video" if self.handler == "vide" else "audio"

    def to_report(self) -> dict:
        movie_ts = getattr(self, "_movie_timescale", 0) or 1
        info: dict = {
            "track_id": self.track_id,
            "type": self.kind,
            "codec": self.codec,
            "timescale": self.timescale,
        }
        if self.kind == "video":
            info["width"] = self.width
            info["height"] = self.height
        else:
            info["channels"] = self.channel_count
            info["sample_rate"] = self.sample_rate
        info["declared_duration_ms"] = round(self.duration * 1000 / self.timescale, 3)
        if self.edit_list:
            info["edit_list"] = [
                {
                    "segment_duration_ms": (
                        None if e.segment_duration == 0
                        else round(e.segment_duration * 1000 / movie_ts, 3)
                    ),
                    "media_time_ms": (
                        None if e.media_time < 0
                        else round(e.media_time * 1000 / self.timescale, 3)
                    ),
                    "media_time": e.media_time,
                    "media_rate": float(e.media_rate),
                }
                for e in self.edit_list
            ]
        return info


@dataclass
class InitInfo:
    ftyp: Optional[bytes]
    moov: bytes                 # exact original moov box bytes
    movie_timescale: int
    tracks: dict[int, Track]    # keyed by track_id
    is_encrypted: bool


def _read_fixed16_16(v: int) -> float:
    return v / 65536.0


def _parse_language(data: bytes, off: int) -> str:
    v = struct.unpack_from(">H", data, off)[0]
    chars = ((v >> 10) & 0x1F, (v >> 5) & 0x1F, v & 0x1F)
    if any(c == 0 for c in chars):
        return "und"
    return "".join(chr(0x60 + c) for c in chars)


def _read_descriptor(buf: bytes, i: int) -> tuple[int, bytes, int] | None:
    """Read one ES descriptor at ``i``; returns (tag, payload, next_index).

    Handles the optional zero padding bytes some muxers (e.g. ffmpeg) emit
    after the tag before the expandable size.
    """
    if i >= len(buf):
        return None
    tag = buf[i]
    i += 1
    # skip leading 0x80 zero-padding bytes
    while i < len(buf) and buf[i] == 0x80:
        i += 1
    size = 0
    for _ in range(4):
        if i >= len(buf):
            return None
        b = buf[i]
        i += 1
        size = (size << 7) | (b & 0x7F)
        if not (b & 0x80):
            break
    if i + size > len(buf):
        return None
    return tag, buf[i:i + size], i + size


def _iter_descriptors(blob: bytes):
    """Yield (tag, payload) for ISO 14496-1 ES descriptors, descending once."""
    def walk(buf: bytes, start=0, end=None):
        i = start
        e = len(buf) if end is None else end
        while i < e:
            desc = _read_descriptor(buf, i)
            if desc is None:
                return
            tag, payload, nxt = desc
            yield tag, payload
            i = nxt

    yield from walk(blob)
    # ES_Descr (0x03) contains DecoderConfigDescr (0x04) which contains
    # DecoderSpecificInfo (0x05); descend into both container tags.
    i = 0
    while i < len(blob):
        desc = _read_descriptor(blob, i)
        if desc is None:
            break
        tag, payload, nxt = desc
        if tag == 0x03:
            # ES_Descr: ES_ID(2) flags(1) then the nested descriptor
            yield from walk(payload, 3)
        elif tag == 0x04:
            # DecoderConfigDescr: 13 fixed bytes before nested descriptor
            yield from walk(payload, 13)
        i = nxt


def _parse_esds_audio(esds_payload: bytes) -> tuple[int, int]:
    """Return (object_type_indication, audio_object_type) from an esds box."""
    body = esds_payload[4:]  # strip FullBox version/flags
    oti = None
    aot = None
    for tag, payload in _iter_descriptors(body):
        if tag == 0x03 and len(payload) >= 2:
            oti = payload[0]
        elif tag == 0x04 and len(payload) >= 13:
            oti = payload[0]
        elif tag == 0x05 and payload:
            aot = (payload[0] >> 3) & 0x1F
            if aot == 31 and len(payload) > 1:
                aot = 32 + ((payload[0] & 0x07) << 3) + (payload[1] >> 5)
    return oti if oti is not None else 0, aot if aot is not None else 0


def _box_contains(data: bytes, parent: Box, wanted: bytes) -> bool:
    try:
        for child in children(data, parent):
            if child.type == wanted:
                return True
            if child.type in boxes.CONTAINER_BOXES:
                if _box_contains(data, child, wanted):
                    return True
    except BoxParseError:
        return False
    return False


def _parse_stsd(data: bytes, stsd: Box, handler: str) -> tuple[str, bool, dict]:
    """Return (codec, encrypted, fixed_header_fields) for the first entry."""
    entry_start = stsd.data_start + 8  # version/flags(4) + entry_count(4)
    if entry_start + 8 > stsd.end:
        raise Fmp4Error("stsd has no sample entry", code="bad_init")
    p = entry_start
    entry_size = struct.unpack_from(">I", data, p)[0]
    entry_type = data[p + 4:p + 8]
    if entry_size < 8 or entry_start + entry_size > stsd.end:
        raise Fmp4Error("invalid sample entry in stsd", code="bad_init")
    entry_end = entry_start + entry_size

    # Sample entry fixed layouts per ISO 14496-12 (offsets after the box
    # size/type): 6 reserved + 2 data_reference_index are common; visual
    # entries add 70 bytes; audio entries add 20 (v0) or 28 (v1/v2).
    common = 8 + 8
    if entry_type in (b"avc1", b"avc3", b"hvc1", b"hev1", b"av01"):
        child_start = entry_start + common + 70
    elif entry_type == b"mp4a":
        version = data[entry_start + common]
        child_start = entry_start + common + (28 if version in (1, 2) else 20)
    else:
        child_start = entry_start + common

    found: dict[bytes, Box] = {}
    pos = child_start
    while pos + 8 <= entry_end:
        size = struct.unpack_from(">I", data, pos)[0]
        t = data[pos + 4:pos + 8]
        if size < 8 or pos + size > entry_end:
            break
        if t in (b"avcC", b"hvcC", b"esds", b"sinf", b"av1C", b"vp09"):
            found[t] = Box(t, pos, 8, pos + 8, size, pos + size, True)
        pos += size

    # fixed sample entry header fields
    fields: dict = {}
    if handler == "vide":
        # visual: after the 8-byte entry box header, 8 bytes reserved/dref,
        # 16 bytes pre_defined/reserved, then width(2) height(2)
        fields["width"] = struct.unpack_from(">H", data, entry_start + 8 + 24)[0]
        fields["height"] = struct.unpack_from(">H", data, entry_start + 8 + 26)[0]
    elif handler == "soun":
        # audio: entry header(8), reserved[6]+dref(2)+reserved[8] ->
        # channelcount at +24, samplesize +26, samplerate 16.16 at +32
        fields["channel_count"] = struct.unpack_from(
            ">H", data, entry_start + 24
        )[0]
        fields["sample_rate"] = struct.unpack_from(
            ">H", data, entry_start + 32
        )[0]

    encrypted = b"sinf" in found
    if entry_type in (b"avc1", b"avc3"):
        if b"avcC" not in found:
            raise Fmp4Error("video sample entry missing avcC", code="codec_unsupported")
        return "h264", encrypted, fields
    if entry_type == b"mp4a":
        esds = found.get(b"esds")
        oti, aot = _parse_esds_audio(
            data[esds.data_start:esds.end] if esds else b""
        )
        # objectTypeIndication 0x40 = MPEG-4 Audio (AAC family),
        # 0x66/0x67/0x68 = MPEG-2 AAC LC/Main/SSR.
        if oti not in (0x40, 0x66, 0x67, 0x68):
            raise Fmp4Error(
                f"audio objectTypeIndication 0x{oti:02x} is not AAC",
                code="codec_unsupported",
            )
        # AudioSpecificConfig object type: 2 = LC, 5 = HE-AAC, 29 = HE-AACv2
        if aot and aot not in (1, 2, 3, 4, 5, 6, 17, 23, 29):
            raise Fmp4Error(
                f"audio object type {aot} is not AAC",
                code="codec_unsupported",
            )
        return "aac", encrypted, fields
    if encrypted:
        raise Fmp4Error("encrypted sample entry is not supported",
                        code="encrypted")
    raise Fmp4Error(
        f"unsupported codec {entry_type.decode('ascii', 'replace')!r}; "
        "only H.264 video and AAC audio are supported",
        code="codec_unsupported",
    )


def parse_init(data: bytes) -> InitInfo:
    ftyp = None
    moov_box = None
    for box in iter_boxes(data, 0, len(data)):
        if not box.complete:
            raise Fmp4Error("init segment is truncated", code="bad_init")
        if box.type == b"ftyp":
            ftyp = data[box.start:box.end]
        elif box.type == b"moov":
            moov_box = box
    if moov_box is None:
        raise Fmp4Error("init segment contains no moov box", code="bad_init")

    mvhd = find_path(data, moov_box, b"mvhd")
    if mvhd is None:
        raise Fmp4Error("moov missing mvhd", code="bad_init")
    version = data[mvhd.data_start]
    off = mvhd.data_start + 4
    if version == 1:
        movie_timescale = struct.unpack_from(">I", data, off + 16)[0]
    else:
        movie_timescale = struct.unpack_from(">I", data, off + 8)[0]

    tracks: dict[int, Track] = {}
    for trak in children(data, moov_box):
        if trak.type != b"trak":
            continue
        tkhd = find_path(data, trak, b"tkhd")
        if tkhd is None:
            raise Fmp4Error("trak missing tkhd", code="bad_init")
        v = data[tkhd.data_start]
        o = tkhd.data_start + 4
        if v == 1:
            # creation(8) modification(8) track_ID(4)
            track_id = struct.unpack_from(">I", data, o + 16)[0]
        else:
            # creation(4) modification(4) track_ID(4)
            track_id = struct.unpack_from(">I", data, o + 8)[0]

        mdhd = find_path(data, trak, b"mdia", b"mdhd")
        hdlr = find_path(data, trak, b"mdia", b"hdlr")
        if mdhd is None or hdlr is None:
            raise Fmp4Error(f"track {track_id} missing mdhd/hdlr",
                            code="bad_init")
        v = data[mdhd.data_start]
        o = mdhd.data_start + 4
        if v == 1:
            timescale = struct.unpack_from(">I", data, o + 16)[0]
            duration = struct.unpack_from(">Q", data, o + 20)[0]
            lang_off = o + 28
        else:
            timescale = struct.unpack_from(">I", data, o + 8)[0]
            duration = struct.unpack_from(">I", data, o + 12)[0]
            lang_off = o + 16
        language = _parse_language(data, lang_off)
        handler = data[hdlr.data_start + 8:hdlr.data_start + 12].decode(
            "ascii", "replace"
        )

        stsd = find_path(
            data, trak, b"mdia", b"minf", b"stbl", b"stsd"
        )
        if stsd is None:
            raise Fmp4Error(f"track {track_id} missing stsd", code="bad_init")
        codec, encrypted, entry_fields = _parse_stsd(data, stsd, handler)
        if handler == "vide" and codec != "h264":
            raise Fmp4Error(
                f"track {track_id}: video codec {codec} is not H.264",
                code="codec_unsupported",
            )
        if handler == "soun" and codec != "aac":
            raise Fmp4Error(
                f"track {track_id}: audio codec {codec} is not AAC",
                code="codec_unsupported",
            )
        if handler not in ("vide", "soun"):
            raise Fmp4Error(
                f"track {track_id}: handler {handler!r} is not supported "
                "(only video/audio)",
                code="codec_unsupported",
            )

        width = float(entry_fields.get("width", 0))
        height = float(entry_fields.get("height", 0))
        channel_count = entry_fields.get("channel_count", 0)
        sample_rate = entry_fields.get("sample_rate", 0)

        track = Track(
            track_id=track_id,
            handler=handler,
            codec=codec,
            timescale=timescale,
            duration=duration,
            language=language,
            width=width,
            height=height,
            channel_count=channel_count,
            sample_rate=sample_rate,
        )
        # stash for report annotation
        object.__setattr__(track, "_movie_timescale", movie_timescale)

        elst = find_path(data, trak, b"edts", b"elst")
        if elst is not None:
            ev, _ = full_box_header(data, elst)
            count_off = elst.data_start + 4
            count = struct.unpack_from(">I", data, count_off)[0]
            eo = count_off + 4
            for _ in range(count):
                if ev == 1:
                    seg_dur = struct.unpack_from(">Q", data, eo)[0]
                    media_time = struct.unpack_from(">q", data, eo + 8)[0]
                    rate = struct.unpack_from(">I", data, eo + 16)[0]
                    eo += 20
                else:
                    seg_dur = struct.unpack_from(">I", data, eo)[0]
                    media_time = struct.unpack_from(">i", data, eo + 4)[0]
                    rate = struct.unpack_from(">I", data, eo + 8)[0]
                    eo += 12
                track.edit_list.append(Edit(
                    segment_duration=seg_dur,
                    media_time=media_time,
                    media_rate=Fraction(rate, 65536),
                ))

        if encrypted:
            raise Fmp4Error(
                f"track {track_id} uses CENC encryption (sinf/schm); "
                "only unencrypted media is supported",
                code="encrypted",
            )
        tracks[track_id] = track

    if not tracks:
        raise Fmp4Error("init segment declares no tracks", code="bad_init")

    # mvex with trex is mandatory for fMP4
    for track_id, track in tracks.items():
        trex = None
        for child in children(data, moov_box):
            if child.type == b"mvex":
                for mvex_child in children(data, child):
                    if (mvex_child.type == b"trex" and
                            struct.unpack_from(">I", data,
                                               mvex_child.data_start + 4)[0]
                            == track_id):
                        trex = mvex_child
        if trex is None:
            raise Fmp4Error(
                f"track {track_id} has no trex default box in mvex",
                code="bad_init",
            )

    return InitInfo(
        ftyp=ftyp,
        moov=data[moov_box.start:moov_box.end],
        movie_timescale=movie_timescale,
        tracks=tracks,
        is_encrypted=_box_contains(data, moov_box, b"sinf"),
    )


# ---------------------------------------------------------------------------
# Media fragments
# ---------------------------------------------------------------------------

# tfhd flags (ISO 14496-12 8.8.7)
TFHD_BASE_DATA_OFFSET = 0x000001
TFHD_SAMPLE_DESC_IDX = 0x000002
TFHD_DEFAULT_DURATION = 0x000008
TFHD_DEFAULT_SIZE = 0x000010
TFHD_DEFAULT_FLAGS = 0x000020
TFHD_DURATION_IS_EMPTY = 0x010000
TFHD_DEFAULT_BASE_IS_MOOF = 0x020000

# trun flags (ISO 14496-12 8.8.8)
TRUN_DATA_OFFSET = 0x000001
TRUN_FIRST_FLAGS = 0x000004
TRUN_SAMPLE_DURATION = 0x000100
TRUN_SAMPLE_SIZE = 0x000200
TRUN_SAMPLE_FLAGS = 0x000400
TRUN_SAMPLE_CTO = 0x000800


@dataclass
class Sample:
    size: int
    duration: int
    cto: int
    flags: int
    data: bytes
    dts: int = 0   # filled by fragment assembly


@dataclass
class Traf:
    track_id: int
    base_dts: int
    samples: list[Sample]
    raw_samples: int                 # sample count before truncation trimming
    dropped_from_truncation: int     # samples removed (tail truncation)
    complete: bool                   # full byte range available
    order: int                       # traf order within the moof


@dataclass
class Fragment:
    source_file: int
    moof_offset_in_file: int
    moof_bytes: bytes                # complete moof (needed for data offsets)
    trafs: list[Traf]
    sequence: int


def _parse_traf(data: bytes, traf: Box, moof_start: int,
                init: InitInfo, order: int) -> Traf:
    tfhd = find_child(data, traf, b"tfhd")
    if tfhd is None:
        raise Fmp4Error("traf missing tfhd", code="bad_fragment")
    _, flags = full_box_header(data, tfhd)
    off = tfhd.data_start + 4
    track_id = struct.unpack_from(">I", data, off)[0]
    off += 4
    if track_id not in init.tracks:
        raise Fmp4Error(
            f"fragment references unknown track_id {track_id}",
            code="unknown_track",
        )

    base_data_offset = None
    sample_desc_idx = None
    default_duration = None
    default_size = None
    default_flags = 0
    if flags & TFHD_BASE_DATA_OFFSET:
        base_data_offset = struct.unpack_from(">Q", data, off)[0]
        off += 8
    if flags & TFHD_SAMPLE_DESC_IDX:
        sample_desc_idx = struct.unpack_from(">I", data, off)[0]
        off += 4
    if flags & TFHD_DEFAULT_DURATION:
        default_duration = struct.unpack_from(">I", data, off)[0]
        off += 4
    if flags & TFHD_DEFAULT_SIZE:
        default_size = struct.unpack_from(">I", data, off)[0]
        off += 4
    if flags & TFHD_DEFAULT_FLAGS:
        default_flags = struct.unpack_from(">I", data, off)[0]
        off += 4
    _ = sample_desc_idx

    duration_is_empty = bool(flags & TFHD_DURATION_IS_EMPTY)
    if base_data_offset is None:
        # When the muxer provides no explicit base-data-offset, use the
        # position of the enclosing moof (the default-base-is-moof semantic
        # assumed by ffmpeg/GPAC/Bento4 for standalone fMP4 segments).
        base_data_offset = moof_start

    tfdt = find_child(data, traf, b"tfdt")
    if tfdt is None:
        raise Fmp4Error(
            f"track {track_id} fragment missing tfdt",
            code="bad_fragment",
        )
    tfdt_version, _ = full_box_header(data, tfdt)
    to = tfdt.data_start + 4
    if tfdt_version == 1:
        base_dts = struct.unpack_from(">Q", data, to)[0]
    else:
        base_dts = struct.unpack_from(">I", data, to)[0]

    trun_box = find_child(data, traf, b"trun")
    if trun_box is None:
        if duration_is_empty:
            return Traf(track_id, base_dts, [], 0, 0,
                        traf.complete, order)
        raise Fmp4Error(
            f"track {track_id} fragment missing trun",
            code="bad_fragment",
        )
    trun_version, trun_flags = full_box_header(data, trun_box)
    ro = trun_box.data_start + 4
    sample_count = struct.unpack_from(">I", data, ro)[0]
    ro += 4
    data_offset = None
    first_flags = None
    if trun_flags & TRUN_DATA_OFFSET:
        data_offset = struct.unpack_from(">i", data, ro)[0]
        ro += 4
    if trun_flags & TRUN_FIRST_FLAGS:
        first_flags = struct.unpack_from(">I", data, ro)[0]
        ro += 4

    per_sample: list[tuple[int, int, int, int]] = []
    for i in range(sample_count):
        sd = default_duration
        ss = default_size
        sf = default_flags
        cto = 0
        if trun_flags & TRUN_SAMPLE_DURATION:
            sd = struct.unpack_from(">I", data, ro)[0]
            ro += 4
        if trun_flags & TRUN_SAMPLE_SIZE:
            ss = struct.unpack_from(">I", data, ro)[0]
            ro += 4
        if trun_flags & TRUN_SAMPLE_FLAGS:
            sf = struct.unpack_from(">I", data, ro)[0]
            ro += 4
        if trun_flags & TRUN_SAMPLE_CTO:
            if trun_version == 0:
                cto = struct.unpack_from(">I", data, ro)[0]
            else:
                cto = struct.unpack_from(">i", data, ro)[0]
            ro += 4
        if i == 0 and first_flags is not None:
            sf = first_flags
        if sd is None or ss is None:
            raise Fmp4Error(
                f"track {track_id}: sample {i} has no resolvable "
                "duration/size (missing trun and trex defaults)",
                code="bad_fragment",
            )
        per_sample.append((ss, sd, cto, sf))

    if data_offset is None:
        # Some muxers omit data-offset; base points at mdat payload then.
        sample_base = base_data_offset
    else:
        sample_base = base_data_offset + data_offset

    # Slice sample bytes relative to the file buffer.
    samples: list[Sample] = []
    cursor = sample_base
    for size, dur, cto, sf in per_sample:
        if cursor < 0 or cursor + size > len(data):
            # tail truncation: stop at last fully available sample
            break
        samples.append(Sample(
            size=size,
            duration=dur,
            cto=cto,
            flags=sf,
            data=data[cursor:cursor + size],
        ))
        cursor += size

    raw_samples = sample_count
    dropped = raw_samples - len(samples)
    return Traf(
        track_id=track_id,
        base_dts=base_dts,
        samples=samples,
        raw_samples=raw_samples,
        dropped_from_truncation=dropped,
        complete=dropped == 0,
        order=order,
    )


def parse_segment(data: bytes, source_index: int,
                  init: InitInfo) -> list[Fragment]:
    """Parse one uploaded media segment into one Fragment per moof/mdat."""
    fragments: list[Fragment] = []
    top = list(iter_boxes(data, 0, len(data)))

    i = 0
    while i < len(top):
        box = top[i]
        if box.type != b"moof":
            i += 1
            continue
        moof_box = box
        if not moof_box.complete:
            raise Fmp4Error(
                f"segment {source_index}: truncated moof header, sample "
                "boundaries cannot be determined",
                code="truncated_moof",
            )
        # next box must be (one) mdat; tolerate intervening free/skip boxes
        j = i + 1
        while j < len(top) and top[j].type in (b"free", b"skip", b"styp"):
            j += 1
        if j >= len(top) or top[j].type != b"mdat":
            raise Fmp4Error(
                f"segment {source_index}: moof without following mdat",
                code="bad_fragment",
            )
        mdat_box = top[j]

        mfhd = find_child(data, moof_box, b"mfhd")
        if mfhd is None:
            raise Fmp4Error("moof missing mfhd", code="bad_fragment")
        sequence = struct.unpack_from(
            ">I", data, mfhd.data_start + 4
        )[0]

        trafs: list[Traf] = []
        order = 0
        for child in children(data, moof_box):
            if child.type != b"traf":
                continue
            trafs.append(
                _parse_traf(
                    data, child, moof_box.start, init, order
                )
            )
            order += 1
        if not trafs:
            raise Fmp4Error("moof contains no traf", code="bad_fragment")

        moof_bytes = data[moof_box.start:moof_box.end]
        frag = Fragment(
            source_file=source_index,
            moof_offset_in_file=moof_box.start,
            moof_bytes=moof_bytes,
            trafs=trafs,
            sequence=sequence,
        )
        # remember the actual mdat payload availability for truncation
        object.__setattr__(
            frag, "_mdat_available",
            len(data) - mdat_box.data_start
        )
        fragments.append(frag)
        i = j + 1

    if not fragments:
        raise Fmp4Error(
            f"segment {source_index} contains no fMP4 fragments (moof/mdat)",
            code="not_fragmented",
        )
    return fragments


# ---------------------------------------------------------------------------
# Rebuild
# ---------------------------------------------------------------------------

def _traf_end_dts(traf: Traf) -> int:
    return traf.base_dts + sum(s.duration for s in traf.samples)


def rebuild_traf_fragment(traf: Traf, sequence: int) -> bytes:
    """Build a standalone single-track moof+mdat from parsed samples."""
    track_id = traf.track_id
    sample_bytes = b"".join(s.data for s in traf.samples)

    # tfdt v1 (64 bit)
    tfdt_payload = struct.pack(">Q", traf.base_dts)
    tfdt = build_full_box(b"tfdt", 1, 0, tfdt_payload)

    # trun v1 with explicit data offset, durations, sizes, flags, cto.
    flags = (TRUN_DATA_OFFSET | TRUN_SAMPLE_DURATION | TRUN_SAMPLE_SIZE
             | TRUN_SAMPLE_FLAGS | TRUN_SAMPLE_CTO)
    trun_payload = struct.pack(">I", len(traf.samples))
    trun_payload += struct.pack(">i", 0)  # data_offset, patched later
    for s in traf.samples:
        trun_payload += struct.pack(">IIII", s.duration, s.size, s.flags,
                                    s.cto)
    trun = build_full_box(b"trun", 1, flags, trun_payload)

    # tfhd: default-base-is-moof, nothing else (everything explicit in trun)
    tfhd = build_full_box(
        b"tfhd", 0, TFHD_DEFAULT_BASE_IS_MOOF,
        struct.pack(">I", track_id),
    )
    traf_box = build_box(b"traf", tfhd + tfdt + trun)
    mfhd = build_full_box(b"mfhd", 0, 0, struct.pack(">I", sequence))
    moof = build_box(b"moof", mfhd + traf_box)

    # data offset = moof total size + 8 (mdat header)
    data_offset = len(moof) + 8
    # locate trun data-offset field: moof > mfhd(fixed 16) > traf header(8)
    # > tfhd header(8)+payload(4)=20 > tfdt header(8)+payload(8)=24
    # compute dynamically instead:
    trun_field_rel = (
        8 + len(mfhd) + 8 + len(tfhd) + len(tfdt) + 8
        + 4  # version/flags
        + 4  # sample_count
    )
    moof = (
        moof[:trun_field_rel]
        + struct.pack(">i", data_offset)
        + moof[trun_field_rel + 4:]
    )

    if len(sample_bytes) + 8 > 0xFFFFFFFF:
        raise Fmp4Error("rebuilt fragment exceeds 4 GiB", code="too_large")
    mdat = build_box(b"mdat", sample_bytes)
    return moof + mdat


def fingerprint(traf: Traf) -> str:
    h = hashlib.sha256()
    h.update(str(traf.track_id).encode())
    h.update(struct.pack(">Q", traf.base_dts))
    for s in traf.samples:
        h.update(struct.pack(">IIiI", s.duration, s.size, s.cto, s.flags))
        h.update(s.data)
    return h.hexdigest()
