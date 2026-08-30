#!/usr/bin/env python3
"""Validate a patched Mac Pro BootROM against the original.

Read-only. Any FAIL means the image must NOT be flashed.

    python3 validate.py bootrom.bin bootrom_patched.bin

Requires: pip install uefi_firmware pefile
"""
import argparse
import struct
import sys
import zlib

try:
    import pefile
    from uefi_firmware import efi_compressor as ec
except ImportError:
    sys.exit("need: pip install uefi_firmware pefile")

TARGET_PREFIX = 'AD70855E'
fails = []


def chk(cond, label, detail=''):
    print(("  PASS  " if cond else "  FAIL  ") + label +
          (("  " + detail) if detail else ''))
    if not cond:
        fails.append(label)


def guid_str(b):
    a, c, e = struct.unpack('<IHH', b[:8])
    return "%08X-%04X-%04X-%s" % (a, c, e, b[8:].hex().upper())


def find_fvs(rom):
    out, off = [], 0
    while off < len(rom) - 0x30:
        if rom[off + 0x28:off + 0x2C] == b'_FVH':
            hdrlen = struct.unpack('<H', rom[off + 0x30:off + 0x32])[0]
            fvlen = struct.unpack('<Q', rom[off + 0x20:off + 0x28])[0]
            if 0 < fvlen <= len(rom) and 0x40 <= hdrlen < 0x200:
                out.append((off, hdrlen, fvlen))
                off += fvlen
                continue
        off += 8
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('original')
    ap.add_argument('patched')
    args = ap.parse_args()

    a = open(args.original, 'rb').read()
    b = open(args.patched, 'rb').read()

    print("=== size ===")
    chk(len(a) == len(b), "same size as original",
        "%d vs %d" % (len(a), len(b)))
    chk(len(b) % (1 << 20) == 0 or len(b) in (0x100000, 0x200000, 0x400000),
        "size looks like a whole flash part", "%d bytes" % len(b))

    print("\n=== machine-unique data preserved ===")
    # any 11-char alnum run that appears once in both is a decent serial proxy
    import re
    ser = [s for s in re.findall(rb'[A-Z0-9]{11}', a) if a.count(s) == 1]
    if ser:
        chk(all(b.count(s) == a.count(s) for s in ser[:20]),
            "serial-like strings unchanged", "%d checked" % len(ser[:20]))
    macs = re.findall(rb'\x00\x17\xf2[\x00-\xff]{3}', a)
    if macs:
        chk(b.count(macs[0]) == a.count(macs[0]), "MAC bytes unchanged")

    fvs_a, fvs_b = find_fvs(a), find_fvs(b)
    chk(len(fvs_a) == len(fvs_b), "same number of firmware volumes",
        "%d" % len(fvs_b))

    print("\n=== regions outside the patched volume are byte-identical ===")
    fv, hdrlen, fvlen = fvs_b[0]
    chk(a[:fv] == b[:fv] if fv else True, "before the volume")
    chk(a[fv + fvlen:] == b[fv + fvlen:], "after the volume (incl. NVRAM)")

    print("\n=== FV header checksums (16-bit sum must be 0) ===")
    for off, hl, _ in fvs_b:
        s = sum(struct.unpack('<H', b[off + i:off + i + 2])[0]
                for i in range(0, hl, 2)) & 0xFFFF
        chk(s == 0, "FV@0x%06X header checksum" % off, "sum=0x%04X" % s)

    print("\n=== every FFS file: header + data checksums ===")
    p, n, bad, tgt = fv + hdrlen, 0, 0, None
    while p + 24 < fv + fvlen:
        if b[p:p + 16] == b'\xff' * 16:
            break
        size = int.from_bytes(b[p + 20:p + 23], 'little')
        if size < 24:
            break
        hdr, body = b[p:p + 24], b[p + 24:p + size]
        hc = (-sum(hdr[i] for i in range(24) if i not in (16, 17, 23))) & 0xFF
        fc = (-sum(body)) & 0xFF
        if hc != hdr[16] or fc != hdr[17]:
            bad += 1
            print("      bad checksum at 0x%X" % p)
        if guid_str(hdr[:16]).startswith(TARGET_PREFIX):
            tgt = p
        n += 1
        p += (size + 7) & ~7

    orig_count = 0
    pa, ha, fa = fvs_a[0]
    q = pa + ha
    while q + 24 < pa + fa:
        if a[q:q + 16] == b'\xff' * 16:
            break
        s2 = int.from_bytes(a[q + 20:q + 23], 'little')
        if s2 < 24:
            break
        orig_count += 1
        q += (s2 + 7) & ~7

    chk(n == orig_count, "file count matches original", "%d" % n)
    chk(bad == 0, "all FFS checksums valid", "%d bad" % bad)
    chk(tgt is not None, "IncompatiblePciDeviceSupport still present")

    if tgt is None:
        print("\nFAILURES: %s" % fails)
        sys.exit(1)

    print("\n=== decode the patched override table ===")
    size = int.from_bytes(b[tgt + 20:tgt + 23], 'little')
    body = b[tgt + 24:tgt + size]
    ssz = int.from_bytes(body[0:3], 'little')
    dec = bytes(ec.TianoDecompress(body[9:ssz], ssz - 9))
    gsz = int.from_bytes(dec[0:3], 'little')
    doff = struct.unpack('<H', dec[20:22])[0]
    stored = struct.unpack('<I', dec[24:28])[0]
    payload = dec[doff:gsz]
    chk(stored == (zlib.crc32(payload) & 0xFFFFFFFF), "section CRC32 valid",
        "0x%08X" % stored)

    o, pe_off, pe_len = 0, None, None
    while o + 4 <= len(payload):
        sz = int.from_bytes(payload[o:o + 3], 'little')
        if sz < 4:
            break
        if payload[o + 3] == 0x10:
            pe_off, pe_len = o + 4, sz - 4
        o += (sz + 3) & ~3
    pe = pefile.PE(data=payload[pe_off:pe_off + pe_len])
    chk(pe.FILE_HEADER.Machine == 0x14C, "PE is still i386 (32-bit)")

    ds = next(s for s in pe.sections if s.Name.startswith(b'.data'))
    raw = ds.get_data()
    qs = [struct.unpack('<Q', raw[i:i + 8])[0]
          for i in range(0, len(raw) - 7, 8)]
    i, entries = 0, []
    while i < len(qs):
        t = qs[i]
        if t == 0xFFF2:
            entries.append((qs[i + 1], qs[i + 2]))
            print("      DEVICE_INF %04X:%04X" % (qs[i + 1], qs[i + 2]))
            i += 6
        elif t == 0xFFF1:
            r = qs[i + 1:i + 9]
            print("        RES ResType=%d align=0x%X BAR=%d len=0x%X"
                  % (r[0], r[5], r[6], r[7]))
            i += 9
        elif t == 0:
            break
        else:
            i += 1
    chk(len(entries) > 0, "table parses and has entries",
        "%d" % len(entries))

    print("\n" + ("ALL CHECKS PASSED" if not fails else "FAILURES: %s" % fails))
    sys.exit(1 if fails else 0)


if __name__ == '__main__':
    main()
