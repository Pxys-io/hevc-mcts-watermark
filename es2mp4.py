#!/usr/bin/env python3
"""es2mp4.py — wrap Annex-B HEVC AUs as MP4 samples without parsing slices.

Bypasses MP4Box -add, whose HEVC import segfaults on some encoder outputs.
Timing (stts/ctts) and the hvcC sample entry are stolen from a reference MP4
with identical GOP structure (e.g. MP4Box's own full-stream import); AU
payload bytes are muxed verbatim, Annex-B -> length-prefixed.

    es2mp4.py CHUNK.266 REF.mp4 AU_OFFSET OUT.mp4

AU_OFFSET = index of the chunk's first AU within the reference stream.
Verifies: sample count match + decode-equality is left to the caller.
"""
import struct
import sys

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
from hevc_gopcut import nal_units, split_aus


def kids(d, s, e):
    i, out = s, []
    while i + 8 <= e:
        sz = struct.unpack(">I", d[i:i + 4])[0]
        typ = d[i + 4:i + 8]
        hdr = 8
        if sz == 1:
            sz = struct.unpack(">Q", d[i + 8:i + 16])[0]
            hdr = 16
        if sz < hdr or i + sz > e:
            break
        out.append((typ, i, i + sz))
        i += sz
    return out


def find(d, path, s=0, e=None):
    e = len(d) if e is None else e
    if not path:
        return [(s, e)]
    r = []
    for typ, ps, pe in kids(d, s, e):
        if typ == path[0]:
            r += find(d, path[1:], ps + 8, pe)
    return r


def box(typ, payload):
    return struct.pack(">I", 8 + len(payload)) + typ + payload


def au_to_sample(au):
    """Annex-B AU -> length-prefixed sample (4-byte BE NAL lengths)."""
    units = [(off, sc) for off, sc, _ in nal_units(au)] + [(len(au), 0)]
    out = bytearray()
    for (a, sc), (b, _) in zip(units, units[1:]):
        payload = au[a + sc:b]
        out += struct.pack(">I", len(payload)) + payload
    return bytes(out)


def table_entries(d, s):
    """Parse stts/ctts body at payload start s -> (version, counts+deltas list)."""
    v = d[s]
    n = struct.unpack(">I", d[s + 4:s + 8])[0]
    arr = []
    for k in range(n):
        c = struct.unpack(">I", d[s + 8 + 8 * k:s + 12 + 8 * k])[0]
        dd = struct.unpack(">i", d[s + 12 + 8 * k:s + 16 + 8 * k])[0]
        arr.append((c, dd))
    return v, arr


def expand(arr):
    return [dd for c, dd in arr for _ in range(c)]


def pack_table(ver, per):
    entries, k = [], 0
    while k < len(per):
        j = k
        while j < len(per) and per[j] == per[k]:
            j += 1
        entries.append((j - k, per[k]))
        k = j
    out = bytes([ver, 0, 0, 0]) + struct.pack(">I", len(entries))
    for c, dd in entries:
        out += struct.pack(">I", c) + struct.pack(">i", dd)
    return out


