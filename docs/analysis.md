# BootROM analysis notes

Reference material for anyone extending the firmware patch. All of this was
derived from the machine's own ROM rather than from EDK sources, because the
2007 build differs from EDK2 master in places that matter.

## ROM layout

`MP11.88Z.005C.B08.0707021221`, ST M50FW016, 2 MB, FWH @ `0xFFE00000`,
chipset Intel 631xESB/632xESB/3100 (ESB2).

```
0x000000  FFS1        1.5 MB   main DXE volume, 170 files
0x180000  FFS1         16 KB
0x190000  NVRAM_EVSA  192 KB   efi-boot-device and friends
0x1C0000  FFS1        256 KB   boot block
```

FFS1 (`7A9354D9-0468-444A-81CE-0BF617D890DF`), revision 1 — the EFI 1.x
format, consistent with Apple EFI 1.10. Phoenix-derived: `$IBIOSI$` is present
and many GUIDs match generic Phoenix ones, which is why `uefi-firmware-parser`
mislabels them as `LENOVO_*`.

Free space at the end of FV0 is ~460 KB, so modules can grow; `build_patch.py`
re-emits every file sequentially and shifts the remainder. This is safe
because DXE drivers are relocatable PEs — position within the volume is not
meaningful.

## Modules of interest

| GUID | Role | Size |
|---|---|---|
| `93b80004-9fb3-11d4-9a3a-0090273fc14d` | PciBusDxe | 29,856 B PE, i386 |
| `ad70855e-0cc5-4abf-8979-be762a949ea3` | IncompatiblePciDeviceSupport | 1,920 B PE, i386 |
| `35b898ca-b6a9-49ce-8c72-904735cc49b7` | DxeCore | 44,288 B |

Containment for each driver:

```
FFS file (type 0x07)
└─ COMPRESSION section (Tiano / EFI 1.1)
   └─ GUID_DEFINED  fc1bcdb0-7d31-49aa-936a-a4600d9dd083  (CRC32)
      ├─ DXE depex
      └─ PE32
```

Tiano compression round-trips **byte-identically** on this ROM
(`TianoCompress(TianoDecompress(x)) == x`), so unchanged data rebuilds exactly.

## Why it hangs

`PciBusDxe` contains **zero** `jmp $` (`EB FE`) deadloops and 12
`EFI_OUT_OF_RESOURCES` (`0x80000009`) return paths — it is built to fail
gracefully. `DxeCore` contains **two** deadloops. So the failure is
allocation failure → caller ASSERT → `CpuDeadLoop()` in DxeCore, which
presents as a silent hang with no video and no network.

## The override table

`IncompatiblePciDeviceSupport` holds a flat `UINT64` list at `.data`
(`0x100004A0`), variable-length records:

```
DEVICE_INF_TAG 0xFFF2 + 5 IDs           = 6 UINT64 = 0x30 bytes
DEVICE_RES_TAG 0xFFF1 + 8 fields        = 9 UINT64 = 0x48 bytes
LIST_END_TAG   0x0000
0xFFFF in an ID field = wildcard
```

Resource fields, in order: `ResType, GenFlag, SpecificFlag,
AddrSpaceGranularity, AddrRangeMin, AddrRangeMax, AddrTranslationOffset,
AddrLen`.

Apple shipped the stock EDK1 table untouched — five legacy I/O-space
workarounds (Adaptec `0x9004`/`0x9005`, QLogic `0x1077`, HP `0x103C`,
Agilent `0x15BC`). `build_patch.py` repurposes the Agilent entry, which keeps
the edit small.

## Verified semantics (from the binary, not from EDK2 master)

`PciBusDxe` locates the protocol and calls it with seven arguments:

```asm
0x100025DE  push 0x10006f58              ; &IncompatiblePciDeviceSupport GUID
0x100025E3  call [eax+0xac]              ; gBS->LocateProtocol
0x10002604  movzx ecx, word [esi+0xa2]   ; SubsystemDeviceId
0x1000260C  movzx ecx, word [esi+0xa0]   ; SubsystemVendorId
0x10002614  movzx ecx, byte [esi+0x7c]   ; RevisionId
0x10002619  movzx ecx, word [esi+0x76]   ; DeviceId
0x1000261E  movzx ecx, word [esi+0x74]   ; VendorId
0x10002624  call dword ptr [eax]         ; This->CheckDevice(...)
```

