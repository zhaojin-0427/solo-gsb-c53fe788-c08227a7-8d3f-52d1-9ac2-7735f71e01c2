"""ISO-BMFF box 解析与构造（无第三方依赖）。

只覆盖 fMP4 修复需要的部分：
- 顶层 / 嵌套 box 遍历，支持尾片截断的安全迭代；
- tfhd / trun / tfdt / trex / elst / mdhd / mvhd / tkhd / hdlr / stsd 解析；
- 重新构造 moof/traf（tfhd、tfdt、trun）与通用 box。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, List, Optional, Sequence

CONTAINERS = {
    b"moov", b"trak", b"mdia", b"minf", b"stbl", b"mvex",
    b"moof", b"traf", b"edts", b"mfra", b"udta", b"sinf", b"schi",
}


@dataclass(frozen=True)
class Box:
    typ: bytes
    start: int          # box 起点（含 header）
    header: int         # header 长度（8 或 16）
    data_start: int     # payload 起点
    end: int            # 完整 box 理论终点
    payload: bytes      # 仅对完整 box 有效
    truncated: bool = False  # 声明长度超过 blob 实际长度


def iter_boxes(blob: bytes, start: int = 0, end: Optional[int] = None) -> Iterator[Box]:
    """遍历 [start, end) 内的所有顶层 box。

    若最后一个 box 声明长度超过实际数据，yield 一个 truncated=True 的 Box
    （payload 为现存部分）；若连 8 字节头都不完整则停止。
    """
    if end is None:
        end = len(blob)
    pos = start
    while pos + 8 <= end:
        size32 = int.from_bytes(blob[pos:pos + 4], "big")
        typ = blob[pos + 4:pos + 8]
        hdr = 8
        if size32 == 1:
            if pos + 16 > end:
                break
            size = int.from_bytes(blob[pos + 8:pos + 16], "big")
            hdr = 16
        elif size32 == 0:
            size = end - pos
        else:
            size = size32
        if size < hdr:
            # 非法长度，无法继续确定边界
            break
        data_start = pos + hdr
        declared_end = pos + size
        truncated = declared_end > end
        payload = blob[data_start:min(declared_end, end)]
        yield Box(typ, pos, hdr, data_start, declared_end, payload, truncated)
        if truncated:
            return
        pos = declared_end


def children(blob: bytes, box: Box) -> List[Box]:
    """返回容器 box 内的完整子 box（截断尾片同样会 yield，调用方自行判断）。"""
    return list(iter_boxes(blob, box.data_start, box.end if box.end <= len(blob) else len(blob)))


def find_child(blob: bytes, box: Box, typ: bytes) -> Optional[Box]:
    for c in children(blob, box):
        if c.typ == typ:
            return c
    return None


def find_path(blob: bytes, root: Optional[Box], *path: bytes) -> Optional[Box]:
    cur: Optional[Box] = root
    for typ in path:
        if cur is None:
            return None
        cur = find_child(blob, cur, typ)
    return cur


def find_all(blob: bytes, box: Box, typ: bytes) -> List[Box]:
    return [c for c in children(blob, box) if c.typ == typ]


def find_top(blob: bytes, typ: bytes) -> Optional[Box]:
    for b in iter_boxes(blob):
        if b.typ == typ:
            return b
    return None


# ---------------------------------------------------------------- 整数读取

def u8(b: bytes, o: int) -> int:
    return b[o]


def u16(b: bytes, o: int) -> int:
    return int.from_bytes(b[o:o + 2], "big")


def u24(b: bytes, o: int) -> int:
    return int.from_bytes(b[o:o + 3], "big")


def u32(b: bytes, o: int) -> int:
    return int.from_bytes(b[o:o + 4], "big")


def u64(b: bytes, o: int) -> int:
    return int.from_bytes(b[o:o + 8], "big")


def i32(b: bytes, o: int) -> int:
    return int.from_bytes(b[o:o + 4], "big", signed=True)


def fixed16(b: bytes, o: int) -> float:
    return u16(b, o) + u16(b, o + 2) / 65536.0


# ---------------------------------------------------------------- 构造

def box(typ: bytes, payload: bytes = b"") -> bytes:
    return (len(payload) + 8).to_bytes(4, "big") + typ + payload


# ---------------------------------------------------------------- 具体 box 解析

@dataclass
class Tkhd:
    track_id: int
    width: float
    height: float


def parse_tkhd(b: Box) -> Tkhd:
    p = b.payload
    ver = p[0]
    if ver == 1:
        track_id = u32(p, 4 + 8 * 2)
        # creation(8) modification(8) 之后是 track_ID(4)
        width = fixed16(p, len(p) - 8)
        height = fixed16(p, len(p) - 4)
    else:
        track_id = u32(p, 4 + 4 * 2)
        width = fixed16(p, len(p) - 8)
        height = fixed16(p, len(p) - 4)
    return Tkhd(track_id, width, height)


@dataclass
class Mdhd:
    timescale: int
    duration: int  # 0 表示时长未知（分片文件常见）


def parse_mdhd(b: Box) -> Mdhd:
    p = b.payload
    ver = p[0]
    if ver == 1:
        # creation(8) modification(8) timescale(4) duration(8)
        return Mdhd(u32(p, 20), u64(p, 24))
    return Mdhd(u32(p, 12), u32(p, 16))


@dataclass
class Mvhd:
    timescale: int
    duration: int


def parse_mvhd(b: Box) -> Mvhd:
    p = b.payload
    ver = p[0]
    if ver == 1:
        return Mvhd(u32(p, 20), u64(p, 24))
    return Mvhd(u32(p, 12), u32(p, 16))


@dataclass
class Edit:
    segment_duration: int   # 以 movie timescale 计
    media_time: int         # -1 为空编辑（empty edit）
    media_rate: float


@dataclass
class Elst:
    edits: List[Edit]


def parse_elst(b: Box) -> Elst:
    p = b.payload
    ver = p[0]
    count = u32(p, 4)
    o = 8
    edits: List[Edit] = []
    for _ in range(count):
        if ver == 1:
            seg = u64(p, o)
            mt = int.from_bytes(p[o + 8:o + 16], "big", signed=True)
            rate = u16(p, o + 16) + u16(p, o + 18) / 65536.0
            o += 20
        else:
            seg = u32(p, o)
            mt = i32(p, o + 4)
            rate = u16(p, o + 8) + u16(p, o + 10) / 65536.0
            o += 12
        edits.append(Edit(seg, mt, rate))
    return Elst(edits)


@dataclass
class Hdlr:
    handler_type: bytes
    name: str


def parse_hdlr(b: Box) -> Hdlr:
    p = b.payload
    ht = p[8:12]
    raw = p[24:].split(b"\x00", 1)[0]
    return Hdlr(ht, raw.decode("utf-8", "replace"))


@dataclass
class StsdInfo:
    entry_type: bytes       # avc1/avc3/mp4a/...
    is_video: bool
    is_audio: bool
    audio_sample_rate: Optional[int] = None
    audio_channels: Optional[int] = None
    raw_entry_header: bytes = b""  # entry 前 8 字节之外的保留区（暂不使用）


def parse_stsd(blob: bytes, stsd: Box) -> StsdInfo:
    """解析 sample description，只接受单个 entry。"""
    p = stsd.payload
    count = u32(p, 4)
    if count != 1:
        raise ValueError(f"stsd entry_count={count}, 仅支持单个 sample description")
    o = 8
    if o + 8 > len(p):
        raise ValueError("stsd entry 不完整")
    entry_size = u32(p, o)
    entry_type = p[o + 4:o + 8]
    entry_end = o + entry_size
    if entry_size < 8 or entry_end > len(p):
        raise ValueError("stsd entry 长度非法")

    is_video = entry_type in (b"avc1", b"avc3")
    is_audio = entry_type == b"mp4a"

    info = StsdInfo(entry_type=entry_type, is_video=is_video, is_audio=is_audio)
    # entry 头(size+type,8) 之后：visual 78 字节 / audio v0 28 字节固定字段，
    # 再往后才是子 box（avcC/esds 等）。参考 ISO 14496-12 SampleEntry 布局。
    child_base = o + 8 + (78 if is_video else 28)
    child_boxes = list(iter_boxes(p, child_base, entry_end)) if is_video or is_audio else []
    if is_video:
        if not any(c.typ == b"avcC" for c in child_boxes):
            raise ValueError("视频 sample entry 缺少 avcC，无法确认 H.264 配置")
    elif is_audio:
        # AudioSampleEntry v0；p 为 stsd payload，entry 起点 o=8：
        # channels 在 stsd 偏移 o+24（entry+16），samplerate u16.16
        # 在 o+34（entry+26）。这与实际 ffmpeg/mpeg4ip 布局一致：
        # entry 头(8) + SampleEntry 字段(8) + reserved(8) => channels@entry+24
        info.audio_channels = u16(p, o + 24)
        info.audio_sample_rate = u16(p, o + 32)
        children_types = {c.typ for c in child_boxes}
        if b"esds" not in children_types:
            raise ValueError("音频 sample entry 缺少 esds，无法确认 AAC 配置")
    return info


@dataclass
class Trex:
    track_id: int
    default_sample_description_index: int
    default_sample_duration: int
    default_sample_size: int
    default_sample_flags: int


def parse_trex(b: Box) -> Trex:
    p = b.payload
    return Trex(u32(p, 4), u32(p, 8), u32(p, 12), u32(p, 16), u32(p, 20))


# ---- moof/traf ----

# tfhd flags
TFHD_BASE_DATA_OFFSET = 0x000001
TFHD_SAMPLE_DESCRIPTION = 0x000002
TFHD_DEFAULT_SAMPLE_DURATION = 0x000008
TFHD_DEFAULT_SAMPLE_SIZE = 0x000010
TFHD_DEFAULT_SAMPLE_FLAGS = 0x000020
TFHD_DURATION_IS_EMPTY = 0x010000
TFHD_DEFAULT_BASE_IS_MOOF = 0x020000


@dataclass
class Tfhd:
    track_id: int
    base_data_offset: Optional[int] = None
    sample_description_index: Optional[int] = None
    default_sample_duration: Optional[int] = None
    default_sample_size: Optional[int] = None
    default_sample_flags: Optional[int] = None
    default_base_is_moof: bool = False
    duration_is_empty: bool = False


def parse_tfhd(b: Box) -> Tfhd:
    p = b.payload
    flags = int.from_bytes(p[1:4], "big")
    o = 4
    t = Tfhd(track_id=u32(p, o), default_base_is_moof=bool(flags & TFHD_DEFAULT_BASE_IS_MOOF),
             duration_is_empty=bool(flags & TFHD_DURATION_IS_EMPTY))
    o += 4
    if flags & TFHD_BASE_DATA_OFFSET:
        t.base_data_offset = u64(p, o)
        o += 8
    if flags & TFHD_SAMPLE_DESCRIPTION:
        t.sample_description_index = u32(p, o)
        o += 4
    if flags & TFHD_DEFAULT_SAMPLE_DURATION:
        t.default_sample_duration = u32(p, o)
        o += 4
    if flags & TFHD_DEFAULT_SAMPLE_SIZE:
        t.default_sample_size = u32(p, o)
        o += 4
    if flags & TFHD_DEFAULT_SAMPLE_FLAGS:
        t.default_sample_flags = u32(p, o)
        o += 4
    return t


def build_tfhd(track_id: int, sample_description_index: Optional[int],
               default_sample_flags: Optional[int]) -> bytes:
    flags = TFHD_DEFAULT_BASE_IS_MOOF
    payload = bytearray()
    payload += bytes([0]) + flags.to_bytes(3, "big")
    payload += track_id.to_bytes(4, "big")
    if sample_description_index is not None:
        flags |= TFHD_SAMPLE_DESCRIPTION
        payload += sample_description_index.to_bytes(4, "big")
    if default_sample_flags is not None:
        flags |= TFHD_DEFAULT_SAMPLE_FLAGS
        payload += default_sample_flags.to_bytes(4, "big")
    payload[1:4] = flags.to_bytes(3, "big")
    return box(b"tfhd", bytes(payload))


@dataclass
class Tfdt:
    base_media_decode_time: int


def parse_tfdt(b: Box) -> Tfdt:
    p = b.payload
    ver = p[0]
    return Tfdt(u64(p, 4) if ver == 1 else u32(p, 4))


def build_tfdt(dts: int) -> bytes:
    # 始终用 version 1（64 位），避免长录制溢出
    payload = bytes([1, 0, 0, 0]) + dts.to_bytes(8, "big")
    return box(b"tfdt", payload)


# trun flags
TRUN_DATA_OFFSET = 0x000001
TRUN_FIRST_SAMPLE_FLAGS = 0x000004
TRUN_SAMPLE_DURATION = 0x000100
TRUN_SAMPLE_SIZE = 0x000200
TRUN_SAMPLE_FLAGS = 0x000400
TRUN_SAMPLE_CTS = 0x000800


@dataclass
class TrunSample:
    duration: Optional[int] = None
    size: Optional[int] = None
    flags: Optional[int] = None
    cts: Optional[int] = None  # version 1 有符号


@dataclass
class Trun:
    data_offset: Optional[int]
    first_sample_flags: Optional[int]
    samples: List[TrunSample]
    has_cts: bool


def parse_trun(b: Box) -> Trun:
    p = b.payload
    ver = p[0]
    flags = int.from_bytes(p[1:4], "big")
    count = u32(p, 4)
    o = 8
    data_offset = first_flags = None
    if flags & TRUN_DATA_OFFSET:
        data_offset = i32(p, o)
        o += 4
    if flags & TRUN_FIRST_SAMPLE_FLAGS:
        first_flags = u32(p, o)
        o += 4
    samples: List[TrunSample] = []
    for _ in range(count):
        s = TrunSample()
        if flags & TRUN_SAMPLE_DURATION:
            s.duration = u32(p, o); o += 4
        if flags & TRUN_SAMPLE_SIZE:
            s.size = u32(p, o); o += 4
        if flags & TRUN_SAMPLE_FLAGS:
            s.flags = u32(p, o); o += 4
        if flags & TRUN_SAMPLE_CTS:
            s.cts = int.from_bytes(p[o:o + 4], "big", signed=(ver == 1)); o += 4
        samples.append(s)
    return Trun(data_offset, first_flags, samples, bool(flags & TRUN_SAMPLE_CTS))


def build_trun(samples: Sequence, data_offset: int,
               first_sample_flags: Optional[int], has_cts: bool,
               all_flags_equal: bool) -> bytes:
    """构造 trun。

    为了完整保留关键帧/非关键帧信息，默认逐样本输出 sample_flags；
    若全部相同则用 first_sample_flags 代替（与 MediaRecorder 输出风格一致）。
    """
    flags = TRUN_DATA_OFFSET | TRUN_SAMPLE_DURATION | TRUN_SAMPLE_SIZE
    if has_cts:
        flags |= TRUN_SAMPLE_CTS
    if all_flags_equal:
        flags |= TRUN_FIRST_SAMPLE_FLAGS
    else:
        flags |= TRUN_SAMPLE_FLAGS

    p = bytearray()
    p += bytes([1 if has_cts else 0]) + flags.to_bytes(3, "big")
    p += len(samples).to_bytes(4, "big")
    p += data_offset.to_bytes(4, "big", signed=True)
    if all_flags_equal:
        p += (first_sample_flags if first_sample_flags is not None
              else (samples[0].flags if samples else 0)).to_bytes(4, "big")
    for s in samples:
        p += (s.duration or 0).to_bytes(4, "big")
        p += len(s.data).to_bytes(4, "big")
        if not all_flags_equal:
            p += (s.flags or 0).to_bytes(4, "big")
        if has_cts:
            p += (s.cts or 0).to_bytes(4, "big", signed=True)
    return box(b"trun", bytes(p))


@dataclass
class Mfhd:
    sequence_number: int


def parse_mfhd(b: Box) -> Mfhd:
    return Mfhd(u32(b.payload, 4))


def build_mfhd(seq: int) -> bytes:
    return box(b"mfhd", (0).to_bytes(4, "big") + seq.to_bytes(4, "big"))