def main():
    chunk_fn, ref_fn, off, out_fn = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
    ref = open(ref_fn, "rb").read()
    (trak_s, trak_e) = find(ref, [b"moov", b"trak"])[0]
    (stbl_s, stbl_e) = find(ref, [b"mdia", b"minf", b"stbl"], trak_s, trak_e)[0]
    (mdhd_s, _) = find(ref, [b"mdia", b"mdhd"], trak_s, trak_e)[0]
    mdhd_v = ref[mdhd_s]
    ts_off = 12 + (8 if mdhd_v else 0)
    timescale = struct.unpack(">I", ref[mdhd_s + ts_off:mdhd_s + ts_off + 4])[0]
    # full hvc1 sample entry (dims + hvcC) reused verbatim (skip stsd header)
    (stsd_s, stsd_e) = find(ref, [b"stsd"], stbl_s, stbl_e)[0]
    (hvc1_s, hvc1_e) = find(ref, [b"hvc1"], stsd_s + 8, stsd_e)[0]
    hvc1_box = ref[hvc1_s - 8:hvc1_e]
    # timing tables expanded + sliced
    (stts_s, _) = find(ref, [b"stts"], stbl_s, stbl_e)[0]
    stts_v, stts_arr = table_entries(ref, stts_s)
    ctts_hit = find(ref, [b"ctts"], stbl_s, stbl_e)
    ctts_v, ctts_arr = (table_entries(ref, ctts_hit[0][0]) if ctts_hit else (1, []))
    # tkhd/mvhd/mdhd templates (patched durations below)
    (tkhd_s, tkhd_e) = find(ref, [b"tkhd"], trak_s, trak_e)[0]
    tkhd = bytearray(ref[tkhd_s - 8:tkhd_e])
    (mvhd_s, mvhd_e) = find(ref, [b"moov", b"mvhd"])[0]
    mvhd = bytearray(ref[mvhd_s - 8:mvhd_e])
    (mdhd_full_s, mdhd_full_e) = find(ref, [b"mdia", b"mdhd"], trak_s, trak_e)[0]
    mdhd = bytearray(ref[mdhd_full_s - 8:mdhd_full_e])

    aus = split_aus(open(chunk_fn, "rb").read())
    n = len(aus)
    dts_all, cts_all = expand(stts_arr), expand(ctts_arr) if ctts_hit else [0] * len(expand(stts_arr))
    assert off + n <= len(dts_all), f"chunk [{off}:{off+n}] exceeds ref samples {len(dts_all)}"
    dts, cts = dts_all[off:off + n], (cts_all[off:off + n] if ctts_hit else None)
    dur = sum(dts)

    samples = [au_to_sample(a) for a in aus]
    stsz = struct.pack(">I", 0) + struct.pack(">I", 0) + struct.pack(">I", n)
    stsz += b"".join(struct.pack(">I", len(s)) for s in samples)
    stsc = struct.pack(">IIIII", 0, 1, 1, n, 1)
    stts = pack_table(stts_v, dts)
    stbl_children = (box(b"stsd", struct.pack(">II", 0, 1) + hvc1_box)
                     + box(b"stts", stts)
                     + (box(b"ctts", pack_table(ctts_v, cts)) if ctts_hit else b"")
                     + box(b"stsc", stsc) + box(b"stsz", stsz)
                     + box(b"stco", struct.pack(">II", 0, 1) + struct.pack(">I", 0)))  # patched later
    stbl = box(b"stbl", stbl_children)
    minf = box(b"minf", box(b"vmhd", bytes([0, 0, 0, 1]) + struct.pack(">HHHH", 0, 0, 0, 0))
               + box(b"dinf", box(b"dref", struct.pack(">II", 0, 1) + box(b"url ", bytes([0, 0, 0, 1]))))
               + stbl)
    # patch durations (offsets relative to box start, header included)
    # v0: mvhd/mdhd = v/f(4)+creation(4)+mod(4)+timescale(4)+duration(4) -> 24
    #     tkhd       = v/f(4)+creation(4)+mod(4)+id(4)+reserved(4)+dur(4) -> 28
    def patch_dur(b, kind):
        v = b[8]
        if kind == "tkhd":
            off = 28 if v == 0 else 40
        else:  # mvhd, mdhd
            off = 24 if v == 0 else 32
        if v == 0:
            struct.pack_into(">I", b, off, dur)
        else:
            struct.pack_into(">Q", b, off, dur)
    patch_dur(mvhd, "mvhd")
    patch_dur(tkhd, "tkhd")
    patch_dur(mdhd, "mdhd")
    mdia = box(b"mdia", bytes(mdhd) + box(b"hdlr", struct.pack(">IHH", 0, 0, 0) + b"vide\x00\x00\x00\x00\x00" + b"tile-band\x00") + minf)
    trak = box(b"trak", bytes(tkhd) + mdia)
    moov_body = bytes(mvhd) + trak
    ftyp = box(b"ftyp", b"isom\x00\x00\x00\x00iso4hvc1")
    stco_off = len(ftyp) + 8 + len(moov_body) + 8
    # rebuild stbl with real stco (sizes unchanged -> offsets stable)
    stbl_children = stbl_children[:-4] + struct.pack(">I", stco_off)
    stbl = box(b"stbl", stbl_children)
    minf = box(b"minf", box(b"vmhd", bytes([0, 0, 0, 1]) + struct.pack(">HHHH", 0, 0, 0, 0))
               + box(b"dinf", box(b"dref", struct.pack(">II", 0, 1) + box(b"url ", bytes([0, 0, 0, 1]))))
               + stbl)
    mdia = box(b"mdia", bytes(mdhd) + box(b"hdlr", struct.pack(">IHH", 0, 0, 0) + b"vide\x00\x00\x00\x00\x00" + b"tile-band\x00") + minf)
    trak = box(b"trak", bytes(tkhd) + mdia)
    moov = box(b"moov", bytes(mvhd) + trak)
    mdat = struct.pack(">I", 8 + sum(len(s) for s in samples)) + b"mdat" + b"".join(samples)
    open(out_fn, "wb").write(ftyp + moov + mdat)
    print(f"{chunk_fn}: {n} AUs -> {out_fn} (dur {dur}/{timescale}s)")


if __name__ == "__main__":
    main()
