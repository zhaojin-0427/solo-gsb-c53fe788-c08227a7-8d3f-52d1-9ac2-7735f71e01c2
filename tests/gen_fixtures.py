"""生成 fMP4 修复服务的测试素材。

1. ffmpeg lavfi 生成约 6s 含 B 帧 H.264 + AAC 的完整 fMP4（每秒一个 moof）；
2. 用本项目解析器拆出 init 与逐轨分片，并用重建器规范化为单 moof/mdat 文件；
3. 构造损坏上传集合：乱序 + 重复 + 缺片 + 尾片截断；
4. 构造 409 场景：同区间不同字节。
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import fmp4, mp4box as bx  # noqa: E402

FFMPEG = "ffmpeg"


def gen_raw_fmp4(work: Path, seconds: int = 6) -> Path:
    raw = work / "raw.mp4"
    cmd = [
        FFMPEG, "-y",
        "-f", "lavfi", "-i",
        f"testsrc2=size=640x360:rate=30:duration={seconds}",
        "-f", "lavfi", "-i",
        f"sine=frequency=440:sample_rate=48000:duration={seconds}",
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-bf", "3", "-g", "30", "-keyint_min", "30", "-sc_threshold", "0",
        "-profile:v", "high", "-level", "3.1", "-b_strategy", "1",
        "-c:a", "aac", "-b:a", "64k", "-ar", "48000", "-ac", "2",
        "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
        "-frag_duration", "1000000",
        raw.name,
    ]
    r = subprocess.run(cmd, cwd=work, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr[-2000:])
        raise SystemExit("ffmpeg failed")
    return raw


def split(raw: Path, dest: Path):
    blob = raw.read_bytes()
    init_bytes = b""
    first_moof = len(blob)
    for b in bx.iter_boxes(blob):
        if b.typ in (b"ftyp", b"moov"):
            init_bytes += blob[b.start:b.end]
        if b.typ == b"moof":
            first_moof = min(first_moof, b.start)

    info = fmp4.parse_init(init_bytes)
    frags, dropped = fmp4.parse_piece(blob, "raw", info)
    assert not dropped, f"原始素材不应截断: {dropped}"

    (dest / "init.mp4").write_bytes(init_bytes)

    counters: dict[int, int] = {}
    by_track: dict[int, list[tuple[int, bytes]]] = {}
    # 按 (track, dts) 排序后赋索引
    frags_sorted = sorted(frags, key=lambda f: (
        0 if info.tracks[f.track_id].handler == b"vide" else 1,
        f.track_id, f.dts_start))
    for f in frags_sorted:
        t = info.tracks[f.track_id]
        idx = counters.get(f.track_id, 0)
        counters[f.track_id] = idx + 1
        data = fmp4._build_moof_mdat(f, idx + 1,
                                     t.trex_sample_desc_index,
                                     t.trex_default_flags)
        by_track.setdefault(f.track_id, []).append((idx, data))

    for tid, lst in by_track.items():
        for idx, data in lst:
            (dest / f"track{tid}-{idx:03d}.m4s").write_bytes(data)

    print(f"tracks={sorted(counters)} per_track={counters}")
    return info, init_bytes, by_track


def build_fixtures(dest: Path):
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    work = dest / "_work"
    work.mkdir()
    raw = gen_raw_fmp4(work)
    info, init_bytes, by_track = split(raw, dest)
    video_tid = next(t for t, tr in info.tracks.items() if tr.handler == b"vide")
    audio_tid = next(t for t, tr in info.tracks.items() if tr.handler == b"soun")

    v = by_track[video_tid]
    a = by_track[audio_tid]

    # ---- good/：乱序 + 重复 + 视频缺第 3 片 + 尾片截断 ----
    good = dest / "good"
    good.mkdir()
    keep_v = [i for i, _ in v if i != 2]
    last_i = v[-1][0]
    for i, data in v:
        if i in keep_v:
            out = data[:-300] if i == last_i else data
            (good / f"v-{i:03d}.m4s").write_bytes(out)
    for i, data in a:
        (good / f"a-{i:03d}.m4s").write_bytes(data)
    dup_idx = keep_v[0]
    shutil.copyfile(good / f"v-{dup_idx:03d}.m4s", good / f"v-{dup_idx:03d}-dup.m4s")

    order_v = sorted(keep_v, reverse=True)
    order_a = sorted([i for i, _ in a], reverse=True)
    order = [f"v-{i:03d}.m4s" for i in order_v[::2]]
    order += [f"a-{i:03d}.m4s" for i in order_a]
    order += [f"v-{i:03d}.m4s" for i in order_v[1::2]]
    order += [f"v-{dup_idx:03d}-dup.m4s"]
    (good / "ORDER.txt").write_text("\n".join(order))

    # ---- overlap/：同一 DTS 区间放字节不同的两个视频片 ----
    ov = dest / "overlap"
    ov.mkdir()
    target_idx, target_data = v[1]
    (ov / f"v-{target_idx:03d}.m4s").write_bytes(target_data)
    mutated = bytearray(target_data)
    for b in bx.iter_boxes(bytes(mutated)):
        if b.typ == b"mdat":
            mutated[b.data_start + 5] ^= 0xFF
            break
    (ov / f"v-{target_idx:03d}-other.m4s").write_bytes(bytes(mutated))
    for i, data in v:
        if i != target_idx:
            (ov / f"v-{i:03d}.m4s").write_bytes(data)
    for i, data in a:
        (ov / f"a-{i:03d}.m4s").write_bytes(data)

    # ---- badcodec/：HEVC init 触发 422 ----
    bc = dest / "badcodec"
    bc.mkdir()
    r = subprocess.run([
        FFMPEG, "-y", "-f", "lavfi", "-i",
        "testsrc2=size=320x240:rate=15:duration=1",
        "-c:v", "libx265", "-tag:v", "hvc1",
        "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
        "-frag_duration", "1000000", "hevc.mp4",
    ], cwd=bc, capture_output=True, text=True)
    if r.returncode != 0:
        print("hevc gen warning:", r.stderr[-500:])
    hevc_blob = (bc / "hevc.mp4").read_bytes() if (bc / "hevc.mp4").exists() else b""
    hevc_init = b""
    for b in bx.iter_boxes(hevc_blob):
        if b.typ in (b"ftyp", b"moov"):
            hevc_init += hevc_blob[b.start:b.end]
        if b.typ == b"moof":
            break
    if hevc_init:
        (bc / "init.mp4").write_bytes(hevc_init)

    shutil.rmtree(work)
    print("fixtures ready:", dest)


def build_interleaved_fixtures(dest: Path):
    """非 empty_moov（moov 带初始样本 mdat）+ 每 moof 内音视频 traf 交织。"""
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    work = dest / "_work"
    work.mkdir()
    raw = work / "inter.mp4"
    r = subprocess.run([
        FFMPEG, "-y",
        "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25:duration=2",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "libx264", "-bf", "2", "-g", "25", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-frag_duration", "500000",
        raw.name,
    ], cwd=work, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr[-1000:])
        raise SystemExit("ffmpeg interleaved failed")
    blob = raw.read_bytes()
    first_moof = len(blob)
    for b in bx.iter_boxes(blob):
        if b.typ == b"moof":
            first_moof = b.start
            break
    (dest / "init.mp4").write_bytes(blob[:first_moof])  # ftyp+moov+初始 mdat

    # 每个 moof+mdat 作为一个上传片段
    tops = list(bx.iter_boxes(blob[first_moof:]))
    i = 0
    n = 0
    while i < len(tops):
        if tops[i].typ != b"moof":
            i += 1
            continue
        j = i + 1
        end = tops[i].end
        while j < len(tops) and tops[j].typ != b"moof":
            end = tops[j].end
            j += 1
        chunk = blob[first_moof + tops[i].start:first_moof + end]
        (dest / f"chunk-{n:03d}.m4s").write_bytes(chunk)
        n += 1
        i = j
    shutil.rmtree(work)
    print("interleaved fixtures:", n, "chunks ->", dest)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", default="tests/fixtures")
    ap.add_argument("--interleaved-dest", default="tests/fixtures_interleaved")
    args = ap.parse_args()
    build_fixtures(Path(args.dest))
    build_interleaved_fixtures(Path(args.interleaved_dest))
