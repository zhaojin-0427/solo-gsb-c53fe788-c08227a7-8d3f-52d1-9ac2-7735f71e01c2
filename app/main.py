"""FastAPI application: fragmented MP4 recovery service.

POST /recover      multipart upload of init.mp4 + one or more fMP4 segments
GET  /download/{id}  download the rebuilt MP4
GET  /health       liveness probe
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from .fmp4 import Fmp4Error
from .ffmpeg_probe import FfprobeError, verify_mp4
from .recovery import OverlapError, _ms, _traf_end_dts, recover

STORE_DIR = Path(os.environ.get("RECOVER_STORE", "/tmp/fmp4-recover"))
RESULT_TTL_SECONDS = int(os.environ.get("RECOVER_TTL_SECONDS", "3600"))
SWEEP_INTERVAL = 60


@asynccontextmanager
async def lifespan(app: FastAPI):
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    task = asyncio.create_task(_sweep_loop())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(
    title="Fragmented MP4 Recovery API",
    version="1.0.0",
    description=(
        "Rebuilds a playable MP4 from an unencrypted fMP4 init segment and "
        "out-of-order / duplicated / incomplete media fragments. H.264 "
        "video and AAC audio only; no transcoding is performed."
    ),
    lifespan=lifespan,
)


async def _sweep_loop() -> None:
    while True:
        try:
            now = time.time()
            for p in STORE_DIR.glob("*.mp4"):
                if now - p.stat().st_mtime > RESULT_TTL_SECONDS:
                    p.unlink(missing_ok=True)
            meta = STORE_DIR / "meta"
            if meta.exists():
                for jp in meta.glob("*.json"):
                    if now - jp.stat().st_mtime > RESULT_TTL_SECONDS:
                        jp.unlink(missing_ok=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        await asyncio.sleep(SWEEP_INTERVAL)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


def _fmp4_error_status(code: str) -> int:
    return {
        "codec_unsupported": 422,
        "encrypted": 422,
        "unknown_track": 422,
        "bad_init": 422,
        "bad_fragment": 422,
        "truncated_moof": 422,
        "truncated_middle": 422,
        "not_fragmented": 422,
        "too_large": 422,
    }.get(code, 422)


def _build_report(result, ffprobe: dict, job_id: str,
                  base_url: str) -> dict:
    init = result.init
    tracks_out = []
    per_track_ms: dict[int, float] = {}
    for tid, track in init.tracks.items():
        trafs = result.kept.get(tid, [])
        kept_count = len(trafs)
        sample_count = sum(len(t.samples) for t in trafs)
        end_dts = max((_traf_end_dts(t) for t in trafs), default=0)
        start_dts = min((t.base_dts for t in trafs), default=0)
        duration_ms = _ms(max(0, end_dts - start_dts), track.timescale)
        per_track_ms[tid] = end_dts * 1000 / track.timescale
        track_report = track.to_report()
        track_report.update({
            "fragments_kept": kept_count,
            "samples_kept": sample_count,
            "decode_start_dts": start_dts,
            "decode_end_dts": end_dts,
            "decode_range_ms": [
                _ms(start_dts, track.timescale),
                _ms(end_dts, track.timescale),
            ],
            "covered_duration_ms": duration_ms,
            "gaps": [
                {
                    "from_dts": a,
                    "to_dts": b,
                    "from_ms": _ms(a, track.timescale),
                    "to_ms": _ms(b, track.timescale),
                    "duration_ms": _ms(b - a, track.timescale),
                }
                for a, b in result.gaps.get(tid, [])
            ],
        })
        tracks_out.append(track_report)

    discarded = [
        d.to_report(init.tracks[d.track_id].timescale)
        for d in result.discarded
    ]

    return {
        "job_id": job_id,
        "status": "ok",
        "download_url": f"{base_url}/download/{job_id}",
        "output": {
            "filename": f"{job_id}.mp4",
            "size_bytes": len(result.output),
            "transcoded": False,
            "ffprobe": ffprobe,
        },
        "timeline": {
            "movie_timescale": init.movie_timescale,
            "max_track_end_ms": round(max(per_track_ms.values()), 3)
            if per_track_ms else 0,
            "note": (
                "Original tfdt decode timestamps are preserved; gaps remain "
                "as silent/black intervals and do not shift later media."
            ),
        },
        "tracks": tracks_out,
        "fragments": {
            "source_files": result.source_count,
            "input_fragments": result.fragment_count_in,
            "output_fragments": result.fragment_count_out,
            "duplicates_removed": len(result.duplicates),
        },
        "duplicates": result.duplicates,
        "discarded_ranges": discarded,
    }


def _run_recovery(init_bytes: bytes, segments: list[bytes]):
    return recover(init_bytes, segments)


@app.post("/recover")
async def recover_endpoint(
    request: Request,
    init: UploadFile = File(..., description="unencrypted fMP4 init segment"),
    segments: list[UploadFile] = File(
        ..., description="one or more (possibly out-of-order) media segments"
    ),
):
    init_bytes = await init.read()
    if not init_bytes:
        raise HTTPException(status_code=400, detail={
            "error": "empty_init",
            "message": "init file is empty",
        })
    if b"moov" not in init_bytes[:2000000]:
        raise HTTPException(status_code=400, detail={
            "error": "not_an_init_segment",
            "message": "init file does not look like an fMP4 init segment "
                       "(no moov box near the beginning)",
        })
    if len(segments) == 0:
        raise HTTPException(status_code=400, detail={
            "error": "no_segments",
            "message": "at least one media segment is required",
        })

    seg_bytes: list[bytes] = []
    for sf in segments:
        buf = await sf.read()
        if not buf:
            continue
        seg_bytes.append(buf)
    if not seg_bytes:
        raise HTTPException(status_code=400, detail={
            "error": "no_segments",
            "message": "all uploaded segments were empty",
        })

    try:
        result = await run_in_threadpool(_run_recovery, init_bytes, seg_bytes)
    except OverlapError as exc:
        return JSONResponse(status_code=409, content={
            "error": "fragment_overlap",
            "message": (
                "fragments on the same track overlap but are not byte-"
                "identical; cannot choose between them automatically"
            ),
            "conflicts": exc.conflicts,
        })
    except Fmp4Error as exc:
        raise HTTPException(
            status_code=_fmp4_error_status(exc.code),
            detail={"error": exc.code, "message": str(exc)},
        )

    # Verify with ffprobe BEFORE publishing the file. Write to a temp path in
    # the same directory and atomically rename; on any failure nothing is
    # left behind as a downloadable result.
    job_id = uuid.uuid4().hex
    final_path = STORE_DIR / f"{job_id}.mp4"
    tmp_path = STORE_DIR / f".{job_id}.part"
    try:
        with open(tmp_path, "wb") as f:
            f.write(result.output)
        try:
            ffprobe = await run_in_threadpool(verify_mp4, result.output)
        except FfprobeError as exc:
            raise HTTPException(status_code=422, detail={
                "error": "output_unverifiable",
                "message": "rebuilt MP4 failed ffprobe validation",
                "ffprobe": str(exc),
            })
        os.replace(tmp_path, final_path)
    except HTTPException:
        tmp_path.unlink(missing_ok=True)
        final_path.unlink(missing_ok=True)
        raise
    except Exception as exc:  # pragma: no cover
        tmp_path.unlink(missing_ok=True)
        final_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail={
            "error": "internal", "message": str(exc),
        })

    base_url = str(request.base_url).rstrip("/")
    report = _build_report(result, ffprobe, job_id, base_url)
    return report


@app.get("/download/{job_id}")
async def download(job_id: str):
    if not (job_id and all(c in "0123456789abcdef" for c in job_id)):
        raise HTTPException(status_code=404, detail="not found")
    path = STORE_DIR / f"{job_id}.mp4"
    if not path.exists():
        raise HTTPException(status_code=404, detail={
            "error": "not_found",
            "message": "job id unknown or result has expired",
        })
    return FileResponse(
        path,
        media_type="video/mp4",
        filename="recovered.mp4",
    )
