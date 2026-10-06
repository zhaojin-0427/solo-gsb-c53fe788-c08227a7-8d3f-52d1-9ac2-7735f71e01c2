"""ffprobe-based verification of the rebuilt file (no transcoding)."""

from __future__ import annotations

import json
import os
import subprocess


class FfprobeError(RuntimeError):
    pass


def _ffprobe_path() -> str:
    return os.environ.get("FFPROBE_BIN", "ffprobe")


def verify_mp4(data: bytes) -> dict:
    """Write ``data`` to a temp file, run ffprobe, return a compact summary.

    Raises FfprobeError when the file cannot be parsed as a playable MP4.
    """
    import tempfile
    import os

    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tf:
        tf.write(data)
        path = tf.name
    try:
        cmd = [
            _ffprobe_path(),
            "-v", "error",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            "-show_chapters",
            path,
        ]
        proc = subprocess.run(cmd, capture_output=True, timeout=60)
        if proc.returncode != 0:
            raise FfprobeError(proc.stderr.decode("utf-8", "replace"))
        info = json.loads(proc.stdout or b"{}")
    finally:
        os.unlink(path)

    streams = []
    for s in info.get("streams", []):
        entry = {
            "index": s.get("index"),
            "codec_type": s.get("codec_type"),
            "codec_name": s.get("codec_name"),
            "timescale": None,
            "start_time": None,
            "duration": None,
            "nb_samples": None,
            "sample_rate": None,
            "channels": None,
            "width": None,
            "height": None,
            "avg_frame_rate": None,
            "has_b_frames": None,
        }
        if "start_time" in s:
            entry["start_time"] = s["start_time"]
        if "duration" in s:
            entry["duration"] = s["duration"]
        if "nb_frames" in s:
            entry["nb_frames"] = s["nb_frames"]
        entry["sample_rate"] = s.get("sample_rate")
        entry["channels"] = s.get("channels")
        entry["width"] = s.get("width")
        entry["height"] = s.get("height")
        if s.get("avg_frame_rate") and s["avg_frame_rate"] != "0/0":
            entry["avg_frame_rate"] = s["avg_frame_rate"]
        entry["has_b_frames"] = s.get("has_b_frames")
        streams.append(entry)

    return {
        "format_name": info.get("format", {}).get("format_name"),
        "duration_s": info.get("format", {}).get("duration"),
        "size_bytes": info.get("format", {}).get("size"),
        "bit_rate": info.get("format", {}).get("bit_rate"),
        "streams": streams,
    }


def read_packets(data: bytes) -> list[dict]:
    """Return per-packet PTS/DTS/CTS for sync verification."""
    import tempfile
    import os

    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tf:
        tf.write(data)
        path = tf.name
    try:
        cmd = [
            _ffprobe_path(),
            "-v", "error",
            "-print_format", "json",
            "-show_packets",
            path,
        ]
        proc = subprocess.run(cmd, capture_output=True, timeout=60)
        if proc.returncode != 0:
            raise FfprobeError(proc.stderr.decode("utf-8", "replace"))
        info = json.loads(proc.stdout or b"{}")
    finally:
        os.unlink(path)
    return info.get("packets", [])
