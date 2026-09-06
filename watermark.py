#!/usr/bin/env python3
"""watermark.py — per-user visible watermarks on HEVC MCTS tiled video.

One-time per video (init): kvazaar tiled encode (1x3 MCTS) + GPAC fMP4/HLS.
Per user: full re-encode of 10% slot segments with bottom-band text + manifest.
Tile-only band swap is documented as experimental (see README + inject_srd.py).

    python watermark.py init --src bbb.mp4 --store store/ [--seconds 0]
    python watermark.py user --store store/ --text "hi u42" --uid u42 [--frac 0.10 --out out/u42/]
    python watermark.py user ... --upload  # needs R2_* env (endpoint, keys, bucket, domain)

Requires: kvazaar, ffmpeg, gpac/MP4Box (--gpac-bin/--mp4box-bin or PATH).
Decoders: HEVC-capable players (Safari, Chrome+HW, FF 134+). H.264 shops: use slice16 repo.
"""
import argparse, hashlib, json, math, os, random, re, shutil, subprocess, sys, time

FPS, SEG_DUR = 24, 4.0
W, H, TILES, QP, PRESET = 640, 360, "1x3", 26, "fast"
GOP = int(FPS * SEG_DUR)
def tileflags():
    return (["--tiles", TILES, "--slices", "tiles",
             "--mv-constraint", "frametile"] if TILES else [])
# NOTE 2026-09-06: plain (single-tile) master is the default. Tiles only
# exist for tile-swap experiments; they cost ~2x bitrate and are NOT
# needed for span-swap (re-encode 10% spans + manifest swap).
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
HERE = os.path.dirname(os.path.abspath(__file__))

def sh(cmd, **kw):
    r = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True, text=True, **kw)
    if r.returncode != 0:
        raise RuntimeError(f"cmd failed: {cmd}\n{r.stderr[-2000:]}")

def need(prog, hint):
    if shutil.which(prog) is None and not os.path.exists(prog):
        sys.exit(f"missing {prog} ({hint})")

def cmd_init(a):
    need("kvazaar", "pacman -S kvazaar"); need("ffmpeg", "pacman -S ffmpeg")
    need(a.gpac_bin, "--gpac-bin"); need(a.mp4box_bin, "--mp4box-bin")
    os.makedirs(f"{a.store}/asset/rend0", exist_ok=True)
    src = a.src if os.path.exists(a.src) else None
    if src is None:
        sys.exit("src not found (pass a local file; URL download left to you)")
    dur = float(subprocess.run(f'ffprobe -v error -show_entries format=duration -of csv=p=0 "{src}"',
                               shell=True, capture_output=True, text=True, check=True).stdout.strip())
    n = int(dur * FPS)
    yuv = f"{a.store}/src.yuv"
    print(f"decode {dur:.0f}s to raw (one-time)...", flush=True)
    sh(f'ffmpeg -v error -y -i "{src}" -vf "scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2" -pix_fmt yuv420p -f rawvideo "{yuv}"')
    master = f"{a.store}/asset/master.266"
    t0 = time.time()
    sh(["kvazaar", "-i", yuv, "--input-res", f"{W}x{H}", "--input-fps", str(FPS),
        "-o", master] + tileflags() +
        ["-q", str(QP), "--preset", PRESET,
         "-p", str(GOP), "--no-open-gop"])
    print(f"tiled encode {time.time()-t0:.0f}s", flush=True)
    hvc = f"{a.store}/asset/master.hvc"
    shutil.copy(master, hvc)
    d = f"{a.store}/asset/rend0"
    sh([a.gpac_bin, "-i", hvc, "-o", f"{d}/hls.m3u8:dur={SEG_DUR}"])
    txt = open(f"{d}/hls.m3u8").read()
    media_src = f"{d}/hls.m3u8"
    if "STREAM-INF" in txt and os.path.exists(f"{d}/hls_1.m3u8"):
        media_src = f"{d}/hls_1.m3u8"  # tiled HEVC master/media split
    inits = [f for f in os.listdir(d) if f.endswith("init.mp4") or "dashinit" in f]
    init = sorted(inits, key=lambda f: os.path.getsize(f"{d}/{f}"), reverse=True)[0]
    os.rename(f"{d}/{init}", f"{d}/init.mp4")
    media = open(media_src).read().replace(init, "init.mp4").replace(os.path.basename(init), "init.mp4")
    segs = sorted([f for f in os.listdir(d) if f.endswith(".m4s") and "init" not in f],
                  key=lambda f: int(re.search(r"(\d+)\.m4s", f).group(1)) if re.search(r"(\d+)\.m4s", f) else 0)
    for i, f in enumerate(segs):
        media = media.replace(f, f"seg_{i:04d}.m4s")
        os.rename(f"{d}/{f}", f"{d}/seg_{i:04d}.m4s")
    open(f"{d}/media.m3u8", "w").write(media)
    for f in ("hls.m3u8", "hls_1.m3u8"):
        try: os.remove(f"{d}/{f}")
        except OSError: pass
    json.dump(dict(w=W, h=H, fps=FPS, seg_dur=SEG_DUR, duration=dur, segments=len(segs),
              tiles=TILES, qp=QP, preset=PRESET),
              open(f"{a.store}/base.json", "w"), indent=1)
    print(f"store ready: {len(segs)} segments", flush=True)

