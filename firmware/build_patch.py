#!/usr/bin/env python3
"""Add a PCI BAR override to a Mac Pro 1,1/2,1 BootROM.

Adds an entry to the firmware's IncompatiblePciDeviceSupport table declaring a
device's oversized BAR as something the 32-bit-EFI firmware can actually
allocate, so POST completes instead of hanging.

Everything is discovered from the image - nothing is hardcoded to a particular
dump. Writes only files; never touches hardware.

    python3 build_patch.py bootrom.bin bootrom_patched.bin
    python3 build_patch.py in.bin out.bin --vendor 0x10de --device 0x1b38 \
                           --bar 1 --size 256M

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

# EFI_INCOMPATIBLE_PCI_DEVICE_SUPPORT driver
TARGET_GUID = 'AD70855E-0CC5-4ABF-8979BE762A949EA3'
CRC32_SECTION_GUID = 'FC1BCDB0-7D31-49AA-936AA4600D9DD083'

DEVICE_INF_TAG = 0xFFF2
DEVICE_RES_TAG = 0xFFF1
LIST_END_TAG = 0x0000
ANY = 0xFFFF


def guid_str(b):
    a, c, e = struct.unpack('<IHH', b[:8])
    return "%08X-%04X-%04X-%s" % (a, c, e, b[8:].hex().upper())


def q(v):
    return struct.pack('<Q', v & 0xFFFFFFFFFFFFFFFF)


def parse_size(s):
    s = str(s).strip().upper()
    mult = {'K': 1 << 10, 'M': 1 << 20, 'G': 1 << 30}
    if s and s[-1] in mult:
        return int(s[:-1], 0) * mult[s[-1]]
    return int(s, 0)


def find_fv(rom):
    """Locate the first firmware volume containing FFS files."""
    for off in range(0, len(rom) - 0x30, 8):
        if rom[off + 0x28:off + 0x2C] == b'_FVH':
            hdrlen = struct.unpack('<H', rom[off + 0x30:off + 0x32])[0]
            fvlen = struct.unpack('<Q', rom[off + 0x20:off + 0x28])[0]
            if 0 < fvlen <= len(rom) and 0x40 <= hdrlen < 0x200:
                return off, hdrlen, fvlen
    sys.exit("no firmware volume found - is this really a BootROM dump?")


def walk_files(rom, fv, hdrlen, fvlen):
    """Yield (offset, size, guid) for each FFS file in the volume."""
    p = fv + hdrlen
    while p + 24 < fv + fvlen:
        if rom[p:p + 16] == b'\xff' * 16:
            break
        size = int.from_bytes(rom[p + 20:p + 23], 'little')
        if size < 24 or p + size > fv + fvlen:
            break
        yield p, size, guid_str(rom[p:p + 16])
        p += (size + 7) & ~7


def ffs_header(old, body):
    """Rebuild a 24-byte FFS header with corrected size and both checksums."""
    h = bytearray(old)
    h[20:23] = (len(body) + 24).to_bytes(3, 'little')
    h[17] = (-sum(body)) & 0xFF                       # file data checksum
    h[16] = 0
    h[16] = (-sum(h[i] for i in range(24)
                  if i not in (16, 17, 23))) & 0xFF   # header checksum
    return bytes(h)


def unwrap(body):
    """FFS body -> (compressed_hdr, decompressed, guid_sec_len, data_off)."""
    ssz = int.from_bytes(body[0:3], 'little')
    if body[3] != 0x01:
        sys.exit("expected a COMPRESSION section in the target file")
    comp = body[9:ssz]
    dec = bytearray(ec.TianoDecompress(comp, len(comp)))
    gsz = int.from_bytes(dec[0:3], 'little')
    if dec[3] != 0x02 or guid_str(dec[4:20]) != CRC32_SECTION_GUID:
        sys.exit("expected a CRC32 GUID-defined section")
    doff = struct.unpack('<H', dec[20:22])[0]
    return comp, dec, gsz, doff


def find_pe(payload):
    o = 0
    while o + 4 <= len(payload):
        sz = int.from_bytes(payload[o:o + 3], 'little')
        if sz < 4:
            break
        if payload[o + 3] == 0x10:
            return o + 4, sz - 4
        o += (sz + 3) & ~3
    sys.exit("no PE32 section inside the target module")


def read_table(raw):
    """Parse the UINT64 override list. Returns list of (offset, kind, values)."""
    qs = [struct.unpack('<Q', raw[i:i + 8])[0]
          for i in range(0, len(raw) - 7, 8)]
    out, i = [], 0
    while i < len(qs):
        t = qs[i]
        if t == DEVICE_INF_TAG:
            out.append((i * 8, 'INF', qs[i + 1:i + 6]))
            i += 6
        elif t == DEVICE_RES_TAG:
            out.append((i * 8, 'RES', qs[i + 1:i + 9]))
            i += 9
        elif t == LIST_END_TAG:
            out.append((i * 8, 'END', []))
            break
        else:
            i += 1
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('src')
    ap.add_argument('dst')
    ap.add_argument('--vendor', type=lambda x: int(x, 0), default=0x10DE)
    ap.add_argument('--device', type=lambda x: int(x, 0), default=0x1B38,
                    help='PCI device id, or 0xFFFF for any (default 0x1B38, Tesla P40)')
    ap.add_argument('--bar', type=int, default=1,
                    help='BAR index to override (default 1)')
    ap.add_argument('--size', default='256M',
                    help='size to declare, e.g. 256M (default 256M)')
    ap.add_argument('--replace', type=lambda x: int(x, 0), default=None,
                    help='vendor id of an existing entry to overwrite; '
                         'default picks the last one automatically')
    args = ap.parse_args()

    length = parse_size(args.size)
    if length & (length - 1):
        sys.exit("--size must be a power of two")
    align = length - 1

    rom = bytearray(open(args.src, 'rb').read())
    print("image: %s (%d bytes)" % (args.src, len(rom)))

    fv, hdrlen, fvlen = find_fv(rom)
    print("firmware volume at 0x%X, length 0x%X" % (fv, fvlen))

    files = list(walk_files(rom, fv, hdrlen, fvlen))
    target = next((f for f in files if f[2].startswith(TARGET_GUID[:8])), None)
    if not target:
        sys.exit("IncompatiblePciDeviceSupport (%s) not found - this firmware "
                 "may not use it" % TARGET_GUID)
    toff, tsize, _ = target
    print("target module at 0x%X, size 0x%X (%d files in volume)"
          % (toff, tsize, len(files)))

    old_hdr = bytes(rom[toff:toff + 24])
    body = bytes(rom[toff + 24:toff + tsize])
    comp, dec, gsz, doff = unwrap(body)
    payload = bytearray(dec[doff:gsz])

    pe_off, pe_len = find_pe(payload)
    pe = pefile.PE(data=bytes(payload[pe_off:pe_off + pe_len]))
    ds = next(s for s in pe.sections if s.Name.startswith(b'.data'))
    data_off = pe_off + ds.PointerToRawData
    raw = bytes(payload[data_off:data_off + ds.SizeOfRawData])

    entries = read_table(raw)
    infs = [e for e in entries if e[1] == 'INF']
    print("existing table entries: %s"
          % ', '.join("0x%04X" % e[2][0] for e in infs))

    if any(e[2][0] == args.vendor and e[2][1] == args.device for e in infs):
        sys.exit("an entry for %04X:%04X already exists"
                 % (args.vendor, args.device))

    # Pick an entry to overwrite. Replacing keeps the module the same size,
    # which avoids shifting every following file in the volume.
    if args.replace is not None:
        victim = next((e for e in infs if e[2][0] == args.replace), None)
        if not victim:
            sys.exit("no entry with vendor 0x%04X to replace" % args.replace)
    else:
        victim = infs[-1]
    ent = data_off + victim[0]
    print("overwriting entry for vendor 0x%04X at .data+0x%X"
          % (victim[2][0], victim[0]))

    new = (q(DEVICE_INF_TAG) + q(args.vendor) + q(args.device)
           + q(ANY) + q(ANY) + q(ANY)
           + q(DEVICE_RES_TAG) + q(0) + q(0) + q(0) + q(0) + q(0)
           + q(align) + q(args.bar) + q(length))
    assert len(new) == 0x78
    payload[ent:ent + 0x78] = new
    print("  -> %04X:%04X  BAR%d  MEM  align=0x%X len=0x%X (%s)"
          % (args.vendor, args.device, args.bar, align, length, args.size))

    # rewrap: new CRC32 over the payload, recompress, rebuild the FFS file
    dec[doff:gsz] = payload
    crc = zlib.crc32(bytes(payload)) & 0xFFFFFFFF
    dec[24:28] = struct.pack('<I', crc)
    re_c = bytes(ec.TianoCompress(bytes(dec), len(dec)))
    print("CRC32 0x%08X, compressed %d -> %d (%+d)"
          % (crc, len(comp), len(re_c), len(re_c) - len(comp)))

    nb = bytearray(body[:9] + re_c)
    nb[0:3] = len(nb).to_bytes(3, 'little')
    new_file = ffs_header(old_hdr, bytes(nb)) + bytes(nb)

    # re-emit every file; the target may have changed size, shifting the rest.
    # Safe: DXE drivers are relocatable PEs, position in the volume is not
    # meaningful.
    out = bytearray(rom[fv:fv + hdrlen])
    for off, size, _ in files:
        out += new_file if off == toff else bytes(rom[off:off + size])
        if len(out) % 8:
            out += b'\xff' * (8 - len(out) % 8)
    if len(out) > fvlen:
        sys.exit("volume overflow: needs 0x%X, have 0x%X" % (len(out), fvlen))
    print("re-emitted %d files, volume uses 0x%X of 0x%X (%d bytes free)"
          % (len(files), len(out), fvlen, fvlen - len(out)))
    out += b'\xff' * (fvlen - len(out))
    rom[fv:fv + fvlen] = out

    open(args.dst, 'wb').write(bytes(rom))
    print("\nwrote %s (%d bytes)" % (args.dst, len(rom)))
    print("now run: python3 validate.py %s %s" % (args.src, args.dst))


if __name__ == '__main__':
    main()
