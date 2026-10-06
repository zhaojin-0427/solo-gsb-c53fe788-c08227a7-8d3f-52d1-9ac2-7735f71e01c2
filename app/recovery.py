"""Recovery orchestration: dedup, overlap/gap analysis and output muxing."""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from .fmp4 import (
    Fmp4Error,
    Fragment,
    InitInfo,
    Traf,
    _traf_end_dts,
    fingerprint,
    parse_init,
    parse_segment,
    rebuild_traf_fragment,
)


class OverlapError(Exception):
    """HTTP 409: same track, overlapping decode ranges that are not dupes."""

    def __init__(self, conflicts: list[dict]):
        super().__init__("overlapping fragments with different content")
        self.conflicts = conflicts


@dataclass
class DiscardedRange:
    track_id: int
    source_file: int
    from_dts: int
    to_dts: int | None      # None when the truncated tail length is unknown
    sample_count: int
    reason: str

    def to_report(self, timescale: int) -> dict:
        return {
            "track_id": self.track_id,
            "source_file": self.source_file,
            "from_dts": self.from_dts,
            "to_dts": self.to_dts,
            "from_ms": round(self.from_dts * 1000 / timescale, 3),
            "to_ms": (
                None if self.to_dts is None
                else round(self.to_dts * 1000 / timescale, 3)
            ),
            "sample_count": self.sample_count,
            "reason": self.reason,
        }


@dataclass
class RecoveryResult:
    init: InitInfo
    output: bytes
    kept: dict[int, list[Traf]]
    duplicates: list[dict]
    gaps: dict[int, list[tuple[int, int]]]
    discarded: list[DiscardedRange]
    source_count: int
    fragment_count_in: int
    fragment_count_out: int


def _ms(ticks: int, timescale: int) -> float:
    return round(ticks * 1000 / timescale, 3)


def _discard_from_truncation(traf: Traf) -> DiscardedRange | None:
    if traf.dropped_from_truncation <= 0:
        return None
    kept_end = _traf_end_dts(traf)
    # Estimate the declared tail end by extending with the average sample
    # duration of the captured samples; unknown when nothing was captured.
    if traf.samples:
        total = sum(s.duration for s in traf.samples)
        avg = total / len(traf.samples)
        declared_end = kept_end + round(avg * traf.dropped_from_truncation)
    else:
        declared_end = None
    return DiscardedRange(
        track_id=traf.track_id,
        source_file=-1,  # filled by caller
        from_dts=kept_end,
        to_dts=declared_end,
        sample_count=traf.dropped_from_truncation,
        reason="tail_fragment_truncated",
    )