Descriptor consumption:

```asm
0x1000264B  mov eax, [ebx + 0x1e]    ; AddrTranslationOffset = BAR INDEX
0x1000264E  cmp eax, 0xff            ; 0xFF = wildcard, all BARs 0..5
0x10002669  cmp ecx, 6               ; bounds check
0x10002673  lea edi, [ecx + 6]       ; &PciBar[BarIndex]
0x10002676  shl edi, 5               ;   32 bytes per entry
0x1000267B  movzx eax, byte [ebx+3]  ; ResType: 0=MEM, 1=IO
0x100026A2  push [ebx + 0x16]        ; AddrRangeMax -> SetNewAlign
0x100026AA  mov eax, [ebx + 0x26]    ; AddrLen
0x100026BB  mov [edi], eax           ; PciBar[BarIndex].Length = AddrLen
```

`SetNewAlign` (`0x100026EF`) treats certain values as **sentinels**, not
alignments:

```
0xFFFFFFFFFFFFFFFF  OLD_ALIGN    -> return unchanged
0xFFFFFFFFFFFFFFFE  EVEN_ALIGN   \
0xFFFFFFFFFFFFFFFD  SQUAD_ALIGN   } round the EXISTING alignment
0xFFFFFFFFFFFFFFFC  DQUAD_ALIGN  /
anything else                    -> *Alignment = value   (stored as size-1)
```

This matters: using `EVEN_ALIGN (-2)` for `AddrRangeMax` — copied from the
stock I/O entries — leaves the P40's original **32 GB** alignment in place, so
allocation still fails even with `Length` reduced. A literal value
(`size - 1`) is required.

`PciBusDxe` also contains live 64-bit granularity handling
(`cmp dword [eax], 0x40`), so the firmware can parse QWORD descriptors — but
the root bridge advertises no aperture above 4 GB, which is why the
`AddrSpaceGranularity = 64` approach does not work on its own.

## Integrity layers

No cryptographic verification exists anywhere in the image: zero PKCS#1,
X.509, PKIX or SHA-2 OIDs, and `guided.certs` is 0 bytes on every module.
2006 Macs predate secure boot. Four checksum layers, all reproducible:

| Layer | Algorithm |
|---|---|
| FV header | 16-bit sum over header, must total 0 |
| FFS header | 8-bit sum, excluding IntegrityCheck and State bytes |
| FFS file data | 8-bit sum (`ATTRIB_CHECKSUM` set on all 170 files) |
| Guided section | CRC32 |

The `.scap` in `/EFI/APPLE/EXTENSIONS/` on the ESP is **not** the BootROM — it
decompresses to a 23 MB copy of Arial Unicode MS, a font resource for
firmware-level text rendering.

## Flash write-protect

The FWH's per-block lock registers are set with **lock-down** during POST.
flashrom can read freely but any erase aborts:

```
Changing lock bits failed ... New value: 0x03   (bit0 Write Lock, bit1 Lock Down)
Ready:BE RUN/FINISH:BE ERROR:PROG OK:VPP OK:PROG RUN/FINISH:WP|TBL#|WP#,ABORT
ERASE FAILED!
```

Lock-down can only be cleared by a reset, so no software sequence unlocks it
from a normally-booted system. The chipset side is *not* the obstacle —
`BIOS_CNTL = 0x00` (`BLE=0`), and flashrom sets `BIOSWE` itself.

**The firmware-update boot state leaves the blocks unlocked.** Shut down fully,
hold the power button until the LED flashes rapidly and a long beep sounds,
release on the beep. The machine boots normally with the chip writable.
Successful runs show `UNLOCK:` in the erase output where a locked one shows
`WP|TBL#|WP#,ABORT`. The unlock does not survive a reset.
