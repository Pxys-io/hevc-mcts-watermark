#!/usr/bin/env python3
"""hevc_gopcut.py — split an Annex-B HEVC elementary stream into GOP chunks.

Relies on closed GOP: every IDR AU starts a chunk. Byte-exact, no demuxer.
Used because MP4Box -split-chunk loses/miscounts frames on tiled tracks.

    hevc_gopcut.py in.266 out_dir [first [last]]   # writes out_dir/gop_%04d.266
"""
import os
import sys


def start_code_iter(buf):
    i, n = 0, len(buf)
    while i < n - 3:
        if buf[i] == 0 and buf[i + 1] == 0 and buf[i + 2] == 1:
            sc = 3
            if i and buf[i - 1] == 0:
                sc = 4
            yield i, sc
            i += sc
        else:
            i += 1


def main():
    src, out_dir = sys.argv[1], sys.argv[2]
    first = int(sys.argv[3]) if len(sys.argv) > 3 else 0
    last = int(sys.argv[4]) if len(sys.argv) > 4 else 10**9
    os.makedirs(out_dir, exist_ok=True)
    buf = open(src, "rb").read()
    # NAL starts: (nal_unit_start_offset, nal_type, first_slice_flag)
    nals = []
    for pos, sc in start_code_iter(buf):
        start = pos - 1 if sc == 4 else pos  # include leading zero of 4-byte code
        t = (buf[pos + 3] >> 1) & 0x3F
        fs = (buf[pos + 5] >> 7) & 1 if t <= 31 else 0
        nals.append((start, t, fs))
    # AU boundary = VCL NAL with first_slice_segment_in_pic_flag set;
    # GOP boundary = such an AU that is an IDR (19/20)
    PARAM = {32, 33, 34, 39, 40, 35}  # VPS SPS PPS SEI-prefix AUD
    idrs = [i for i, (off, t, fs) in enumerate(nals)
            if t in (19, 20) and fs]
    # pull each IDR's start back over its parameter-set preamble
    starts = []
    for i in idrs:
        j = i
        while j > 0 and nals[j - 1][1] in PARAM and (j - 1 not in idrs):
            j -= 1
        starts.append(j)
    bounds = [0] + starts
    if bounds[1:] and bounds[1] == 0:
        bounds = starts  # stream begins with IDR anyway
    chunks = []
    for a, b in zip(bounds, bounds[1:] + [len(nals)]):
        chunks.append((nals[a][0], nals[b][0] if b < len(nals) else len(buf)))
    # chunk containing byte 0 may include a preamble before first IDR; merge it
    if chunks and chunks[0][0] != 0 and len(chunks) > 1:
        chunks[1] = (0, chunks[1][1])
        chunks = chunks[1:]
    written = 0
    for g, (s, e) in enumerate(chunks):
        if first <= g <= last:
            with open(os.path.join(out_dir, f"gop_{g:04d}.266"), "wb") as f:
                f.write(buf[s:e])
            written += 1
    # stream preamble (VPS/SPS/PPS before first slice) — needed to wrap
    # chunks from streams that carry parameter sets only once
    for off, t, fs in nals:
        if t <= 31:  # first VCL NAL
            with open(os.path.join(out_dir, "params.266"), "wb") as f:
                f.write(buf[:off])
            break
    print(f"{src}: {len(chunks)} GOPs -> wrote {written} chunks in {out_dir}")


if __name__ == "__main__":
    main()
