"""fMP4 解析、校验与重建核心。

处理流程::

    parse_init()   解析 init.mp4（ftyp/moov/mvex、track、edit list、编码）
    parse_piece()  解析每一个上传片段（可含 styp/sidx + 若干 moof/mdat），
                   允许尾片截断 —— 尽力保留完整样本
    plan_recover() 每轨按 DTS 排序，去重、检测重叠与缺口
    rebuild()      不转码，原样复制样本字节，重建规范化 moof/mdat
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Dict, List, Optional, Tuple

from . import mp4box as bx
from .mp4box import Box


class RecoverError(Exception):
    """可映射为 HTTP 状态码的业务错误。"""

    def __init__(self, status_code: int, code: str, message: str, details=None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}


# ---------------------------------------------------------------- 数据模型

@dataclass
class TrackInfo:
    track_id: int
    handler: bytes            # b"vide" / b"soun"
    codec_fourcc: bytes       # avc1/avc3/mp4a
    codec_name: str           # h264 / aac
    timescale: int
    mdhd_duration: int
    width: Optional[int] = None
    height: Optional[int] = None
    sample_rate: Optional[int] = None
    channels: Optional[int] = None
    edit_list: List[dict] = field(default_factory=list)
    trex_default_duration: int = 0
    trex_default_size: int = 0
    trex_default_flags: int = 0
    trex_sample_desc_index: int = 1


@dataclass
class InitInfo:
    tracks: Dict[int, TrackInfo]
    movie_timescale: int
    movie_duration: int
    ftyp: bytes
    moov: bytes
    moov_box: Box
    mvex_present: bool
    initial_mdat: bytes = b""  # moov 内嵌初始样本对应的 mdat（非 empty_moov 时）
    # 每轨初始 mdat 覆盖的 DTS 区间（来自 stbl/stts/ctts），无则 None
    initial_coverage: Optional[Dict[int, Tuple[int, int]]] = None


@dataclass
class Sample:
    dts: int
    duration: int
    flags: int
    cts: int
    data: bytes
    desc_index: int = 1

    @property
    def is_rap(self) -> bool:
        # sample_flags: 高 16 位中低 2 位为 sample_depends_on / sample_is_depended_on；
        # sample_depends_on == 2 表示该样本依赖他人（非参考帧）
        return ((self.flags >> 24) & 0x03) != 2

    @property
    def pts(self) -> int:
        return self.dts + self.cts


@dataclass
class DroppedTail:
    track_id: int
    reason: str
    dts_from: Optional[int]    # 舍弃区间（track timescale，媒体时间）
    dts_to: Optional[int]

    def report(self, timescale: int) -> dict:
        def sec(v):
            return None if v is None else float(Fraction(v, timescale))
        return {
            "track_id": self.track_id,
            "reason": self.reason,
            "dts_from": self.dts_from,
            "dts_to": self.dts_to,
            "seconds_from": sec(self.dts_from),
            "seconds_to": sec(self.dts_to),
        }


@dataclass
class Fragment:
    track_id: int
    source: str                       # 来源文件名/标识
    seq: int                          # 原始 mfhd sequence number
    dts_start: int                    # tfdt（媒体时间）
    samples: List[Sample]
    payload_hash: str = ""
    truncated: bool = False

    @property
    def dts_end(self) -> int:
        end = self.dts_start
        for s in self.samples:
            end += s.duration
        return end

    @property
    def byte_length(self) -> int:
        return sum(len(s.data) for s in self.samples)


# ---------------------------------------------------------------- init 解析

_VIDEO_OK = {b"avc1", b"avc3"}
_AUDIO_OK = {b"mp4a"}


def _resolve_codec(stsd: bx.StsdInfo, entry_box_payload: bytes) -> Tuple[str, bytes]:
    """返回 (codec 名, fourcc)；不支持时抛 422。"""
    et = stsd.entry_type
    if et in _VIDEO_OK:
        return "h264", et
    if et == b"mp4a":
        # 通过 esds objectTypeIndication 判断是否 AAC
        oti = _esds_oti(entry_box_payload)
        if oti in (0x40, 0x41, 0x66, 0x67, 0x68, 0x69, 0x6B):
            # 0x40 Audio ISO/IEC 14496-3 (AAC family)；
            # 0x66-0x6B 为 DTS/Opus 等扩展，需要再排除非 AAC
            if oti in (0x40, 0x41):
                return "aac", et
            if oti == 0x66:  # DTS
                pass
        # Opus 常见为 mp4a + 0x4C(?) 实际用 OpusSpecificBox('dOps') 或 OTI 0x21/Opus
        raise RecoverError(422, "UNSUPPORTED_CODEC",
                           f"音频 objectTypeIndication=0x{oti:02x} 不是 AAC")
    known = {
        b"hvc1": "H.265/HEVC", b"hev1": "H.265/HEVC", b"av01": "AV1",
        b"vp09": "VP9", b"mp4v": "MPEG-4 Visual", b"enca": "加密音频",
        b"encv": "加密视频", b"ac-3": "AC-3", b"ec-3": "E-AC-3",
        b"fLaC": "FLAC", b"Opus": "Opus",
    }
    name = known.get(et, et.decode("latin1", "replace"))
    raise RecoverError(422, "UNSUPPORTED_CODEC", f"不支持的编码 {name}，仅支持 H.264 与 AAC")


def _esds_oti(stsd_entry_payload: bytes) -> int:
    """从 mp4a entry 内的 esds 解析 objectTypeIndication。入参为整个 entry（含头 8 字节）。"""
    for c in bx.iter_boxes(stsd_entry_payload, 8 + 28, len(stsd_entry_payload)):
        if c.typ != b"esds":
            continue
        p = c.payload
        i = 4  # version/flags
        if i >= len(p) or p[i] != 0x03:
            break
        i, _ = _tag_len(p, i)
        i += 2  # ES_ID
        i += 1  # streamDependenceFlag/URL_Flag/OCRstreamFlag/streamPriority
        if i < len(p) and p[i] == 0x04:
            i, body_end = _tag_len(p, i)
            if i < len(p):
                return p[i]
        break
    raise RecoverError(422, "UNSUPPORTED_CODEC", "无法从 esds 解析音频 objectTypeIndication")


def _tag_len(p: bytes, i: int) -> Tuple[int, int]:
    """跳过 1 字节 tag + 变长 size，返回 (内容起点, 内容结束)。"""
    tag_pos = i
    i += 1
    size = 0
    for _ in range(5):
        b = p[i]
        i += 1
        size = (size << 7) | (b & 0x7F)
        if not (b & 0x80):
            break
    return i + 0, i + size  # noqa: type ignore


def _initial_coverages(blob: bytes, moov_box: Box) -> Dict[int, Tuple[int, int]]:
    """从 moov/stbl/stts 计算每轨初始（非分片）样本的 DTS 覆盖区间。

    适用于非 empty_moov 的 fMP4：moov 内 stbl 描述首个 mdat 中的样本，
    后续 moof/tfdt 接续其后。
    """
    cov: Dict[int, Tuple[int, int]] = {}
    for trak in bx.find_all(blob, moov_box, b"trak"):
        tkhd_b = bx.find_path(blob, trak, b"tkhd")
        stts_b = bx.find_path(blob, trak, b"mdia", b"minf", b"stbl", b"stts")
        if tkhd_b is None or stts_b is None:
            continue
        tid = bx.parse_tkhd(tkhd_b).track_id
        p = stts_b.payload
        count = bx.u32(p, 4)
        o = 8
        total = 0
        for _ in range(count):
            n = bx.u32(p, o)
            delta = bx.u32(p, o + 4)
            total += n * delta
            o += 8
        if total > 0:
            cov[tid] = (0, total)
    return cov


def parse_init(blob: bytes) -> InitInfo:
    if len(blob) < 16:
        raise RecoverError(422, "INVALID_INIT", "init 文件过小，不是合法 MP4")

    ftyp = moov = None
    mvex_present = False
    initial_mdat = b""
    for b in bx.iter_boxes(blob):
        if b.truncated:
            raise RecoverError(422, "INVALID_INIT", f"init 中 {b.typ.decode()} box 被截断")
        if b.typ == b"ftyp":
            ftyp = blob[b.start:b.end]
        elif b.typ == b"moov":
            moov = blob[b.start:b.end]
            moov_box = b
            if bx.find_child(blob, b, b"mvex") is not None:
                mvex_present = True
        elif b.typ == b"mdat":
            # 非 empty_moov 的 fMP4：moov 的 stbl/stco 指向内嵌初始样本，
            # 该 mdat 必须随 moov 原样保留到输出。
            initial_mdat += blob[b.start:b.end]
    if moov is None:
        raise RecoverError(422, "INVALID_INIT", "init 缺少 moov box")
    if ftyp is None:
        ftyp = bx.box(b"ftyp", b"iso5" + bytes(4) + b"iso5dashmp41")

    mvhd_b = bx.find_path(blob, moov_box, b"mvhd")
    if mvhd_b is None:
        raise RecoverError(422, "INVALID_INIT", "moov 缺少 mvhd")
    mvhd = bx.parse_mvhd(mvhd_b)

    trex: Dict[int, bx.Trex] = {}
    mvex_b = bx.find_child(blob, moov_box, b"mvex")
    if mvex_b is not None:
        for t in bx.children(blob, mvex_b):
            if t.typ == b"trex":
                r = bx.parse_trex(t)
                trex[r.track_id] = r

    tracks: Dict[int, TrackInfo] = {}
    for trak in bx.find_all(blob, moov_box, b"trak"):
        tkhd_b = bx.find_path(blob, trak, b"tkhd")
        mdhd_b = bx.find_path(blob, trak, b"mdia", b"mdhd")
        hdlr_b = bx.find_path(blob, trak, b"mdia", b"hdlr")
        stsd_b = bx.find_path(blob, trak, b"mdia", b"minf", b"stbl", b"stsd")
        if not all((tkhd_b, mdhd_b, hdlr_b, stsd_b)):
            raise RecoverError(422, "INVALID_INIT", "trak 结构不完整")
        tkhd = bx.parse_tkhd(tkhd_b)
        mdhd = bx.parse_mdhd(mdhd_b)
        hdlr = bx.parse_hdlr(hdlr_b)
        stsd = bx.parse_stsd(blob, stsd_b)

        if hdlr.handler_type not in (b"vide", b"soun"):
            raise RecoverError(
                422, "UNSUPPORTED_CODEC",
                f"track {tkhd.track_id} handler={hdlr.handler_type.decode('latin1')}，仅支持视频/音频")

        # stsd entry payload（相对 entry 头）
        p = stsd_b.payload
        entry_size = bx.u32(p, 8)
        entry_slice = p[8:8 + entry_size]
        codec, fourcc = _resolve_codec(stsd, entry_slice)
        if hdlr.handler_type == b"vide" and codec != "h264":
            raise RecoverError(422, "UNSUPPORTED_CODEC", "视频轨不是 H.264")
        if hdlr.handler_type == b"soun" and codec != "aac":
            raise RecoverError(422, "UNSUPPORTED_CODEC", "音频轨不是 AAC")

        edits: List[dict] = []
        elst_b = bx.find_path(blob, trak, b"edts", b"elst")
        if elst_b is not None:
            elst = bx.parse_elst(elst_b)
            for e in elst.edits:
                edits.append({
                    "segment_duration_movie_time": e.segment_duration,
                    "media_time": e.media_time,
                    "media_rate": round(e.media_rate, 6),
                    "empty": e.media_time == -1,
                })

        r = trex.get(tkhd.track_id)
        ti = TrackInfo(
            track_id=tkhd.track_id, handler=hdlr.handler_type,
            codec_fourcc=fourcc, codec_name=codec,
            timescale=mdhd.timescale, mdhd_duration=mdhd.duration,
            edit_list=edits,
            trex_default_duration=r.default_sample_duration if r else 0,
            trex_default_size=r.default_sample_size if r else 0,
            trex_default_flags=r.default_sample_flags if r else 0,
            trex_sample_desc_index=r.default_sample_description_index if r else 1,
        )
        if codec == "h264":
            # VisualSampleEntry：SampleEntry 头 8（size+type）后 6 reserved+
            # data_reference_index(2)、再 16 字节保留，width/height 在
            # entry 内偏移 32/34（stsd payload 偏移 40/42）。
            ti.width = bx.u16(entry_slice, 32)
            ti.height = bx.u16(entry_slice, 34)
        else:
            ti.channels = stsd.audio_channels
            ti.sample_rate = stsd.audio_sample_rate
        tracks[ti.track_id] = ti

    if not tracks:
        raise RecoverError(422, "INVALID_INIT", "moov 中没有任何 track")

    # 加密检查：track 内存在 saiz/senc 或 entry 为 encv/enca（上面已拒绝）
    for trak in bx.find_all(blob, moov_box, b"trak"):
        for sub in (bx.find_path(blob, trak, b"mdia", b"minf", b"stbl"),):
            if sub is not None:
                for c in bx.children(blob, sub):
                    if c.typ in (b"saiz", b"saio", b"senc", b"pssh"):
                        raise RecoverError(422, "ENCRYPTED",
                                           "init 含加密相关 box（saiz/senc/pssh），仅支持未加密 MP4")

    return InitInfo(tracks=tracks, movie_timescale=mvhd.timescale,
                    movie_duration=mvhd.duration, ftyp=ftyp, moov=moov,
                    moov_box=moov_box, mvex_present=mvex_present,
                    initial_mdat=initial_mdat,
                    initial_coverage=_initial_coverages(blob, moov_box)
                    if initial_mdat else None)


# ---------------------------------------------------------------- 片段解析

def parse_piece(blob: bytes, source: str, init: InitInfo) -> Tuple[List[Fragment], List[DroppedTail]]:
    """解析上传片段，返回 (每轨 Fragment 列表, 截断舍弃信息)。

    一个文件里允许包含 styp/sidx/prft 等头 box 和多个 moof/mdat。
    """
    fragments: List[Fragment] = []
    dropped: List[DroppedTail] = []

    boxes = list(bx.iter_boxes(blob))
    i = 0
    while i < len(boxes):
        b = boxes[i]
        if b.typ != b"moof":
            if b.truncated:
                break
            i += 1
            continue

        if b.truncated:
            # moof 头都不完整，无法确定任何样本
            dropped.append(DroppedTail(-1, "moof_truncated", None, None))
            break

        moof = b
        # 仅收集本 moof 之后、下一个 moof 之前的 mdat（非 empty_moov 文件
        # 第一个 moof 之前可能存在普通 mdat，不得关联到任何分片）。
        mdats_after: List[Box] = []
        j = i + 1
        while j < len(boxes):
            nxt = boxes[j]
            if nxt.typ == b"moof":
                break
            if nxt.typ == b"mdat" and nxt.start >= moof.end:
                mdats_after.append(nxt)
            j += 1
        mdats = mdats_after

        try:
            frags = _parse_moof(blob, moof, mdats, source, init, dropped)
            fragments.extend(frags)
        except RecoverError:
            raise
        except Exception as exc:  # 结构无法解析 -> 422
            raise RecoverError(422, "INVALID_FRAGMENT",
                               f"{source}: 无法解析 moof: {exc}")

        i = j

    return fragments, dropped


def _parse_moof(blob: bytes, moof: Box, mdats: List[Box], source: str,
                init: InitInfo, dropped: List[DroppedTail]) -> List[Fragment]:
    mfhd_b = bx.find_child(blob, moof, b"mfhd")
    if mfhd_b is None:
        raise RecoverError(422, "INVALID_FRAGMENT", f"{source}: moof 缺少 mfhd")
    seq = bx.parse_mfhd(mfhd_b).sequence_number

    trafs = bx.find_all(blob, moof, b"traf")
    if not trafs:
        raise RecoverError(422, "INVALID_FRAGMENT", f"{source}: moof 内没有 traf")

    # 合并可用 mdat 字节区间（blob 内绝对偏移），整个 moof 共享
    mdat_ranges = []
    any_truncated_mdat = False
    for m in mdats:
        actual_end = min(m.end, len(blob))
        mdat_ranges.append((m.data_start, actual_end))
        if m.truncated:
            any_truncated_mdat = True
    if not mdat_ranges:
        raise RecoverError(422, "INVALID_FRAGMENT",
                           f"{source}: moof 后缺少 mdat，无法确定样本边界")

    out: List[Fragment] = []
    for traf in trafs:
        tfhd_b = bx.find_child(blob, traf, b"tfhd")
        tfdt_b = bx.find_child(blob, traf, b"tfdt")
        trun_b = bx.find_child(blob, traf, b"trun")
        if tfhd_b is None or trun_b is None:
            raise RecoverError(422, "INVALID_FRAGMENT",
                               f"{source}: traf 缺少 tfhd/trun")
        tfhd = bx.parse_tfhd(tfhd_b)
        trun = bx.parse_trun(trun_b)
        if tfhd.track_id not in init.tracks:
            raise RecoverError(422, "UNKNOWN_TRACK",
                               f"{source}: 片段引用了 init 中不存在的 track {tfhd.track_id}")
        track = init.tracks[tfhd.track_id]

        # 加密拒绝
        for c in bx.children(blob, traf):
            if c.typ in (b"senc", b"saiz", b"saio"):
                raise RecoverError(422, "ENCRYPTED",
                                   f"{source}: track {tfhd.track_id} 片段已加密")

        if tfhd.duration_is_empty and not trun.samples:
            continue
        if tfdt_b is None:
            raise RecoverError(422, "INVALID_FRAGMENT",
                               f"{source}: track {tfhd.track_id} 缺少 tfdt，无法确定 DTS")
        dts0 = bx.parse_tfdt(tfdt_b).base_media_decode_time

        # ---- 样本基址 ----
        # ISO 14496-12 三种来源：
        #  * tfhd base-data-offset（绝对偏移，分片独立上传时会失效）；
        #  * default-base-is-moof（基址=moof 起点）；
        #  * 隐式（第一个 traf 为 moof 起点，其后为前一 traf 数据末尾）。
        if trun.data_offset is None:
            raise RecoverError(422, "INVALID_FRAGMENT",
                               f"{source}: track {tfhd.track_id} trun 缺少 data_offset，"
                               "无法确定样本边界")
        trun_off = trun.data_offset

        def _valid_base(base: int) -> bool:
            abs0 = base + trun_off
            return any(rs <= abs0 < re_ for rs, re_ in mdat_ranges)

        if tfhd.base_data_offset is not None:
            base = tfhd.base_data_offset
            if not _valid_base(base) and _valid_base(moof.start):
                # 分片脱离原文件独立上传：绝对偏移失效，回退到 moof 相对
                base = moof.start
        elif tfhd.default_base_is_moof:
            base = moof.start
        else:
            traf_idx = trafs.index(traf)
            base = moof.start if traf_idx == 0 else _previous_traf_data_end(
                blob, trafs[:traf_idx], moof)

        # 第一个样本的绝对偏移；交织 mdat 中各样本依次排布
        cursor = base + trun.data_offset

        first_flags = trun.first_sample_flags

        samples: List[Sample] = []
        cur_dts = dts0
        first_dropped_dts: Optional[int] = None
        truncated_samples = 0

        for idx, ts in enumerate(trun.samples):
            size = ts.size if ts.size is not None else (
                tfhd.default_sample_size if tfhd.default_sample_size is not None
                else track.trex_default_size)
            duration = ts.duration if ts.duration is not None else (
                tfhd.default_sample_duration if tfhd.default_sample_duration is not None
                else track.trex_default_duration)
            if ts.flags is not None:
                flags = ts.flags
            elif idx == 0 and first_flags is not None:
                flags = first_flags
            elif tfhd.default_sample_flags is not None:
                flags = tfhd.default_sample_flags
            else:
                flags = track.trex_default_flags
            cts = ts.cts or 0
            desc_index = track.trex_sample_desc_index

            if not size or size <= 0:
                raise RecoverError(422, "UNKNOWN_SAMPLE_BOUNDARY",
                                   f"{source}: track {tfhd.track_id} 第 {idx} 个样本大小为 0/未知")

            s_start = cursor
            s_end = cursor + size
            contained_in = any(
                rs <= s_start and s_end <= re_ for (rs, re_) in mdat_ranges)
            if not contained_in:
                in_any = any(rs <= s_start < re_ for (rs, re_) in mdat_ranges)
                if in_any and any_truncated_mdat:
                    # 尾片截断：本样本及之后全部舍弃（含其他 traf 已写入的
                    # 完整样本仍然保留，因为 mdat 中它们位于更靠前的位置）
                    if first_dropped_dts is None:
                        first_dropped_dts = cur_dts
                    truncated_samples = len(trun.samples) - idx
                    break
                raise RecoverError(422, "UNKNOWN_SAMPLE_BOUNDARY",
                                   f"{source}: track {tfhd.track_id} 样本数据超出 mdat 边界")

            data = blob[s_start:s_end]
            samples.append(Sample(dts=cur_dts, duration=duration, flags=flags,
                                  cts=cts, data=data, desc_index=desc_index))
            cur_dts += duration
            cursor = s_end

        if first_dropped_dts is not None and truncated_samples:
            # 推算舍弃区间终点
            drop_end = cur_dts
            # 尝试用 trun 中剩余样本 duration 估算更精确的终点
            est = first_dropped_dts
            for ts in trun.samples[len(samples):]:
                d = ts.duration if ts.duration is not None else (
                    tfhd.default_sample_duration if tfhd.default_sample_duration is not None
                    else track.trex_default_duration)
                est += d or 0
            if est > first_dropped_dts:
                drop_end = est
            dropped.append(DroppedTail(
                tfhd.track_id,
                "tail_sample_truncated",
                first_dropped_dts,
                drop_end if drop_end > first_dropped_dts else None,
            ))

        if not samples:
            # 整个 traf 没有完整样本（整片截断到第一个样本之内）
            dropped.append(DroppedTail(
                tfhd.track_id, "fragment_all_samples_truncated",
                dts0, dts0 if dts0 else None))
            continue

        frag = Fragment(track_id=tfhd.track_id, source=source, seq=seq,
                        dts_start=dts0, samples=samples, truncated=any_truncated_mdat)
        h = hashlib.sha256()
        for s in frag.samples:
            h.update(s.data)
        frag.payload_hash = h.hexdigest()
        out.append(frag)

    return out


def _previous_traf_data_end(blob: bytes, previous_trafs: List[Box], moof: Box) -> int:
    """推算前一 traf 样本数据的结束偏移（用于默认 base 的隐式继承）。"""
    end = moof.start
    for traf in previous_trafs:
        trun_b = bx.find_child(blob, traf, b"trun")
        if trun_b is None:
            continue
        trun = bx.parse_trun(trun_b)
        if trun.data_offset is None:
            continue
        total = 0
        for s in trun.samples:
            total += s.size or 0
        cand = moof.start + trun.data_offset + total
        end = max(end, cand)
    return end


# ---------------------------------------------------------------- 去重 / 重叠 / 缺口

@dataclass
class Gap:
    track_id: int
    dts_from: int
    dts_to: int
    leading: bool

    def report(self, timescale: int) -> dict:
        return {
            "track_id": self.track_id,
            "dts_from": self.dts_from,
            "dts_to": self.dts_to,
            "seconds_from": float(Fraction(self.dts_from, timescale)),
            "seconds_to": float(Fraction(self.dts_to, timescale)),
            "duration_seconds": float(Fraction(self.dts_to - self.dts_from, timescale)),
            "leading": self.leading,
        }


@dataclass
class Overlap:
    track_id: int
    a: Fragment
    b: Fragment
    dts_from: int
    dts_to: int


@dataclass
class TrackPlan:
    track_id: int
    kept: List[Fragment]
    duplicates_removed: int
    gaps: List[Gap]


def plan_recover(frags: List[Fragment], init: InitInfo
                 ) -> Tuple[List[TrackPlan], List[Overlap]]:
    """每轨排序、去重、找缺口；发现非重复重叠返回 overlap（-> 409）。"""
    by_track: Dict[int, List[Fragment]] = {}
    for f in frags:
        by_track.setdefault(f.track_id, []).append(f)

    plans: List[TrackPlan] = []
    overlaps: List[Overlap] = []

    for tid in sorted(init.tracks):
        track_frags = by_track.get(tid, [])
        # 稳定排序：起点优先，随后按 seq/来源
        ordered = sorted(track_frags,
                         key=lambda f: (f.dts_start, f.dts_end, f.seq, f.source))

        # 去重：区间 + 字节均相同。逐条与已保留片段比较。
        kept: List[Fragment] = []
        dup_removed = 0
        i = 0
        while i < len(ordered):
            f = ordered[i]
            is_dup = False
            for k in kept:
                if (k.dts_start == f.dts_start and k.dts_end == f.dts_end
                        and k.byte_length == f.byte_length
                        and k.payload_hash == f.payload_hash):
                    is_dup = True
                    break
            if is_dup:
                dup_removed += 1
            else:
                kept.append(f)
            i += 1

        # 重叠 & 缺口扫描（kept 已按 start 排序）
        gaps: List[Gap] = []
        cov_end = 0
        if init.initial_coverage and tid in init.initial_coverage:
            cov_end = init.initial_coverage[tid][1]
        prev: Optional[Fragment] = None
        for f in kept:
            if prev is None:
                # 前导缺口只在首个分片起点超过 moov 初始样本覆盖终点时存在
                if f.dts_start > cov_end:
                    gaps.append(Gap(tid, cov_end, f.dts_start, leading=True))
            else:
                if f.dts_start > prev.dts_end:
                    gaps.append(Gap(tid, prev.dts_end, f.dts_start, leading=False))
                elif f.dts_start < prev.dts_end:
                    # 区间重叠（起点相同但内容不同也在此）
                    if not (f.dts_start == prev.dts_start and f.dts_end == prev.dts_end):
                        overlaps.append(Overlap(
                            tid, prev, f,
                            max(f.dts_start, prev.dts_start),
                            min(f.dts_end, prev.dts_end)))
                    else:
                        # 完全同区间但字节不同 -> 冲突
                        overlaps.append(Overlap(tid, prev, f, f.dts_start, f.dts_end))
            prev = f
        plans.append(TrackPlan(tid, kept, dup_removed, gaps))

    return plans, overlaps


# ---------------------------------------------------------------- 重建

def _ensure_mvex(init: InitInfo) -> bytes:
    """若 init 的 moov 缺少 mvex，按各轨补 trex（fMP4 必需）。"""
    if init.mvex_present:
        return init.moov
    blob = init.moov
    # 定位 moov 内最后一个 trak 之后插入
    inner = list(bx.iter_boxes(blob))
    assert inner and inner[0].typ == b"moov"
    moov_b = inner[0]
    payload = bytearray(blob[moov_b.data_start:moov_b.end])
    trexes = b""
    for tid in sorted(init.tracks):
        t = init.tracks[tid]
        # trex: version/flags(4) track_ID(4) desc_index(4) duration(4) size(4) flags(4)
        body = (bytes(4) + tid.to_bytes(4, "big") +
                (1).to_bytes(4, "big") +
                t.trex_default_duration.to_bytes(4, "big") +
                t.trex_default_size.to_bytes(4, "big") +
                t.trex_default_flags.to_bytes(4, "big"))
        trexes += bx.box(b"trex", body)
    mvex = bx.box(b"mvex", trexes)
    payload += mvex
    return bx.box(b"moov", bytes(payload))


def _build_moof_mdat(frag: Fragment, seq: int,
                     desc_index: int, default_flags: int) -> bytes:
    """重建单 traf 的 moof + mdat，样本字节原样复制。"""
    payload = b"".join(s.data for s in frag.samples)
    mdat = bx.box(b"mdat", payload)

    has_cts = any(s.cts != 0 for s in frag.samples)
    # 是否所有样本 flags 一致（用 first_sample_flags 紧凑表达）
    all_flags = {s.flags for s in frag.samples}
    all_equal = len(all_flags) == 1
    first_flags = next(iter(all_flags)) if all_equal else (
        frag.samples[0].flags if frag.samples else default_flags)

    tfhd = bx.build_tfhd(frag.track_id, desc_index,
                         default_flags if not all_equal else None)
    tfdt = bx.build_tfdt(frag.dts_start)

    class _V:
        pass
    views = []
    for s in frag.samples:
        v = _V()
        v.duration = s.duration
        v.flags = s.flags
        v.cts = s.cts
        v.data = s.data
        views.append(v)

    # data_offset = moof 总长 + mdat header(8)。
    # moof = box(moof, mfhd + box(traf, tfhd + tfdt + trun))
    # trun 大小只取决于样本数与 flags 集合，data_offset 为固定 4 字节，
    # 因此先用占位 data_offset 组装一次，得到 moof 实际长度后回填。
    def build(data_offset: int) -> bytes:
        trun = bx.build_trun(views, data_offset, first_flags, has_cts, all_equal)
        traf = bx.box(b"traf", tfhd + tfdt + trun)
        return bx.box(b"moof", bx.build_mfhd(seq) + traf)

    placeholder = build(0)
    data_offset = len(placeholder) + 8
    moof = build(data_offset)
    return moof + mdat


def rebuild(init: InitInfo, plans: List[TrackPlan]) -> bytes:
    """按统一时间轴交错输出 moof/mdat（不转码）。"""
    out = bytearray()
    out += init.ftyp
    out += _ensure_mvex(init)
    # moov 内嵌初始样本（非 empty_moov 录制常见）——原样保留其 mdat，
    # moov 内 stco 偏移无需修改（ftyp/moov 与原始顺序、位置一致）。
    out += init.initial_mdat

    # 统一时间轴比较键：精确分数秒；同刻先视频后音频，再按 track_id
    def sort_key(f: Fragment):
        ts = init.tracks[f.track_id].timescale
        return (Fraction(f.dts_start, ts),
                0 if init.tracks[f.track_id].handler == b"vide" else 1,
                f.track_id)

    # 每轨内部按 DTS 排序，再按统一时间轴全局交错（同刻先视频）
    ordered_per_track = {p.track_id: sorted(p.kept, key=lambda f: f.dts_start)
                         for p in plans}
    pool: List[Fragment] = []
    for fs in ordered_per_track.values():
        pool.extend(fs)
    pool.sort(key=sort_key)

    # mfhd 全局单调递增
    for n, frag in enumerate(pool, start=1):
        t = init.tracks[frag.track_id]
        out += _build_moof_mdat(frag, n, t.trex_sample_desc_index,
                                t.trex_default_flags)
    return bytes(out)
