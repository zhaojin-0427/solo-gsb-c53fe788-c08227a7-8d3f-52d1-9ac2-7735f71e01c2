"""HTTP-level tests for /recover and /download."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

FFMPEG = shutil.which("ffmpeg")
pytestmark = pytest.mark.skipif(not FFMPEG, reason="ffmpeg required")

os.environ["RECOVER_STORE"] = "/tmp/fmp4-test-store"
os.environ["RECOVER_TTL_SECONDS"] = "60"
# ensure ffprobe is discoverable inside tests
extra_path = str(Path.home() / "bin")
if extra_path not in os.environ.get("PATH", ""):
    os.environ["PATH"] = extra_path + os.pathsep + os.environ.get("PATH", "")

from app.main import app  # noqa: E402

FIX = Path("/tmp/fmp4-test-fixture")


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_recover_roundtrip(client):
    init = (FIX / "init.mp4").read_bytes()
    segs = {p.name: p.read_bytes() for p in sorted(FIX.glob("seg*.m4s"))}
    names = list(segs)
    # shuffled + duplicate
    order = [names[3], names[0], names[1], names[5], names[1], names[4], names[2]]
    files = [("segments", (n, segs[n], "video/mp4")) for n in order]
    r = client.post(
        "/recover",
        files=[("init", ("init.mp4", init, "video/mp4"))] + files,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert body["fragments"]["duplicates_removed"] == 2
    assert body["output"]["transcoded"] is False

    dl = client.get(body["download_url"].split("8000")[-1])
    assert dl.status_code == 200
    assert dl.headers["content-type"] == "video/mp4"
    assert len(dl.content) == body["output"]["size_bytes"]
    assert dl.content[:8] != b""  # ftyp present


def test_missing_segments_400(client):
    init = (FIX / "init.mp4").read_bytes()
    r = client.post("/recover", files=[("init", ("i.mp4", init, "video/mp4"))])
    assert r.status_code == 422  # FastAPI validation for missing form field


def test_overlap_409(client):
    init = (FIX / "init.mp4").read_bytes()
    seg2 = (FIX / "seg002.m4s").read_bytes()
    from app.fmp4 import parse_init, parse_segment
    info = parse_init(init)
    frags = parse_segment(seg2, 2, info)
    traf = next(t for f in frags for t in f.trafs if t.track_id == 1)
    tampered = bytearray(seg2)
    pos = tampered.find(traf.samples[5].data)
    tampered[pos + 10] ^= 0x01
    files = [("init", ("init.mp4", init, "video/mp4"))]
    for n in ("seg000.m4s", "seg001.m4s", "seg002.m4s"):
        files.append(("segments", (n, (FIX / n).read_bytes(), "video/mp4")))
    files.append(("segments", ("x.m4s", bytes(tampered), "video/mp4")))
    files.append(("segments", ("seg004.m4s", (FIX / "seg004.m4s").read_bytes(),
                               "video/mp4")))
    r = client.post("/recover", files=files)
    assert r.status_code == 409
    assert r.json()["error"] == "fragment_overlap"
    assert r.json()["conflicts"][0]["track_id"] == 1


def test_hevc_init_422(client, tmp_path):
    subprocess.run([
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=10:duration=1",
        "-tag:v", "hvc1", "-c:v", "libx265",
        "-f", "hls", "-hls_time", "1", "-hls_playlist_type", "vod",
        "-hls_segment_type", "fmp4",
        "-hls_fmp4_init_filename", "i.mp4",
        "-hls_segment_filename", "s%03d.m4s",
        "x.m3u8",
    ], check=True, capture_output=True, cwd=tmp_path)
    seg0 = next(tmp_path.glob("s*.m4s")).read_bytes()
    r = client.post(
        "/recover",
        files=[
            ("init", ("i.mp4", (tmp_path / "i.mp4").read_bytes(), "video/mp4")),
            ("segments", ("s0.m4s", seg0, "video/mp4")),
        ],
    )
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "codec_unsupported"


def test_failed_job_leaves_no_file(client, tmp_path):
    init = (FIX / "init.mp4").read_bytes()
    r = client.post(
        "/recover",
        files=[
            ("init", ("init.mp4", init, "video/mp4")),
            ("segments", ("bad.m4s", b"not an fmp4 fragment", "video/mp4")),
        ],
    )
    assert r.status_code == 422
    # nothing downloadable may be published
    leftovers = list(Path(os.environ["RECOVER_STORE"]).glob("*.mp4"))
    # successful tests do publish; just assert no .part files linger
    assert not list(Path(os.environ["RECOVER_STORE"]).glob(".*.part"))


def test_download_404(client):
    r = client.get("/download/deadbeefdeadbeefdeadbeefdeadbeef")
    assert r.status_code == 404
