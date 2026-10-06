"""分片 MP4 修复 HTTP API。

POST /recover
    form-data:
        init: init.mp4（未加密，含 ftyp+moov，H.264 视频 / AAC 音频）
        pieces: 一个或多个 fMP4 分片（乱序、重复、缺片、尾片截断均可）
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import uuid
from fractions import Fraction
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from . import fmp4
from .fmp4 import Fragment, RecoverError, parse_init, parse_piece, plan_recover, rebuild

JOBS_DIR = Path(os.environ.get("RECOVER_JOBS_DIR", "/tmp/fmp4-recover/jobs"))
FFPROBE_BIN = os.environ.get("FFPROBE_BIN", "ffprobe")
FFMPEG_BIN = os.environ.get("FFMPEG_BIN", "ffmpeg")

app = FastAPI(
    title="分片 MP4 修复服务",
    description="接收未加密 init.mp4 与乱序/重复/缺片 fMP4 分片，还原时间轴并输出无转码 MP4。",
    version="1.0.0",
)


@app.get("/")
async def root():
    return {
        "service": "fmp4-recover",
        "endpoints": {"POST /recover": "修复分片", "GET /download/{job_id}": "下载修复结果",
                      "GET /healthz": "健康检查"},
    }


@app.get("/healthz")
async def healthz():
    return {"ok": True}


def _err(exc: RecoverError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code,
                        content={"error": {"code": exc.code, "message": exc.message,
                                           "details": exc.details}})


def _track_report(track: fmp4.TrackInfo, plan: fmp4.TrackPlan,
                  dropped: List[fmp4.DroppedTail], received: int) -> dict:
    ts = track.timescale

    frags = plan.kept
    samples = sum(len(f.samples) for f in frags)
    if frags:
        dts_min = min(f.dts_start for f in frags)
        dts_max = max(f.dts_end for f in frags)
    else:
        dts_min = dts_max = 0
    bytes_total = sum(f.byte_length for f in frags)

    return {
        "track_id": track.track_id,
        "kind": "video" if track.handler == b"vide" else "audio",
        "codec": track.codec_name,
        "timescale": ts,
        "width": track.width,
        "height": track.height,
        "sample_rate": track.sample_rate,
        "channels": track.channels,
        "received_fragments": received,
        "kept_fragments": len(frags),
        "duplicates_removed": plan.duplicates_removed,
        "samples": samples,
        "bytes": bytes_total,
        "media_decode_range": {
            "dts_from": dts_min, "dts_to": dts_max,
            "seconds_from": float(Fraction(dts_min, ts)),
            "seconds_to": float(Fraction(dts_max, ts)),
            "duration_seconds": float(Fraction(dts_max - dts_min, ts)),
        },
        "gaps": [g.report(ts) for g in plan.gaps],
        "edit_list": track.edit_list,
        "dropped_tail": [d.report(ts) for d in dropped if d.track_id == track.track_id],
    }


async def _run_ffprobe(path: Path) -> dict:
    proc = await asyncio.create_subprocess_exec(
        FFPROBE_BIN, "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise RecoverError(500, "FFPROBE_FAILED",
                           "ffprobe 无法读取修复结果",
                           {"stderr": err.decode("utf-8", "replace")[-2000:]})
    import json
    return json.loads(out)


@app.post("/recover")
async def recover(request: Request,
                  init: UploadFile = File(..., description="init.mp4"),
                  pieces: List[UploadFile] = File(..., description="fMP4 分片，可多个"),
                  base_url_override: Optional[str] = Form(None)):
    init_bytes = await init.read()
    try:
        info = parse_init(init_bytes)
    except RecoverError as e:
        return _err(e)

    all_frags: List[Fragment] = []
    dropped: List[fmp4.DroppedTail] = []
    received_per_track: dict = {}
    piece_names: List[str] = []

    for up in pieces:
        data = await up.read()
        name = up.filename or "piece"
        piece_names.append(name)
        try:
            frags, drops = parse_piece(data, name, info)
        except RecoverError as e:
            return _err(e)
        for f in frags:
            received_per_track[f.track_id] = received_per_track.get(f.track_id, 0) + 1
        all_frags.extend(frags)
        dropped.extend(drops)

    if not all_frags:
        return _err(RecoverError(422, "NO_USABLE_FRAGMENT",
                                 "所有分片均无完整样本，无法修复"))

    plans, overlaps = plan_recover(all_frags, info)
    if overlaps:
        ov = overlaps[0]
        t = info.tracks[ov.track_id]
        return _err(RecoverError(
            409, "FRAGMENT_OVERLAP",
            f"track {ov.track_id} 在 DTS "
            f"[{ov.dts_from},{ov.dts_to}) 存在内容不同的重叠片段（去重仅限区间与字节完全相同）",
            {
                "overlaps": [{
                    "track_id": o.track_id,
                    "dts_from": o.dts_from, "dts_to": o.dts_to,
                    "seconds_from": float(Fraction(o.dts_from, t.timescale)),
                    "seconds_to": float(Fraction(o.dts_to, t.timescale)),
                    "fragment_a": {"source": o.a.source, "seq": o.a.seq,
                                   "dts_start": o.a.dts_start, "dts_end": o.a.dts_end},
                    "fragment_b": {"source": o.b.source, "seq": o.b.seq,
                                   "dts_start": o.b.dts_start, "dts_end": o.b.dts_end},
                } for o in overlaps],
            }))

    output = rebuild(info, plans)

    # ffprobe 校验（先写临时文件，失败不留下下载文件）
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    job_id = uuid.uuid4().hex
    final_path = JOBS_DIR / f"{job_id}.mp4"
    tmp_fd, tmp_name = tempfile.mkstemp(prefix=f"{job_id}.", suffix=".mp4.tmp",
                                        dir=str(JOBS_DIR))
    try:
        with os.fdopen(tmp_fd, "wb") as f:
            f.write(output)

        try:
            probe = await _run_ffprobe(Path(tmp_name))
        except RecoverError as e:
            return _err(e)

        os.replace(tmp_name, final_path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)

    base = base_url_override or str(request.base_url).rstrip("/")
    download_url = f"{base}/download/{job_id}"

    # 统一时间轴：movie timescale（精确），同时给秒
    tracks_reports = []
    for pl in plans:
        tr = info.tracks[pl.track_id]
        tracks_reports.append(
            _track_report(tr, pl, dropped, received_per_track.get(pl.track_id, 0)))

    # 媒体时长（按各轨 DTS 覆盖范围的最大值，单位秒）
    duration_s = Fraction(0)
    for rep in tracks_reports:
        r = rep["media_decode_range"]
        duration_s = max(duration_s, Fraction(r["dts_to"], rep["timescale"]))

    # edit list 在统一 movie 时基下的映射（供核对音画同步起点）
    timeline_notes = _edit_notes(info)

    return {
        "job_id": job_id,
        "status": "ok",
        "download_url": download_url,
        "input": {"init": init.filename, "pieces": piece_names,
                  "piece_count": len(piece_names)},
        "movie_timescale": info.movie_timescale,
        "timeline": {
            "duration_seconds": float(duration_s),
            "gap_policy": "keep",
            "note": "缺口时间轴原样保留（后续样本时间戳不压缩），播放器在缺口处表现为卡顿/静音。",
            "edit_list_notes": timeline_notes,
        },
        "tracks": tracks_reports,
        "output": {
            "transcoded": False,
            "bytes": len(output),
            "ffprobe": {
                "format_name": probe.get("format", {}).get("format_name"),
                "duration_seconds": _safe_float(probe.get("format", {}).get("duration")),
                "streams": [
                    {"index": s.get("index"), "codec_type": s.get("codec_type"),
                     "codec_name": s.get("codec_name"),
                     "time_base": s.get("time_base"),
                     "duration_seconds": _safe_float(s.get("duration")),
                     "nb_read_frames": s.get("nb_read_frames"),
                     "width": s.get("width"), "height": s.get("height"),
                     "sample_rate": s.get("sample_rate"),
                     "channels": s.get("channels")}
                    for s in probe.get("streams", [])
                ],
            },
        },
    }


def _safe_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _edit_notes(info) -> list:
    notes = []
    mts = info.movie_timescale
    for tid, t in info.tracks.items():
        for idx, e in enumerate(t.edit_list):
            notes.append({
                "track_id": tid,
                "edit_index": idx,
                "empty": e["empty"],
                "segment_duration_seconds":
                    float(Fraction(e["segment_duration_movie_time"], mts)),
                "media_time": e["media_time"],
                "media_time_seconds": None if e["media_time"] < 0 else
                    float(Fraction(e["media_time"], t.timescale)),
                "media_rate": e["media_rate"],
            })
    return notes


@app.get("/download/{job_id}")
async def download(job_id: str):
    if not all(c in "0123456789abcdef" for c in job_id) or len(job_id) != 32:
        raise HTTPException(status_code=404, detail="job not found")
    path = JOBS_DIR / f"{job_id}.mp4"
    if not path.exists():
        raise HTTPException(status_code=404, detail="job not found")
    return FileResponse(path, media_type="video/mp4",
                        filename=f"recovered-{job_id}.mp4")
