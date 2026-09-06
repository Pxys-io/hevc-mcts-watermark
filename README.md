# hevc-mcts-watermark

Per-user **visible** watermarks on HEVC video, fMP4/HLS: per user, 10% of
the segments ("slots") are re-encoded with bottom-band text and swapped in
via the manifest (discontinuity + per-slot init). 90% of segments are shared,
re-encoded spans only — no full transcode, no tiles required.

- **Encoder:** kvazaar (tiled HEVC) + GPAC/MP4Box (fMP4/HLS packaging).
- **Packaging:** fMP4/CMAF HLS throughout (MPEG-TS transmux chokes at scale).
- **Decoder:** HEVC-capable players (Safari, Chrome+HW decode, FF 134+).
  Pure-software shops: use the H.264 `slice16-fmp4-watermark` repo instead.
- **Custom watermark:** `watermark.py` below (same UX as the slice repo).

## Quickstart

```bash
# one-time per video, PLAIN master by default (single-tile: ~2x smaller than
# tiled, plays on dumber decoders). --tiles opts into the MCTS tiled master
# (experiments only — see below).
python watermark.py init --src bbb.mp4 --store store/ \
    --gpac-bin /path/to/gpac --mp4box-bin /path/to/MP4Box

# per user (~2-3 s per slot, ~26 s for 15 slots on 10 min video)
python watermark.py user --store store/ --text "hi u42" --uid u42 \
    --frac 0.10 --out out/u42/ \
    --gpac-bin /path/to/gpac --mp4box-bin /path/to/MP4Box
# serve the store's asset/rend0/ over HTTP(S): media.u42.m3u8 is the marked
# copy (slot segs swapped), media.m3u8 pristine; player.html rendered to out/
```

Toolchain: `kvazaar` + `ffmpeg` are distro packages (`pacman -S kvazaar
ffmpeg`). `gpac`/`MP4Box` must be source-built if your distro build is broken
(the 26.07.0-2 Arch build links a missing libavdevice): clone
https://github.com/gpac/gpac.git, `./configure && make -j8`, pass `--gpac-bin`
/ `--mp4box-bin`. Pinned revision we validated: `a76535488`.

## Honest status

- **Shipped path (this wrapper): plain master + full re-encode of 10% slot
  spans.** Tiles were measured at ~2x bitrate (178 KB vs 350 KB per 10 s) for
  zero delivery benefit under span-swap, so they are OFF by default; slot
  encodes keep wavefront parallelism (~0.8 s/4 s span).
- Top video outside the band
  are NOT bit-identical (generation loss, small invisible deltas); only the
  band carries the mark. ~26 s per unique string on 10 min video, 0 s per view
  after (plain HLS).
- **Experimental: tile-only band swap** (`inject_srd.py` + `hevcmerge`):
  re-encode only the bottom tile and merge — pilot-proven (0.145 s/20 s,
  0.008% boundary rows) but per-slot single-segment packaging is finicky
  (needs closed-GOP everywhere or the dasher drops non-SAP packets). Not in
  the wrapper yet — open research item, see below.
- **Known file issue:** merged HEVC shows mixed-NAL-type warnings on some AUs
  under strict decode (ffmpeg tolerates, Chrome may not). The kvazaar-direct
  slot path in this wrapper does NOT merge, so it is unaffected.

## Measured (BBB 596 s, 640x360, 8-core)

| step | time |
|---|---|
| tiled encode (once) | ~3.5 min, 21 MB |
| HLS package (once) | ~2 s, 148 segs |
| per-user slots (15) | ~26 s |

Live demo: `https://lec-host.pxysio.top/wm2test/bbb-mcts2/player.html`

## Files

- `watermark.py` — init/user CLI (this doc)
- `inject_srd.py` — GSRD tile-position injector (tile-swap experiments)
- `player.html` — hls.js test player (jump-to-watermark, live stats)

## License

MIT for `watermark.py`, `inject_srd.py`, `player.html`.
Toolchain licenses: kvazaar BSD-3, x264/ffmpeg GPL, GPAC LGPL.
