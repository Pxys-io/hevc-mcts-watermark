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
        raise RuntimeError(f"fail {cmd}\n{(r.stderr or r.stdout)[-1500:]}")

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
    # 2. assemble a full-length band ES: user chunks for picked slots (each GOP
    #    is closed/independent), shared base band chunks elsewhere (byte ops only)
    gopcut = ["python3", os.path.join(HERE, "hevc_gopcut.py")]
    t0 = time.time()
    user_chunks = f"{tmp}/uchunks"
    sh(gopcut + [f"{tmp}/band_user.266", user_chunks])
    base_band = f"{d}/base_band.266"  # one-time shared encode (cached)
    if not os.path.exists(base_band):
        print("[init-lazy] encoding shared base band (one-time)", flush=True)
        tb = time.time()
        sh(["kvazaar", "-i", f"{d}/band.yuv", "--input-res", f"{W}x{band_h}",
            "--input-fps", str(fps), "-o", base_band,
            "-q", str(meta["qp"]), "--preset", meta["preset"], "-p", str(gop),
            "--no-open-gop", "--no-wpp", "--no-tmvp"])
        print(f"[init-lazy] base band {time.time()-tb:.0f}s", flush=True)
    base_chunks = f"{d}/band_gops"
    if not os.path.isdir(base_chunks) or not os.listdir(base_chunks):
        sh(gopcut + [base_band, base_chunks])
    nsegs_ = math.ceil(meta["frames"] / gop)
    uc = sorted(os.listdir(user_chunks))
    upos = 0
    with open(f"{tmp}/band_full.266", "wb") as o:
        for si in range(nsegs_):
            if si in pool:
                src = os.path.join(user_chunks, uc[upos]); upos += 1
            else:
                src = os.path.join(base_chunks, f"gop_{si:04d}.266")
            with open(src, "rb") as f:
                o.write(f.read())
    sh([a.mp4box, "-add", f"{tmp}/band_full.266", "-new", "-quiet", f"{tmp}/band_full.mp4"])
    # 3. full-length merge (no demux cutting anywhere)
    # each input track needs explicit GSRD positioning for hevcmerge
    # (hevcsplit tracks carry their own; ffmpeg-extracted ones lose it)
    t0 = time.time()
    ins = []
    for k in range(1, N):
        y = (k - 1) * H // N
        src = f"{d}/t{k}.mp4"
        dst = f"{d}/t{k}s.mp4"
        if not os.path.exists(dst):
            sh(["python3", os.path.join(HERE, "inject_srd.py"), src, dst,
                "0", str(y), str(W), str(H)])
        ins.append(dst)
    sh(["python3", os.path.join(HERE, "inject_srd.py"), f"{tmp}/band_full.mp4",
        f"{tmp}/band_full_srd.mp4", "0", str(band_y), str(W), str(H)])
    add = []
    for s in ins:
        add += ["-add", s]
    sh([a.mp4box] + add + ["-add", f"{tmp}/band_full_srd.mp4", "-new", "-quiet", f"{tmp}/merged.mp4"])
    sh([a.gpac_bin, "-i", f"{tmp}/merged.mp4", "hevcmerge", "-o", f"{tmp}/merged_single.mp4"])
    # 4. package: one ffmpeg fMP4 pass over the merged MP4 (IDR-aligned, -c copy),
    #    then keep only the picked segments (unpicked slots are served from base)
    t0 = time.time()
    all_dir = f"{tmp}/all"; os.makedirs(all_dir, exist_ok=True)
    r = subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", f"{tmp}/merged_single.mp4", "-c:v", "copy", "-an",
         "-f", "hls", "-hls_time", f"{meta['seg_dur']}", "-hls_playlist_type", "vod",
         "-hls_segment_type", "fmp4", "-hls_fmp4_init_filename", "init.mp4",
         "-hls_segment_filename", f"{all_dir}/su_%04d.m4s", f"{all_dir}/pl.m3u8"],
        capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"package failed: {r.stderr[-800:]}")
    out = a.out; os.makedirs(out, exist_ok=True)
    shutil.copy(os.path.join(all_dir, "init.mp4"), os.path.join(out, "init.mp4"))
    for si in slots:
        shutil.copy(os.path.join(all_dir, f"su_{si:04d}.m4s"), os.path.join(out, f"su_{si:04d}.m4s"))
    dt_enc = time.time() - t0
    n_segs = len([f for f in os.listdir(out) if f.endswith(".m4s")])
    print(f"[user {a.uid}] merge+package {dt_enc:.0f}s "
          f"-> {n_segs}/{len(slots)} fMP4 segs in {out}", flush=True)

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
