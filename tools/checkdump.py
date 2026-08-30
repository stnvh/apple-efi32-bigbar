"""Test the '1MB decode limit' claim against our actual dump."""
import struct
import math
from collections import Counter

d = open('bootrom.bin', 'rb').read()
print("dump size: %d (0x%X)" % (len(d), len(d)))
print("")
print("=== content per 512KB quarter (a 1MB-limited read would show ===")
print("===  the upper half as all-0xFF or a mirror of the lower)   ===")
for i in range(4):
    q = d[i * 0x80000:(i + 1) * 0x80000]
    ff = q.count(0xFF) / len(q)
    c = Counter(q)
    n = len(q)
    H = -sum((v / n) * math.log2(v / n) for v in c.values())
    print("  0x%06X-0x%06X  0xFF=%5.1f%%  entropy=%.2f  distinct=%d"
          % (i * 0x80000, (i + 1) * 0x80000 - 1, ff * 100, H, len(c)))

print("")
print("=== is upper 1MB a mirror of lower 1MB? (aliasing signature) ===")
print("  identical halves: %s" % (d[:0x100000] == d[0x100000:]))

print("")
print("=== structures found ABOVE the 1MB line (proves >1MB was read) ===")
for off in (0x180000, 0x190000, 0x1C0000):
    sig = d[off + 0x28:off + 0x2C]
    hl = struct.unpack('<H', d[off + 0x30:off + 0x32])[0]
    s = sum(struct.unpack('<H', d[off + i:off + i + 2])[0]
            for i in range(0, hl, 2)) & 0xFFFF
    print("  0x%06X sig=%r hdrlen=0x%X checksum=0x%04X %s"
          % (off, sig, hl, s, "VALID" if s == 0 else "INVALID"))

print("")
print("=== unique machine data location ===")
i = d.find(b'CK6480CNUPZ')
print("  serial at 0x%X (%s 1MB line)" % (i, "above" if i > 0x100000 else "below"))
