#!/usr/bin/env python3
"""watermark_tiles.py — HEVC tile-swap watermarks (technique C).

One-time per video (init): kvazaar 1xN CTU-aligned tiled master (GOP=seg),
hevcsplit into N tile tracks, decode band once to band.yuv.
Per user (frac of segments): crop+overlay band -> kvazaar band encode ->
MP4Box merge -> GSRD inject -> hevcmerge -> package picked segs (disco/MAP).

    python3 watermark_tiles.py init  --src bbb.mp4 --store S [--width 640 --height 320
        --tiles 5 --band-frac 0.8 --qp 26 --fps 24]
    python3 watermark_tiles.py user  --store S --uid u42 --text "hi u42" [--frac 0.10 --out OUT]

CTU rule: --height must be a multiple of 64, --tiles divides height/64.
Requires: kvazaar, ffmpeg, gpac+MP4Box (vendored), inject_srd.py next to this file.
"""
import argparse, base64, hashlib, json, math, os, random, shutil, subprocess, sys, time

FPS, SEG_DUR, QP, PRESET = 24, 4.0, 26, "fast"
HERE = os.path.dirname(os.path.abspath(__file__))

def sh(cmd, shell=None):
    if shell is None:
        shell = isinstance(cmd, str)
    r = subprocess.run(cmd, shell=shell, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"fail {cmd}\n{r.stderr[-1500:]}")

