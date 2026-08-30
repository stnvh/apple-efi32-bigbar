"""Verify, against the ACTUAL PciBusDxe binary in this ROM, that it
consumes the IncompatiblePciDeviceSupport protocol and how."""
import struct
import pefile

P = ('ext/volume-0/file-93b80004-9fb3-11d4-9a3a-0090273fc14d'
     '/section0/section0/section0.pe')
pe = pefile.PE(P)
base = pe.OPTIONAL_HEADER.ImageBase
blob = open(P, 'rb').read()

# EFI_INCOMPATIBLE_PCI_DEVICE_SUPPORT_PROTOCOL_GUID
# {eb23f55a-7863-4ac2-8d3d-956535de0375}
guid = (struct.pack('<IHH', 0xEB23F55A, 0x7863, 0x4AC2)
        + bytes([0x8D, 0x3D, 0x95, 0x65, 0x35, 0xDE, 0x03, 0x75]))
i = blob.find(guid)
print("IncompatiblePciDeviceSupport protocol GUID in PciBusDxe: %s"
      % ("FOUND at 0x%X" % i if i >= 0 else "NOT FOUND"))

print("")
print("=== ACPI descriptor handling (proves it parses CheckDevice output) ===")
# 0x8A = ACPI QWORD Address Space Descriptor, 0x79 = end tag
for val, label in ((0x8A, 'QWORD Address Space Descriptor'),
                   (0x79, 'ACPI end tag'),
                   (0x0A, 'ACPI small end tag alt')):
    # cmp al, imm8 / cmp byte ptr, imm8
    n = blob.count(bytes([0x3C, val])) + blob.count(bytes([0x80, 0x38, val]))
    print("  compares against 0x%02X (%s): %d site(s)" % (val, label, n))

print("")
print("=== PCI_MAX_BAR / wide-match constant 0xFF used as BAR index ===")
# PCI_MAX_BAR is 6 in EDK1/EDK2; 0xFF is the 'all BARs' sentinel
for val in (0x06, 0xFF):
    n = blob.count(bytes([0x83, 0xF8, val]))   # cmp eax, imm8
    print("  cmp eax, 0x%02X : %d site(s)" % (val, n))

print("")
print("=== alignment sentinels present in code/data? ===")
for name, v in (('OLD_ALIGN  0xFFFFFFFFFFFFFFFF', 0xFFFFFFFFFFFFFFFF),
                ('EVEN_ALIGN 0xFFFFFFFFFFFFFFFE', 0xFFFFFFFFFFFFFFFE),
                ('SQUAD_ALIGN 0xFFFFFFFFFFFFFFFD', 0xFFFFFFFFFFFFFFFD),
                ('DQUAD_ALIGN 0xFFFFFFFFFFFFFFFC', 0xFFFFFFFFFFFFFFFC)):
    print("  %-32s occurrences=%d" % (name, blob.count(struct.pack('<Q', v))))
print("  (0xFE/0xFD/0xFC as 32-bit halves are what a 32-bit build compares)")
for name, v in (('EVEN  as -2 dword', 0xFFFFFFFE),
                ('SQUAD as -3 dword', 0xFFFFFFFD),
                ('DQUAD as -4 dword', 0xFFFFFFFC)):
    n = blob.count(bytes([0x83, 0xF8]) + bytes([v & 0xFF]))
    print("  cmp eax,%s : %d site(s)" % (name, n))
