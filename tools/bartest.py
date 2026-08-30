"""Read a few dwords from BAR0 (known good, 32-bit) and BAR1 (manually
placed at 0x800000000) to see whether the chipset decodes above 4GB.
Run as root:  sudo python3 bartest.py"""
import mmap
import os
import struct

PAGE = 4096


def peek(phys, count=4):
    off = phys & ~(PAGE - 1)
    delta = phys - off
    fd = os.open('/dev/mem', os.O_RDONLY | getattr(os, 'O_SYNC', 0))
    try:
        m = mmap.mmap(fd, PAGE * 2, mmap.MAP_SHARED,
                      mmap.PROT_READ, offset=off)
    finally:
        os.close(fd)
    vals = [struct.unpack('<I', m[delta + i * 4:delta + i * 4 + 4])[0]
            for i in range(count)]
    m.close()
    return vals


for name, addr in (("BAR0 (regs, 0xc3000000)", 0xc3000000),
                   ("BAR1 (VRAM ap, 0x800000000)", 0x800000000)):
    try:
        v = peek(addr)
        print("%-30s %s" % (name, ' '.join('%08x' % x for x in v)))
        if all(x == 0xFFFFFFFF for x in v):
            print("%-30s -> all 0xFF = NO DECODE (nothing responds)" % "")
        elif all(x == 0 for x in v):
            print("%-30s -> all zero (decodes, or reads as 0)" % "")
        else:
            print("%-30s -> live data = DECODES" % "")
    except Exception as e:
        print("%-30s ERROR: %s" % (name, e))

print("")
print("BAR0 dword0 is NVIDIA PMC_BOOT_0 - for GP102 expect 0x1?0000a1-ish")