def cmd_user(a):
    meta = json.load(open(f"{a.store}/base.json"))
    d = f"{a.store}/asset/rend0"
    n_slots = math.ceil(meta["segments"] * a.frac)
    # seeded pick: different users get different times (default seed=uid).
    # string-hash dedupes renders: same seed+text reuses the same slots.
    seed = a.seed if a.seed else a.uid
    rng = random.Random(int(hashlib.sha256(seed.encode()).hexdigest(), 16))
    idxs = sorted(rng.sample(range(meta["segments"]), min(n_slots, meta["segments"])))
    tw, th, ty = W, H // 3, H - H // 3
    tmp = f"{a.store}/slot_work"; os.makedirs(tmp, exist_ok=True)
    t0 = time.time()
    for i in idxs:
        slotdir = f"{d}/slots/slot{i:04d}/{a.uid}"
        os.makedirs(slotdir, exist_ok=True)
        sh(f'cat "{d}/init.mp4" "{d}/seg_{i:04d}.m4s" > "{tmp}/slot.hvc"')
        # overlay: full-band scrim + centered opaque text in the bottom tile (rows ty..H).
        # NOTE: full-slot re-encode => top tiles are NOT bit-identical (generation
        # loss, a few % pixels), only the band carries the mark. Tile-only swap
        # (bit-identical top) is experimental — see README.
        sh(f'ffmpeg -v error -y -i "{tmp}/slot.hvc" -vf "drawbox=y={ty}:h={th}:c=black@0.6:t=fill,'
           f'drawtext=fontfile={FONT}:text=\'{a.text}\':fontsize={th//4}:fontcolor=white:'
           f'x=(w-tw)/2:y={ty}+({th}-th)/2" '
           f'-pix_fmt yuv420p -f rawvideo "{tmp}/slot_user.yuv"')
        # NOTE: slot re-encode MUST keep MCTS tiles (TILEFLAGS): per-slot init.mp4
        # swaps mid-stream, and an untiled SPS trips decoder re-init -> visible
        # corruption at slot boundaries (seen live, Sep 2026). Do not "optimize".
        sh(["kvazaar", "-i", f"{tmp}/slot_user.yuv", "--input-res", f"{W}x{H}",
            "--input-fps", str(FPS), "-o", f"{tmp}/slot_user.266"] + tileflags() +
            ["-q", str(QP), "--preset", PRESET, "-p", str(GOP), "--no-open-gop"])
        sh([a.mp4box_bin, "-add", f"{tmp}/slot_user.266", "-new", "-quiet", f"{tmp}/slot_user.mp4"])
        ht = f"{tmp}/hls_{i}"; os.makedirs(ht, exist_ok=True)
        sh([a.gpac_bin, "-i", f"{tmp}/slot_user.mp4", "-o", f"{ht}/slot.m3u8:dur=9999"])
        um4s = sorted([f for f in os.listdir(ht) if f.endswith(".m4s") and "init" not in f],
                      key=lambda f: os.path.getsize(f"{ht}/{f}"), reverse=True)
        inits = [f for f in os.listdir(ht) if "init" in f and f.endswith(".mp4")]
        shutil.move(f"{ht}/{um4s[0]}", f"{slotdir}/seg.m4s")
        shutil.copy(f"{ht}/{inits[0]}", f"{slotdir}/init.mp4")
        # tfdt alignment: the standalone-packaged slot seg carries tfdt=0 while
        # shared segs carry absolute timeline values. A 0-based slot after a
        # discontinuity misleads hls.js offset math (~0.125 s hole at slot exit,
        # seen live). Patch the slot tfdt (v0, same size) to the shared value.
        import struct as _st
        def _tfdt(path):
            d = open(path, "rb").read(); i = 0
            while i + 8 <= len(d):
                n = _st.unpack(">I", d[i:i+4])[0]; t2 = d[i+4:i+8]
                if t2 == b"moof":
                    j = i + 8
                    while j + 8 <= i + n:
                        m = _st.unpack(">I", d[j:j+4])[0]; t3 = d[j+4:j+8]
                        if t3 == b"traf":
                            k = j + 8
                            while k + 8 <= j + m:
                                q = _st.unpack(">I", d[k:k+4])[0]; t4 = d[k+4:k+8]
                                if t4 == b"tfdt":
                                    assert d[k+8] == 0, "tfdt v1 needs 8-byte patch"
                                    return k + 12, _st.unpack(">I", d[k+12:k+16])[0]
                                k += q if q > 0 else (j + m - k)
                        j += m if m > 0 else (i + n - j)
                i += n if n > 0 else len(d)
            raise RuntimeError("no tfdt in " + path)
        _off, _want = _tfdt(f"{d}/seg_{i:04d}.m4s")[0], _tfdt(f"{d}/seg_{i:04d}.m4s")[1]
        _o2, _ = _tfdt(f"{slotdir}/seg.m4s")
        _b = bytearray(open(f"{slotdir}/seg.m4s", "rb").read())
        _st.pack_into(">I", _b, _o2, _want)
        open(f"{slotdir}/seg.m4s", "wb").write(_b)
        # elst fix: gpac single-seg packaging writes edts/elst media_time=3000
        # (skip first 0.125 s) into per-slot inits; shared init has no elst.
        # hls.js honors it -> 0.125 s hole after every slot. Zero it in place.
        _ib = bytearray(open(f"{slotdir}/init.mp4", "rb").read())
        _stack = [(0, len(_ib))]
        while _stack:
            _lo, _hi = _stack.pop(); _j = _lo
            while _j + 8 <= _hi:
                _n = _st.unpack(">I", _ib[_j:_j+4])[0]; _t = bytes(_ib[_j+4:_j+8])
                if _n <= 0: _n = _hi - _j
                if _t == b"elst":
                    assert _ib[_j+8] == 0 and _st.unpack(">I", _ib[_j+12:_j+16])[0] == 1
                    _st.pack_into(">i", _ib, _j+20, 0)
                if _t in (b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts"):
                    _stack.append((_j+8, _j+_n))
                _j += _n
        open(f"{slotdir}/init.mp4", "wb").write(_ib)
    # per-user manifest: shared segs + disco/MAP slot swaps (fMP4, same codec)
    out, pending, seg_re = [], None, re.compile(r"^seg_(\d{4})\.m4s$")
    slots = set(idxs)
    for line in open(f"{d}/media.m3u8").read().splitlines():
        l = line.strip()
        if l.startswith("#EXTINF:"): pending = l; continue
        m = seg_re.match(l)
        if m and int(m.group(1)) not in slots:
            # shared segment: keep its EXTINF + uri (dropping EXTINF shortens
            # the timeline to slots only — seen live as 60 s instead of 596 s)
            if pending is not None:
                out.append(pending); pending = None
            out.append(l)
            continue
        if m and int(m.group(1)) in slots:
            sn = int(m.group(1))
            out += ["#EXT-X-DISCONTINUITY", f'#EXT-X-MAP:URI="slots/slot{sn:04d}/{a.uid}/init.mp4"',
                    pending, f"slots/slot{sn:04d}/{a.uid}/seg.m4s",
                    "#EXT-X-DISCONTINUITY", '#EXT-X-MAP:URI="init.mp4"']
            pending = None
        elif pending is not None and l.startswith("#"): out.append(l)
        else: out.append(line)
    os.makedirs(a.out, exist_ok=True)
    mu, mm = f"{d}/media.{a.uid}.m3u8", f"{a.out}/media.{a.uid}.m3u8"
    open(mu, "w").write("\n".join(out) + "\n")
    shutil.copy(mu, mm)
    mast = ("#EXTM3U\n#EXT-X-INDEPENDENT-SEGMENTS\n"
            f'#EXT-X-STREAM-INF:BANDWIDTH=660000,CODECS="hvc1.1.6.L186.80",RESOLUTION={W}x{H}\n'
            f"media.{a.uid}.m3u8\n")
    open(f"{d}/master.{a.uid}.m3u8", "w").write(mast)
    open(f"{a.out}/master.{a.uid}.m3u8", "w").write(mast)
    # player render (copy, never in place): relative BASE for local http serving
    dt = time.time() - t0
    tpl = open(os.path.join(HERE, "player.html")).read()
    wm = {"uid": a.uid, "seed": seed, "text": a.text, "slots": idxs, "seg_dur": SEG_DUR,
          "total_dur": meta["duration"], "render_seconds": round(dt, 1),
          "n_renders": len(idxs),
          "built_utc": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())}
    rendered, n1 = re.subn(r'(?:const|let) BASE = ".*?";', 'let BASE = "./";', tpl, count=1)
    rendered, n2 = re.subn(r'(?:const|let) WM_BUILD = \{.*?\};',
                           f'const WM_BUILD = {json.dumps(wm)};', rendered, count=1)
    assert n1 == 1 and n2 == 1, "player template markers not found"
    open(f"{a.out}/player.html", "w").write(rendered)
    print(f"user ready in {dt:.0f}s ({len(idxs)} slots): {a.out}", flush=True)
    if a.upload:
        for k in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
                  "R2_BUCKET", "R2_PUBLIC_DOMAIN"):
            if k not in os.environ: sys.exit(f"upload needs env {k}")
        import boto3
        s3 = boto3.client("s3", endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
                          aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
                          aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"], region_name="auto")
        print("(upload: wire your prefix walk here — see README)", flush=True)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gpac-bin", default="gpac"); ap.add_argument("--mp4box-bin", default="MP4Box")
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("init"); i.add_argument("--src", required=True); i.add_argument("--store", required=True)
    i.add_argument("--width", type=int, default=640); i.add_argument("--height", type=int, default=360)
    i.add_argument("--tiles", default="1x3"); i.add_argument("--qp", type=int, default=26)
    i.add_argument("--fps", type=int, default=24)
    u = sub.add_parser("user"); u.add_argument("--store", required=True); u.add_argument("--text", required=True)
    u.add_argument("--uid", required=True); u.add_argument("--frac", type=float, default=0.10)
    u.add_argument("--seed", default=None, help="slot picker seed (default: uid)")
    u.add_argument("--out", required=True); u.add_argument("--upload", action="store_true")
    a = ap.parse_args()
    global W, H, TILES, QP
    if a.cmd == "init":
        global FPS, GOP
        W, H, TILES, QP = a.width, a.height, a.tiles, a.qp
        FPS, GOP = a.fps, int(a.fps * SEG_DUR)
        cmd_init(a)
    else:
        global TILES, QP, PRESET
        try:
            import json as _j
            meta = _j.load(open(f"{a.store}/base.json"))
            W, H, TILES, QP, PRESET = meta["w"], meta["h"], meta.get("tiles", TILES), meta.get("qp", QP), meta.get("preset", PRESET)
        except Exception:
            pass
        cmd_user(a)

if __name__ == "__main__":
    main()
