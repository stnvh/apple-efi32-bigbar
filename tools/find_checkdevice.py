"""Locate the CheckDevice call site in PciBusDxe and disassemble it."""
import struct
import pefile
from capstone import Cs, CS_ARCH_X86, CS_MODE_32

P = ('ext/volume-0/file-93b80004-9fb3-11d4-9a3a-0090273fc14d'
     '/section0/section0/section0.pe')
pe = pefile.PE(P)
base = pe.OPTIONAL_HEADER.ImageBase
blob = open(P, 'rb').read()
t = [s for s in pe.sections if s.Name.startswith(b'.text')][0]
code = t.get_data()[:t.Misc_VirtualSize]
va = base + t.VirtualAddress
md = Cs(CS_ARCH_X86, CS_MODE_32)

guid = (struct.pack('<IHH', 0xEB23F55A, 0x7863, 0x4AC2)
        + bytes([0x8D, 0x3D, 0x95, 0x65, 0x35, 0xDE, 0x03, 0x75]))
foff = blob.find(guid)

# map file offset -> RVA
rva = None
for s in pe.sections:
    if s.PointerToRawData <= foff < s.PointerToRawData + s.SizeOfRawData:
        rva = s.VirtualAddress + (foff - s.PointerToRawData)
        sect = s.Name.rstrip(b'\x00').decode()
print("GUID at file 0x%X -> RVA 0x%X (%s) -> VA 0x%X" % (foff, rva, sect, base + rva))

target = struct.pack('<I', base + rva)
refs = [i for i in range(len(code) - 3) if code[i:i + 4] == target]
print("references to that VA in .text: %s" % [hex(va + r) for r in refs])

for r in refs:
    lo, hi = max(0, r - 60), min(len(code), r + 90)
    print("")
    print("=== around ref at 0x%X ===" % (va + r))
    for ins in md.disasm(code[lo:hi], va + lo):
        mark = '  <<< GUID' if va + r in (ins.address, ins.address + 1) else ''
        print("0x%08X  %-7s %s%s" % (ins.address, ins.mnemonic, ins.op_str, mark))
