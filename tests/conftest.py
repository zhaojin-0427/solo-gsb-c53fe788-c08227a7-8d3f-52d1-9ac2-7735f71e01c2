import shutil
import subprocess
from pathlib import Path

import pytest

FFMPEG = shutil.which("ffmpeg")
FIX = Path("/tmp/fmp4-test-fixture")


@pytest.fixture(scope="session")
def fixture():
    if not FFMPEG:
        pytest.skip("ffmpeg required for fixtures")
    if FIX.exists():
        shutil.rmtree(FIX)
    FIX.mkdir(parents=True)
    subprocess.run([
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i",
        "testsrc2=size=640x360:rate=25:duration=6",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-g", "25", "-keyint_min", "25", "-sc_threshold", "0", "-bf", "2",
        "-c:a", "aac", "-ar", "44100", "-ac", "2", "-b:a", "96k",
        "-f", "hls", "-hls_time", "1", "-hls_playlist_type", "vod",
        "-hls_list_size", "0", "-hls_segment_type", "fmp4",
        "-hls_fmp4_init_filename", "init.mp4",
        "-hls_segment_filename", "seg%03d.m4s",
        "stream.m3u8",
    ], check=True, capture_output=True, cwd=FIX)
    return FIX


@pytest.fixture(scope="session")
def init_bytes(fixture):
    return (FIX / "init.mp4").read_bytes()


@pytest.fixture(scope="session")
def segments(fixture):
    return [p.read_bytes() for p in sorted(FIX.glob("seg*.m4s"))]
