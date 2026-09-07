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

def sh(cmd, shell=False):
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
    # 1. crop band over picked segments only -> overlay -> kvazaar
    pick_expr = "+".join(f"between(n\\,{s*gop}\\,{min((s+1)*gop-1, meta['frames']-1)})" for s in slots)
    sh(f'ffmpeg -v error -y -i {d}/band.yuv -vf "select=\'{pick_expr}\','
       f'drawbox=c=black@0.6:t=fill,drawtext=fontfile={FONT}:text=\'{a.text}\':'
       f'fontsize={band_h//2}:fontcolor=white:x=(w-tw)/2:y=(h-th)/2" '
       f'-pix_fmt yuv420p -f rawvideo {tmp}/band_user.yuv')
    sh(["kvazaar", "-i", f"{tmp}/band_user.yuv", "--input-res", f"{W}x{band_h}",
        "--input-fps", str(fps), "-o", f"{tmp}/band_user.266",
        "-q", str(meta["qp"]), "--preset", meta["preset"], "-p", str(gop),
        "--no-open-gop", "--no-wpp", "--no-tmvp"])
    sh([a.mp4box, "-add", f"{tmp}/band_user.266", "-new", "-quiet", f"{tmp}/band_user.mp4"])
    # 2. merge: static tiles + user band
    add = " ".join(f"-add {d}/t{k}.mp4" for k in range(1, N))
    sh([a.mp4box] + add.split() + [f"-add", f"{tmp}/band_user.mp4", "-new", "-quiet", f"{tmp}/merged.mp4"])
    sh(["python3", os.path.join(HERE, "inject_srd.py"), f"{tmp}/merged.mp4",
        f"{tmp}/merged_srd.mp4", "0", str(band_y), str(W), str(H)])
    sh([a.gpac, "-i", f"{tmp}/merged_srd.mp4", "hevcmerge", "-o", f"{tmp}/merged_single.mp4"])
    dt_enc = time.time() - t0
    # 3. package picked segments (disco/MAP manifests over shared base segs)
    out = a.out; os.makedirs(out, exist_ok=True)
    t1 = time.time()
    for s in slots:
        t2 = time.time()
        s0, s1 = s * gop, (s + 1) * gop
        subprocess.run(f'ffmpeg -v error -y -i {tmp}/merged_single.mp4 -vf select=\'between(n\\,{s0}\\,{s1-1})\' -vsync 0 -frames:v {gop} -c:v hevc_mvc 2>/dev/null', shell=True, capture_output=True)
        # simpler: cut with -ss/-t and re-wrap (hevcmerge output is a plain mp4)
        subprocess.run(f'ffmpeg -v error -y -i {tmp}/merged_single.mp4 -ss {s0/fps:.6f} -t {meta["seg_dur"]:.6f} '
                       f'-c:v copy -an -f hls -hls_time {meta["seg_dur"]} -hls_playlist_type vod '
                       f'-hls_segment_type fmp4 -hls_fmp4_init_filename init_{a.uid}.mp4 '
                       f'-hls_segment_filename {out}/su_{s:04d}_%d.m4s {out}/cut_{s}.m3u8',
                       shell=True, capture_output=True)
    print(f"[user {a.uid}] band+merge {dt_enc:.0f}s, package {time.time()-t1:.0f}s "
          f"({nslots} slots) -> {out}", flush=True)

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
