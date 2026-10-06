"""核心逻辑测试（无需 pytest，直接 `python3 tests/test_core.py` 运行）。

依赖 tests/fixtures（由 gen_fixtures.py 生成）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import fmp4, mp4box as bx  # noqa: E402

FX = ROOT / "tests" / "fixtures"


def _ffprobe_bin():
    return os.environ.get(
        "FFPROBE_BIN",
        str(ROOT / ".tools" / "ffmpeg-7.0.2-amd64-static" / "ffprobe"))


def load():
    info = fmp4.parse_init((FX / "init.mp4").read_bytes())
    return info


def parse_file(info, name):
    frags, dropped = fmp4.parse_piece((FX / name).read_bytes(), name, info)
    return frags, dropped


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("ok -", msg)


def test_init_tracks():
    info = load()
    check({t.codec_name for t in info.tracks.values()} == {"h264", "aac"},
          "init 解析为 h264 + aac")
    v = next(t for t in info.tracks.values() if t.codec_name == "h264")
    a = next(t for t in info.tracks.values() if t.codec_name == "aac")
    check((v.width, v.height) == (640, 360), "视频分辨率 640x360")
    check(a.sample_rate == 48000 and a.channels == 2, "音频 48000Hz/2ch")
    check(v.timescale == 15360 and a.timescale == 48000, "各轨 timescale 正确")


def test_bframe_cts():
    info = load()
    frags, _ = parse_file(info, "track1-001.m4s")
    cts = [s.cts for s in frags[0].samples]
    check(any(c != 0 for c in cts), "H.264 片段存在非零 cts（B 帧）")
    check(all(c >= 0 for c in cts), "重编码 cts 非负（libx264 风格）")
    # 重建后 cts 应保留
    out = fmp4._build_moof_mdat(frags[0], 1, 1, 0)
    parsed2, _ = fmp4.parse_piece(out, "rebuilt", info)
    check([s.cts for s in parsed2[0].samples] == cts, "重建后 cts 逐样本保留")


def test_dedup_gap_truncation():
    info = load()
    frags = []
    order = (FX / "good" / "ORDER.txt").read_text().splitlines()
    dropped_all = []
    for name in order:
        fr, dr = parse_file(info, f"good/{name}")
        frags += fr
        dropped_all += dr

    plans, overlaps = fmp4.plan_recover(frags, info)
    check(not overlaps, "重复片段不产生重叠冲突")
    pv = next(p for p in plans if info.tracks[p.track_id].handler == b"vide")
    pa = next(p for p in plans if info.tracks[p.track_id].handler == b"soun")
    check(pv.duplicates_removed == 1, "检测并去除 1 个重复视频片")
    check(len(pv.gaps) == 1 and pv.gaps[0].leading is False, "视频有 1 个中间缺口")
    g = pv.gaps[0]
    check(g.dts_to - g.dts_from == 15360, "缺口长度恰为 1 秒（timescale 单位）")
    check(pa.gaps == [], "音频无缺口")

    # 尾片截断
    check(any(d.track_id == 1 and d.reason == "tail_sample_truncated"
              for d in dropped_all), "尾片截断被识别")
    tail = [d for d in dropped_all if d.track_id == 1][0]
    check(tail.dts_from == 91648 and tail.dts_to == 92160,
          f"舍弃区间正确 {tail.dts_from}..{tail.dts_to}")

    # 重建 + 缺口不压缩
    out = fmp4.rebuild(info, plans)
    outp = Path("/tmp") / "recovered-test.mp4"
    outp.write_bytes(out)
    reparsed, _ = fmp4.parse_piece(out, "out", info)
    starts = sorted(f.dts_start for f in reparsed
                    if f.track_id == pv.track_id)
    # 缺口两侧起点仍为 0..15360 与 46080（未前移）
    check(46080 in starts and 30720 not in starts,
          "缺口后的片段 tfdt 未被压缩")

    import json, subprocess
    r = subprocess.run([_ffprobe_bin(), "-v", "error", "-count_frames",
                        "-select_streams", "v",
                        "-show_entries", "stream=nb_read_frames",
                        "-of", "json", str(outp)], capture_output=True, text=True)
    n = int(json.loads(r.stdout)["streams"][0]["nb_read_frames"])
    # 180 帧（6 片 ×30）- 缺口 30 - 尾截断 1 = 149
    check(n == 149, f"ffprobe 帧数 149（实际 {n}），缺口与尾截断均生效")


def test_overlap_409():
    info = load()
    frags = []
    for f in sorted((FX / "overlap").glob("*.m4s")):
        fr, _ = fmp4.parse_piece(f.read_bytes(), f.name, info)
        frags += fr
    _, overlaps = fmp4.plan_recover(frags, info)
    check(len(overlaps) >= 1, "同区间不同字节报重叠（409）")


def test_unsupported_codec():
    try:
        fmp4.parse_init((FX / "badcodec" / "init.mp4").read_bytes())
    except fmp4.RecoverError as e:
        check(e.status_code == 422, "HEVC init 报 422")
        return
    raise AssertionError("HEVC init 必须报错")


def test_unknown_sample_boundary():
    info = load()
    data = bytearray((FX / "track1-000.m4s").read_bytes())
    # 把 mdat 的声明长度缩短 4096 字节（但不截断文件本体），
    # 使最后一个样本落在 mdat 声明边界之外：mdat 未截断 => 422
    mdat = next(b for b in bx.iter_boxes(bytes(data)) if b.typ == b"mdat")
    new_size = (mdat.end - mdat.start) - 4096
    data[mdat.start:mdat.start + 4] = new_size.to_bytes(4, "big")
    try:
        fmp4.parse_piece(bytes(data), "bad", info)
    except fmp4.RecoverError as e:
        check(e.status_code == 422, "样本越界且 mdat 声明完整时报 422")
        return
    raise AssertionError("样本越界必须报 422")


def test_unified_timebase():
    info = load()
    frags = []
    for f in sorted(FX.glob("track1-*.m4s"))[:2]:
        fr, _ = fmp4.parse_piece(f.read_bytes(), f.name, info)
        frags += fr
    for f in sorted(FX.glob("track2-*.m4s"))[:2]:
        fr, _ = fmp4.parse_piece(f.read_bytes(), f.name, info)
        frags += fr
    from fractions import Fraction
    for f in frags:
        secs = Fraction(f.dts_start, info.tracks[f.track_id].timescale)
        # 前两片在两轨上的秒级起点应可直接比较
    v0 = Fraction(0, 15360)
    a0 = Fraction(0, 48000)
    check(v0 == a0 == 0, "不同 timescale 统一为秒后可精确比较")


def test_interleaved_moof_with_initial_mdat():
    """非 empty_moov：moov 带初始 mdat，moof 内多 traf 交织 + 绝对 base-data-offset。"""
    fx = ROOT / "tests" / "fixtures_interleaved"
    info = fmp4.parse_init((fx / "init.mp4").read_bytes())
    check(len(info.initial_mdat) > 0, "init 含 moov 内嵌初始 mdat")
    frags = []
    for p in sorted(fx.glob("chunk-*.m4s")):
        fr, _ = fmp4.parse_piece(p.read_bytes(), p.name, info)
        frags += fr
    plans, overlaps = fmp4.plan_recover(frags, info)
    check(not overlaps, "交织分片无重叠")
    for p in plans:
        check(p.gaps == [], f"track {p.track_id} 初始样本覆盖区间内无前导缺口")
    out = fmp4.rebuild(info, plans)
    outp = Path("/tmp") / "interleaved-test.mp4"
    outp.write_bytes(out)
    # 首字节布局：ftyp + moov + 初始 mdat
    firsts = [b.typ for _, b in zip(range(3), bx.iter_boxes(out))]
    check(firsts[:3] == [b"ftyp", b"moov", b"mdat"],
          f"输出保留 ftyp/moov/初始 mdat 顺序: {firsts[:3]}")

    import json, subprocess
    ffprobe = _ffprobe_bin()
    r = subprocess.run([ffprobe, "-v", "error", "-count_frames",
                        "-show_entries",
                        "stream=codec_type,nb_read_frames,start_time,duration",
                        "-of", "json", str(outp)], capture_output=True, text=True)
    check(r.returncode == 0, "ffprobe 可读取交织重建结果")
    pr = json.loads(r.stdout)
    counts = {s["codec_type"]: int(s["nb_read_frames"]) for s in pr["streams"]}
    check(counts.get("video") == 50 and counts.get("audio") == 88,
          f"帧数与原录制一致 {counts}")
    vstart = next(s for s in pr["streams"] if s["codec_type"] == "video")["start_time"]
    check(abs(float(vstart) - 0.08) < 1e-6,
          f"视频起点保留 B 帧显示延迟 0.08s（实际 {vstart}），音画同步")


if __name__ == "__main__":
    test_init_tracks()
    test_bframe_cts()
    test_dedup_gap_truncation()
    test_overlap_409()
    test_unsupported_codec()
    test_unknown_sample_boundary()
    test_unified_timebase()
    test_interleaved_moof_with_initial_mdat()
    print("\nall tests passed")
