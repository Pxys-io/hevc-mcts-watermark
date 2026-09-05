#!/usr/bin/env python3
"""Inject a GPAC SRD udta box into an MP4's video track so hevcmerge accepts it
as a positioned tile.

Appends a 'GSRD' user-data box (GPAC GF_ISOM_UDTA_GPAC_SRD, 21-byte payload:
mergeable=1, groupID=1, offx, offy, orig_w, orig_h) into the target video
trak's udta, then fixes all chunk offsets (stco/co64) for the moov growth so
mdat samples stay aligned.

usage: inject_srd.py <in.mp4> <out.mp4> <offx> <offy> <osize_w> <osize_h>
"""
import struct, sys

def parse_boxes(d, start, end):
    out, i = [], start
    while i + 8 <= end:
        size = struct.unpack('>I', d[i:i+4])[0]
        typ = d[i+4:i+8]
        hdr = 8
        if size == 1:
            size = struct.unpack('>Q', d[i+8:i+16])[0]; hdr = 16
        elif size == 0:
            size = end - i
        if size < hdr or i + size > end:
            break
        out.append((typ, i, size))
        i += size
    return out

def build_gsrd(offx, offy, ow, oh):
    payload = bytearray()
    payload.append(1 << 7)                 # mergeable=1, 7 unused bits
    payload += struct.pack('>I', 1)        # groupID
    payload += struct.pack('>I', offx)
    payload += struct.pack('>I', offy)
    payload += struct.pack('>I', ow)
    payload += struct.pack('>I', oh)
    gsrd = struct.pack('>I', 8 + len(payload)) + b'GSRD' + bytes(payload)  # 29B
    return struct.pack('>I', 8 + len(gsrd)) + b'udta' + gsrd               # 37B

def main():
    inp, outp = sys.argv[1], sys.argv[2]
    offx, offy, ow, oh = map(int, sys.argv[3:7])
    d = bytearray(open(inp, 'rb').read())
    gsrd = build_gsrd(offx, offy, ow, oh)
    delta = len(gsrd)

    moov = next((i for t, i, s in parse_boxes(d, 0, len(d)) if t == b'moov'), None)
    if moov is None:
        raise SystemExit("no moov box")
    moov_size = struct.unpack('>I', d[moov:moov+4])[0]

    # find the video trak lacking a GSRD udta
    target = None
    for t, i, s in parse_boxes(d, moov + 8, moov + moov_size):
        if t != b'trak':
            continue
        is_video, has_gsrd = False, False
        for t2, i2, s2 in parse_boxes(d, i + 8, i + s):
            if t2 == b'udta' and bytes(d[i2:i2+s2]).count(b'GSRD'):
                has_gsrd = True
            if t2 == b'mdia':
                for t3, i3, s3 in parse_boxes(d, i2 + 8, i2 + s2):
                    if t3 == b'hdlr' and d[i3+16:i3+20] == b'vide':
                        is_video = True
        if is_video and not has_gsrd:
            target = (i, s)
            break
    if not target:
        raise SystemExit("no video trak without GSRD found")

    ti, ts = target
    # append udta at the END of the trak box
    pos = ti + ts
    d[pos:pos] = gsrd
    d[ti:ti+4] = struct.pack('>I', ts + delta)
    d[moov:moov+4] = struct.pack('>I', moov_size + delta)

    # fix chunk offsets: every stco/co64 entry that points AFTER moov start
    def fix_offsets(data, moov_start, moov_end, delta):
        n = 0
        for t, i, s in parse_boxes(data, moov_start + 8, moov_end):
            if t == b'stco':
                cnt = struct.unpack('>I', data[i+12:i+16])[0]
                for k in range(cnt):
                    off = i + 16 + 4*k
                    v = struct.unpack('>I', data[off:off+4])[0]
                    if v >= moov_start:
                        data[off:off+4] = struct.pack('>I', v + delta)
                        n += 1
            elif t == b'co64':
                cnt = struct.unpack('>I', data[i+12:i+16])[0]
                for k in range(cnt):
                    off = i + 16 + 8*k
                    v = struct.unpack('>Q', data[off:off+8])[0]
                    if v >= moov_start:
                        data[off:off+8] = struct.pack('>Q', v + delta)
                        n += 1
            elif t in (b'moov', b'trak', b'mdia', b'minf', b'stbl'):
                n += fix_offsets(data, i, i + s, delta)
        return n

    fixed = fix_offsets(d, moov, moov + moov_size + delta, delta)
    open(outp, 'wb').write(bytes(d))
    print(f"injected GSRD off=({offx},{offy}) orig={ow}x{oh} -> {outp} (+{delta}B, {fixed} chunk offsets fixed)")

if __name__ == '__main__':
    main()