def recover(init_bytes: bytes, segments: list[bytes]) -> RecoveryResult:
    init = parse_init(init_bytes)

    raw: list[tuple[Fragment, Traf]] = []
    fragment_count_in = 0
    for idx, seg in enumerate(segments):
        for fragment in parse_segment(seg, idx, init):
            fragment_count_in += 1
            for traf in fragment.trafs:
                raw.append((fragment, traf))

    # Group per track and sort by decode start (stable over upload order).
    per_track: dict[int, list[tuple[Fragment, Traf]]] = {
        tid: [] for tid in init.tracks
    }
    for fragment, traf in raw:
        per_track[traf.track_id].append((fragment, traf))
    for tid in per_track:
        per_track[tid].sort(key=lambda ft: (ft[1].base_dts,
                                            ft[0].source_file,
                                            ft[1].order))

    kept: dict[int, list[Traf]] = {tid: [] for tid in init.tracks}
    duplicates: list[dict] = []
    discarded: list[DiscardedRange] = []
    conflicts: list[dict] = []
    # truncated trafs accepted (dedup/overlap checks still apply)
    truncated: dict[int, list[tuple[Fragment, Traf]]] = {
        tid: [] for tid in init.tracks
    }

    for tid, items in per_track.items():
        seen: dict[str, int] = {}   # fingerprint -> index in kept
        for fragment, traf in items:
            fp = fingerprint(traf)
            start = traf.base_dts
            end = _traf_end_dts(traf)

            # exact duplicate (same decode interval, same sample bytes)?
            if fp in seen:
                dup = kept[tid][seen[fp]]
                duplicates.append({
                    "track_id": tid,
                    "source_file": fragment.source_file,
                    "other_source_file": getattr(dup, "_src", None),
                    "from_dts": start,
                    "to_dts": end,
                    "from_ms": _ms(start, init.tracks[tid].timescale),
                    "to_ms": _ms(end, init.tracks[tid].timescale),
                    "fingerprint": fp[:16],
                })
                continue

            overlap = None
            for other in kept[tid]:
                o_start = other.base_dts
                o_end = _traf_end_dts(other)
                if start < o_end and o_start < end:
                    overlap = (other, o_start, o_end)
                    break
            if overlap is not None:
                other, o_start, o_end = overlap
                lo = min(start, o_start)
                hi = max(end, o_end)
                conflicts.append({
                    "track_id": tid,
                    "overlap_from_dts": max(start, o_start),
                    "overlap_to_dts": min(end, o_end),
                    "overlap_from_ms": _ms(max(start, o_start),
                                           init.tracks[tid].timescale),
                    "overlap_to_ms": _ms(min(end, o_end),
                                         init.tracks[tid].timescale),
                    "fragment_a": {
                        "source_file": fragment.source_file,
                        "from_dts": start,
                        "to_dts": end,
                        "fingerprint": fp[:16],
                    },
                    "fragment_b": {
                        "source_file": getattr(other, "_src", None),
                        "from_dts": o_start,
                        "to_dts": o_end,
                        "fingerprint": fingerprint(other)[:16],
                    },
                    "decode_range_dts": [lo, hi],
                    "decode_range_ms": [
                        _ms(lo, init.tracks[tid].timescale),
                        _ms(hi, init.tracks[tid].timescale),
                    ],
                })
                continue

            if traf.dropped_from_truncation:
                truncated[tid].append((fragment, traf))

            object.__setattr__(traf, "_src", fragment.source_file)
            seen[fp] = len(kept[tid])
            kept[tid].append(traf)

    if conflicts:
        raise OverlapError(conflicts)

    # Truncation is only legal on the final fragment of a track. Since a
    # truncated tail cannot establish sample boundaries for what follows,
    # any truncated fragment strictly before later media -> 422.
    for tid, truncs in truncated.items():
        if not kept[tid] or not truncs:
            continue
        latest_start = max(t.base_dts for t in kept[tid])
        for fragment, traf in truncs:
            if traf.base_dts < latest_start:
                raise Fmp4Error(
                    f"track {tid}: truncated fragment from source "
                    f"{fragment.source_file} (dts {traf.base_dts}) is "
                    "followed by later fragments; sample boundary ambiguous",
                    code="truncated_middle",
                )
            drop = _discard_from_truncation(traf)
            if drop is not None:
                drop.source_file = fragment.source_file
                discarded.append(drop)

    # Gaps per track: positive distance between successive decode ranges.
    # Gaps are never filled or shifted; following tfdt stays at its value.
    gaps: dict[int, list[tuple[int, int]]] = {}
    for tid, trafs in kept.items():
        track_gaps: list[tuple[int, int]] = []
        prev_end = None
        for traf in trafs:
            start = traf.base_dts
            if prev_end is not None and start > prev_end:
                track_gaps.append((prev_end, start))
            end = _traf_end_dts(traf)
            prev_end = end if prev_end is None else max(prev_end, end)
        if track_gaps:
            gaps[tid] = track_gaps

    # Interleave output across tracks on the global decode timeline.
    all_trafs: list[Traf] = [t for ts in kept.values() for t in ts]
    all_trafs.sort(key=lambda t: (
        Fraction(t.base_dts, init.tracks[t.track_id].timescale),
        # audio first at identical wall time for tight interleaving
        0 if init.tracks[t.track_id].handler == "soun" else 1,
        t.track_id,
    ))

    out = bytearray()
    if init.ftyp is not None:
        # keep the original init segment's file-type box byte-for-byte
        out += init.ftyp
    else:
        # synthesize a standard fMP4 ftyp (28 bytes: hdr + major + minor
        # + three compatible brands iso5/iso6/dash)
        out += (b"\x00\x00\x00\x1cftypiso5\x00\x00\x02\x00"
                b"iso5iso6dash")
    out += init.moov
    for seq, traf in enumerate(all_trafs, start=1):
        out += rebuild_traf_fragment(traf, seq)

    return RecoveryResult(
        init=init,
        output=bytes(out),
        kept=kept,
        duplicates=duplicates,
        gaps=gaps,
        discarded=discarded,
        source_count=len(segments),
        fragment_count_in=fragment_count_in,
        fragment_count_out=len(all_trafs),
    )