def cmd_init(a):
    W, H, N = a.width, a.height, a.tiles
    assert H % 64 == 0 and (H // 64) % N == 0, f"height {H} must be 64-multiple and tiles divide H/64"
    dur = float(subprocess.run(f'ffprobe -v error -show_entries format=duration -of csv=p=0 "{a.src}"',
                               shell=True, capture_output=True, text=True, check=True).stdout.strip())
    n = int(dur * FPS)
    gop = int(FPS * SEG_DUR)
    d = f"{a.store}/asset"; os.makedirs(d, exist_ok=True)
    master = f"{d}/master5.266"
    t0 = time.time()
    print(f"[init] streaming {dur:.0f}s -> kvazaar {W}x{H} tiles 1x{N} gop={gop}", flush=True)
    r = subprocess.run(
        f'ffmpeg -v error -i "{a.src}" -vf scale={W}:{H} -pix_fmt yuv420p -f rawvideo - | '
        f'kvazaar -i - --input-res {W}x{H} --input-fps {FPS} -o {master} '
        f'--tiles 1x{N} --slices tiles --mv-constraint frametile '
        f'-q {QP} --preset {PRESET} -p {gop} --no-open-gop -n {n}',
        shell=True)
    if r.returncode != 0: sys.exit("encode failed")
    print(f"[init] encode {time.time()-t0:.0f}s", flush=True)
    G, M = a.gpac_bin, a.mp4box
    sh([G, "-i", master, "hevcsplit", "-o", f"{d}/tiled.mp4"])
    for k in range(1, N + 1):
        sh([M, "-add", f"{d}/tiled.mp4#{k}", "-new", "-quiet", f"{d}/t{k}.mp4"])
    # decode band once (static tiles never change; band pixels reused per user)
    band_h = H - (H // N) * (N - 1)
    band_y = H - band_h
    t0 = time.time()
    sh(f'ffmpeg -v error -y -i {master} -vf crop={W}:{band_h}:0:{band_y} '
       f'-pix_fmt yuv420p -f rawvideo {d}/band.yuv')
    print(f"[init] band decoded {time.time()-t0:.0f}s ({band_w}x{band_h})".replace("band_w", str(W)), flush=True)
    json.dump(dict(w=W, h=H, fps=FPS, seg_dur=SEG_DUR, duration=dur, frames=n,
                   tiles=N, band_y=band_y, band_h=band_h, qp=QP, preset=PRESET),
              open(f"{a.store}/base.json", "w"), indent=1)
    print("[init] ready", flush=True)

def cmd_user(a):
    meta = json.load(open(f"{a.store}/base.json"))
    W, H, N = meta["w"], meta["h"], meta["tiles"]
    fps, gop = meta["fps"], int(meta["fps"] * meta["seg_dur"])
    band_y, band_h = meta["band_y"], meta["band_h"]
    d = f"{a.store}/asset"
    nsegs = math.ceil(meta["frames"] / gop)
    seed = a.seed or a.uid
    rng = random.Random(int(hashlib.sha256(seed.encode()).hexdigest(), 16))
    nslots = max(1, round(nsegs * a.frac))
    slots = sorted(rng.sample(range(nsegs), nslots))
    pool = set(slots)
    slot_segs = sorted({s * gop // gop for s in slots})  # seg idx = slot idx
    FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    tmp = f"{a.store}/work_{a.uid}"; os.makedirs(tmp, exist_ok=True)
    t0 = time.time()
    # 1. extract picked slots from band.yuv by byte-offset (rawvideo = seek math),
    #    then overlay text in a filter pass (no select filter involved)
    FRB = W * band_h * 3 // 2
    t0 = time.time()
    with open(f"{d}/band.yuv", "rb") as f, open(f"{tmp}/picked.yuv", "wb") as o:
        for s in slots:
            f.seek(s * gop * FRB)
            o.write(f.read(gop * FRB))
    vf = (f"drawbox=c=black@0.6:t=fill,"
          f"drawtext=fontfile={FONT}:text='{a.text}':fontsize={band_h//2}:"
          f"fontcolor=white:x=(w-tw)/2:y=(h-th)/2")
    sh(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "yuv420p",
        "-s", f"{W}x{band_h}", "-r", str(fps), "-i", f"{tmp}/picked.yuv",
        "-vf", vf, "-pix_fmt", "yuv420p", "-f", "rawvideo", f"{tmp}/band_user.yuv"])
    sh(["kvazaar", "-i", f"{tmp}/band_user.yuv", "--input-res", f"{W}x{band_h}",
        "--input-fps", str(fps), "-o", f"{tmp}/band_user.266",
        "-q", str(meta["qp"]), "--preset", meta["preset"], "-p", str(gop),
        "--no-open-gop", "--no-wpp", "--no-tmvp"])
    sh([a.mp4box, "-add", f"{tmp}/band_user.266", "-new", "-quiet", f"{tmp}/band_user.mp4"])
    # 2. per-slot merge: static tile cuts (MP4Box -split-chunk, cached), band slot,
    #    inject SRD, hevcmerge -> 4 s single-tile HEVC per picked slot
    cuts = f"{a.store}/cuts"; os.makedirs(cuts, exist_ok=True)
    n_picked = len(slots)
    t0 = time.time()
    slot_mergeds = []
    for i, s in enumerate(slots):
        print(f"[slot {i} seg {s}] t={t0s if False else s*4}s: cutting", flush=True)
        t0s, t1s = s * meta["seg_dur"], (s + 1) * meta["seg_dur"]
        chunk = f"{t0s:g}:{t1s:g}"
        s4 = []
        for k in range(1, N):
            c = f"{cuts}/t{k}_s{s:04d}.mp4"
            if not os.path.exists(c):
                sh([a.mp4box, "-split-chunk", chunk, f"{d}/t{k}.mp4", "-out", c])
            s4.append(c)
        bslot = f"{tmp}/bslot_{i:02d}.mp4"
        sh([a.mp4box, "-split-chunk", f"{i * meta['seg_dur']:g}:{(i + 1) * meta['seg_dur']:g}",
            f"{tmp}/band_user.mp4", "-out", bslot])
        add = []
        for c in s4:
            add += ["-add", c]
        sh([a.mp4box] + add + ["-add", bslot, "-new", "-quiet", f"{tmp}/merged.mp4"])
        print(f"[slot {i}] merged.mp4 = {os.path.getsize(f'{tmp}/merged.mp4')}", flush=True)
        sh(["python3", os.path.join(HERE, "inject_srd.py"), f"{tmp}/merged.mp4",
            f"{tmp}/merged_srd.mp4", "0", str(band_y), str(W), str(H)])
        sh([a.gpac_bin, "-i", f"{tmp}/merged_srd.mp4", "hevcmerge", "-o", f"{tmp}/mone_{i:02d}.mp4"])
        print(f"[slot {i}] mone = {os.path.getsize(f'{tmp}/mone_{i:02d}.mp4')}", flush=True)
        slot_mergeds.append(f"{tmp}/mone_{i:02d}.mp4")
    dt_enc = time.time() - t0
    # 3. concat merged slots -> one fMP4 HLS pass (GOP-aligned, -c copy)
    out = a.out; os.makedirs(out, exist_ok=True)
    t1 = time.time()
    cat = []
    for m in slot_mergeds:
        cat += ["-cat", m]
    sh([a.mp4box] + cat + ["-new", "-quiet", f"{tmp}/merged_user.mp4"])
    r = subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", f"{tmp}/merged_user.mp4", "-c:v", "copy", "-an",
         "-f", "hls", "-hls_time", f"{meta['seg_dur']}", "-hls_playlist_type", "vod",
         "-hls_segment_type", "fmp4", "-hls_fmp4_init_filename", "init.mp4",
         "-hls_segment_filename", f"{out}/su_%04d.m4s", f"{out}/pl.m3u8"],
        cwd=tmp, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"package failed: {r.stderr[-800:]}")
    for f_ in os.listdir(tmp):
        if f_.endswith(".m4s") or f_ == "init.mp4":
            shutil.move(os.path.join(tmp, f_), os.path.join(out, f_))
    n_segs = len([f for f in os.listdir(out) if f.endswith(".m4s")])
    print(f"[user {a.uid}] band+merge {dt_enc:.0f}s, package {time.time()-t1:.0f}s "
          f"-> {n_segs}/{n_picked} fMP4 segs in {out}", flush=True)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gpac-bin", default=os.environ.get("GPAC_BIN", "gpac"))
    ap.add_argument("--mp4box", default=os.environ.get("MP4BOX_BIN", "MP4Box"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("init")
    i.add_argument("--gpac-bin", default=os.environ.get("GPAC_BIN","gpac"))
    i.add_argument("--mp4box", default=os.environ.get("MP4BOX_BIN","MP4Box")); i.add_argument("--src", required=True); i.add_argument("--store", required=True)
    i.add_argument("--width", type=int, default=640); i.add_argument("--height", type=int, default=320)
    i.add_argument("--tiles", type=int, default=5)
    u = sub.add_parser("user")
    u.add_argument("--gpac-bin", default=os.environ.get("GPAC_BIN","gpac"))
    u.add_argument("--mp4box", default=os.environ.get("MP4BOX_BIN","MP4Box")); u.add_argument("--store", required=True); u.add_argument("--uid", required=True)
    u.add_argument("--text", required=True); u.add_argument("--frac", type=float, default=0.10)
    u.add_argument("--seed", default=None); u.add_argument("--out", required=True)
    a = ap.parse_args()
    (cmd_init if a.cmd == "init" else cmd_user)(a)

if __name__ == "__main__":
    main()
