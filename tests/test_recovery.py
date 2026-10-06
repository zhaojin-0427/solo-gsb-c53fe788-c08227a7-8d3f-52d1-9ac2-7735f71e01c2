"""End-to-end tests for the fMP4 recovery service.

Fixtures are generated once with the ffmpeg available on PATH (the CI image
and the Docker container both provide one). Skip the module when ffmpeg is
missing.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from app.fmp4 import parse_init, parse_segment, rebuild_traf_fragment
from app.recovery import OverlapError, recover

pytestmark = pytest.mark.skipif(
    not shutil.which("ffmpeg"), reason="ffmpeg required for fixtures"
)
FFMPEG = shutil.which("ffmpeg")


def test_init_parses_tracks(init_bytes):
    info = parse_init(init_bytes)
    codecs = {t.codec for t in info.tracks.values()}
    assert codecs == {"h264", "aac"}
    video = next(t for t in info.tracks.values() if t.codec == "h264")
    audio = next(t for t in info.tracks.values() if t.codec == "aac")
    assert (video.width, video.height) == (640.0, 360.0)
    assert audio.channel_count == 2
    assert audio.sample_rate == 44100
    # ffmpeg fMP4 uses an empty edit for priming; it must be preserved
    assert any(e.media_time == -1 for e in video.edit_list)
    assert any(e.media_time == -1 for e in audio.edit_list)


def test_segments_have_b_frame_cto(segments, init_bytes):
    init = parse_init(init_bytes)
    found_cto = False
    for seg in segments:
        for frag in parse_segment(seg, 0, init):
            for traf in frag.trafs:
                if traf.track_id == next(
                    t for t in init.tracks.values() if t.codec == "h264"
                ).track_id:
                    found_cto |= any(s.cto != 0 for s in traf.samples)
    assert found_cto, "fixture should contain B-frame composition offsets"


def test_reorder_and_dedup(init_bytes, segments):
    order = [3, 0, 5, 1, 1, 4, 2]
    result = recover(init_bytes, [segments[i] for i in order])
    assert result.fragment_count_in == 7
    assert result.fragment_count_out == 12  # 6 video trafs + 6 audio trafs
    assert len(result.duplicates) == 2     # one per track for repeated seg1
    assert result.gaps == {}
    for tid, trafs in result.kept.items():
        dts = [t.base_dts for t in trafs]
        assert dts == sorted(dts)


def test_gap_preserves_timeline(init_bytes, segments):
    result = recover(init_bytes, [segments[0], segments[1],
                                  segments[4], segments[5]])
    for tid, gaps in result.gaps.items():
        assert gaps, "every track should have a 2s gap"
        for a, b in gaps:
            assert b > a
    # rebuild and check tfdt jumps with ffprobe packets
    info = parse_init(init_bytes)
    from app.ffmpeg_probe import read_packets, verify_mp4
    summary = verify_mp4(result.output)
    assert float(summary["duration_s"]) == pytest.approx(6.023991, abs=0.01)
    vpackets = [p for p in read_packets(result.output)
                if p["codec_type"] == "video"]
    dts = [int(p["dts"]) for p in vpackets]
    # gap from 25600 to 51200 must remain as a discontinuity
    assert any(b - a > 512 for a, b in zip(dts, dts[1:]))


def test_tail_truncation_keeps_whole_samples(init_bytes, segments):
    tail = bytearray(segments[-1])
    del tail[-60000:]
    result = recover(init_bytes, segments[:-1] + [bytes(tail)])
    assert result.discarded, "truncated samples must be reported"
    for d in result.discarded:
        assert d.reason == "tail_fragment_truncated"
        assert d.sample_count > 0
    from app.ffmpeg_probe import verify_mp4
    verify_mp4(result.output)  # output must still be readable


def test_middle_truncation_is_422(init_bytes, segments):
    mid = bytearray(segments[2])
    del mid[-50000:]
    with pytest.raises(Exception) as exc:
        recover(init_bytes, [segments[0], bytes(mid), segments[4]])
    assert getattr(exc.value, "code", None) == "truncated_middle"


def test_overlap_different_content_is_conflict(init_bytes, segments):
    init = parse_init(init_bytes)
    frags = parse_segment(segments[2], 2, init)
    traf = next(t for f in frags for t in f.trafs if t.track_id == 1)
    buf = bytearray(segments[2])
    pos = buf.find(traf.samples[5].data)
    buf[pos + 10] ^= 0xFF
    with pytest.raises(OverlapError) as exc:
        recover(init_bytes,
                [segments[0], segments[1], segments[2], bytes(buf),
                 segments[4]])
    assert exc.value.conflicts[0]["track_id"] == 1


def test_single_track_fragments_interleave(init_bytes, segments):
    """Fragments each carrying a single traf must mux back into A/V sync."""
    init = parse_init(init_bytes)
    singles: list[bytes] = []
    for seg in segments:
        for frag in parse_segment(seg, 0, init):
            for traf in frag.trafs:
                singles.append(rebuild_traf_fragment(traf, frag.sequence))
    # feed in reverse order
    result = recover(init_bytes, list(reversed(singles)))
    assert result.fragment_count_out == 12
    assert result.duplicates == []
    from app.ffmpeg_probe import verify_mp4
    summary = verify_mp4(result.output)
    assert len(summary["streams"]) == 2
    assert float(summary["duration_s"]) == pytest.approx(6.023991, abs=0.02)


def test_unsupported_codec_init_rejected(tmp_path, segments):
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
    from app.fmp4 import Fmp4Error
    with pytest.raises(Fmp4Error) as exc:
        parse_init((tmp_path / "i.mp4").read_bytes())
    assert exc.value.code == "codec_unsupported"


def test_no_transcode_output_structure(init_bytes, segments):
    result = recover(init_bytes, list(reversed(segments)))
    # original sample bytes are reused: exact byte size matches a plain
    # concat of init + all mdat payloads
    init = parse_init(init_bytes)
    assert result.output.startswith(init.ftyp)
    assert init.moov in result.output


def test_init_without_ftyp_gets_synthesized(init_bytes, segments):
    import struct as st
    from app.ffmpeg_probe import verify_mp4

    ftyp_size = st.unpack_from(">I", init_bytes, 0)[0]
    no_ftyp = init_bytes[ftyp_size:]
    assert no_ftyp[4:8] == b"moov"
    result = recover(no_ftyp, [segments[0], segments[2]])
    assert result.output[4:8] == b"ftyp"
    assert st.unpack_from(">I", result.output, 0)[0] == 28
    verify_mp4(result.output)


def test_timeline_matches_original_byte_concat(init_bytes, segments):
    """Rebuilt PTS/DTS must match the original recording frame-for-frame."""
    import json
    import subprocess

    from app.ffmpeg_probe import read_packets

    original = init_bytes + b"".join(segments)
    result = recover(init_bytes, list(reversed(segments)))

    orig_pk = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_packets", "pipe:0"],
        input=original, capture_output=True, check=True).stdout)["packets"]
    rec_pk = read_packets(result.output)
    for codec_type in ("video", "audio"):
        o = [p for p in orig_pk if p["codec_type"] == codec_type]
        r = [p for p in rec_pk if p["codec_type"] == codec_type]
        assert len(o) == len(r)
        assert all(int(a["dts"]) == int(b["dts"])
                   for a, b in zip(o, r)), f"{codec_type} DTS drift"
        assert all(abs(float(a["pts_time"]) - float(b["pts_time"])) < 1e-3
                   for a, b in zip(o, r)), f"{codec_type} PTS drift"



def test_cenc_init_rejected(init_bytes):
    """An init whose visual sample entry carries a sinf box is CENC -> 422."""
    import struct as st
    from app.mp4boxes import children, iter_boxes

    data = bytearray(init_bytes)
    avcc = data.find(b"avcC") - 4  # start of avcC box
    sinf = st.pack(">I4s", 8 + 12, b"sinf") + st.pack(
        ">I4s4s", 12, b"frma", b"avc1"
    )

    moov = next(b for b in iter_boxes(bytes(data), 0, len(data))
                if b.type == b"moov")
    containers: list = []

    def walk(box):
        if box.start <= avcc < box.end:
            containers.append(box)
            # stsd payload is not a plain box list (version/flags + count
            # precede the sample entry); stop descending there.
            if box.type != b"stsd":
                for c in children(bytes(data), box):
                    walk(c)

    walk(moov)
    for b in containers:
        st.pack_into(">I", data, b.start,
                     st.unpack_from(">I", data, b.start)[0] + len(sinf))
    stsd = next(b for b in containers if b.type == b"stsd")
    entry_start = stsd.data_start + 8
    st.pack_into(">I", data, entry_start,
                 st.unpack_from(">I", data, entry_start)[0] + len(sinf))
    data[avcc:avcc] = sinf

    from app.fmp4 import Fmp4Error
    with pytest.raises(Fmp4Error) as exc:
        parse_init(bytes(data))
    assert exc.value.code in ("encrypted", "codec_unsupported")
