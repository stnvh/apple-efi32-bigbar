# Installing Ubuntu 26.04 on a Mac Pro 1,1 / 2,1

These machines have **32-bit EFI**. Nothing about a modern Linux install works
normally on them, and the failures are silent and misleading. This is the full
procedure including every place it breaks.

Assumed situation: no usable video (the common case — these are being
resurrected as headless boxes), so everything is driven blind or over SSH.

## Why the normal route fails

**The firmware cannot launch a 64-bit EFI binary.** Every modern install ISO
ships only `BOOTX64.EFI`, so the USB simply does not appear as a boot option.
A 32-bit-EFI Mac needs `BOOTIA32.EFI`.

**These machines cannot boot from USB at all** in EFI mode. Even with a
correct 32-bit loader on the stick, the firmware will not enumerate it — it
never appears in the Option-key picker and `bless` silently falls back. Only
internal SATA, FireWire, and optical are bootable.

**A 32-bit EFI cannot start a 64-bit kernel directly.** The EFI stub is
bitness-locked. You need GRUB built for `i386-efi`, which loads the kernel
itself and performs the 32→64 transition. There is no shim, no patch, and
OpenCore/rEFInd do not help: they run at BDS, long after the constraint bites.

## Step 1 — build a 32-bit GRUB

Do this on any other Linux machine. Architecture does not matter —
`grub2-mkimage` cross-targets, and the ia32 modules are `noarch`. It worked on
an ARM64 Fedora box.

```bash
sudo dnf install grub2-efi-ia32-modules      # or: apt install grub-efi-ia32-bin
grub2-mkimage -O i386-efi -o BOOTIA32.EFI -p /EFI/boot \
  part_gpt part_msdos part_apple fat iso9660 hfsplus hfs ext2 \
  search search_fs_file search_label search_fs_uuid \
  normal linux chain configfile boot echo test true \
  all_video efi_gop efi_uga video video_fb terminal terminfo \
  ls cat halt reboot minicmd help sleep loadenv gzio probe

file BOOTIA32.EFI    # MUST say "PE32 ... Intel i386", NOT PE32+
```

`PE32+` means you built a 64-bit binary and the Mac will not launch it.

**Include `chain` and `hfsplus`** or the macOS fallback entry dies with
*"chainloader doesn't exist"* — and on a machine with no video that presents
as an unexplained hang.

## Step 2 — boot the installer

Because USB is not bootable, put the kernel and initrd on the **internal ESP**
and let the firmware boot those. The installer's squashfs can still live on the
USB: once the kernel is running, Linux's own USB stack sees the stick fine.
The firmware limitation only applies before the kernel starts.

```bash
# from macOS or another Linux, with the ISO mounted:
cp /mnt/iso/casper/vmlinuz /mnt/iso/casper/initrd  /Volumes/EFI/EFI/boot/
cp BOOTIA32.EFI                                    /Volumes/EFI/EFI/boot/
```

`/EFI/boot/grub.cfg` on the internal ESP:

```
set timeout=10
set default=0
insmod all_video
menuentry "Ubuntu installer" {
  linux /EFI/boot/vmlinuz ---
  initrd /EFI/boot/initrd
}
menuentry "macOS" {
  search --no-floppy --file --set=root /System/Library/CoreServices/boot.efi
  chainloader /System/Library/CoreServices/boot.efi
}
menuentry "Exit to firmware picker" { exit 1 }
```

Point the firmware at it from macOS:

```bash
sudo bless --mount /Volumes/EFI --file /Volumes/EFI/EFI/boot/BOOTIA32.EFI --setBoot
```

Keep the USB plugged in — casper finds the squashfs on it once Linux boots.

**Recovery**: this only rewrites an NVRAM pointer. If a boot fails, the
firmware falls back to the blessed macOS volume; a PRAM reset
(⌘⌥P R, release after the second chime) clears the pointer entirely. Nothing
here writes to the macOS partition.

## Step 3 — the installer will fail. This is expected.

Subiquity dies partway through with:

```
grub-install: error: /usr/lib/grub/i386-pc/modinfo.sh doesn't exist.
```

It detected a non-standard EFI setup, fell back to **BIOS** GRUB, and the
package isn't on the ISO. There is no way to make it succeed — subiquity has
no 32-bit-EFI code path.

**What matters: it fails at the bootloader stage, which is *after* the
filesystem copy but *before* user creation and package installation.** So you
are left with:

- a complete root filesystem on the target — usable
- **no user account**
- **no `openssh-server`**
- no bootloader

You finish it by hand. Get a shell from the installer: **Help → Enter shell**,
or `Ctrl+Alt+F2` at the console.

> SSH into the installer lands you in the Subiquity TUI, not a shell —
> it uses a `ForceCommand` and runs the UI in tmux, so passing a command to
> `ssh` does not bypass it. Starting a second `sshd` on another port does not
> help either if you log in as the same user. Use the console, or log in as a
> different user.

## Step 4 — finish the install from the installer shell

```bash
for d in dev dev/pts proc sys; do sudo mountpoint -q /target/$d || sudo mount --bind /$d /target/$d; done
sudo rm -f /target/etc/resolv.conf
echo "nameserver 1.1.1.1" | sudo tee /target/etc/resolv.conf

sudo chroot /target apt-get update
sudo chroot /target apt-get install -y openssh-server

sudo chroot /target useradd -m -s /bin/bash -G sudo,adm YOURNAME
sudo mkdir -p /target/home/YOURNAME/.ssh
echo 'ssh-rsa AAAA...your key...' | sudo tee /target/home/YOURNAME/.ssh/authorized_keys
sudo chmod 700 /target/home/YOURNAME/.ssh
sudo chmod 600 /target/home/YOURNAME/.ssh/authorized_keys
sudo chroot /target chown -R YOURNAME:YOURNAME /home/YOURNAME/.ssh

# temporary, so you are not locked out before setting a password
echo 'YOURNAME ALL=(ALL) NOPASSWD:ALL' | sudo tee /target/etc/sudoers.d/90-YOURNAME
sudo chmod 440 /target/etc/sudoers.d/90-YOURNAME

sudo chroot /target systemctl enable ssh
echo myhostname | sudo tee /target/etc/hostname

sudo rm -f /target/etc/resolv.conf
sudo ln -sf ../run/systemd/resolve/stub-resolv.conf /target/etc/resolv.conf
for d in sys proc dev/pts dev; do sudo umount -l /target/$d; done
```

Note the root partition UUID (`sudo blkid`) — you need it next.

**After first login**: `sudo passwd YOURNAME`, then
`sudo rm /etc/sudoers.d/90-YOURNAME`. In that order, or you lose sudo.

## Step 5 — boot the installed system

There is still no bootloader. Reuse the same `BOOTIA32.EFI` on the ESP,
pointing at the installed kernel. `/boot/vmlinuz` and `/boot/initrd.img` are
symlinks Ubuntu keeps current, so this survives kernel upgrades:

```
set timeout=10
set default=0
insmod all_video
insmod ext2
insmod part_gpt

menuentry "Ubuntu" {
  search --no-floppy --fs-uuid --set=root YOUR-ROOT-UUID
  linux /boot/vmlinuz root=UUID=YOUR-ROOT-UUID ro
  initrd /boot/initrd.img
}
menuentry "Ubuntu (nomodeset)" {
  search --no-floppy --fs-uuid --set=root YOUR-ROOT-UUID
  linux /boot/vmlinuz root=UUID=YOUR-ROOT-UUID ro nomodeset
  initrd /boot/initrd.img
}
menuentry "macOS" {
  search --no-floppy --file --set=root /System/Library/CoreServices/boot.efi
  chainloader /System/Library/CoreServices/boot.efi
}
menuentry "Exit to firmware picker" { exit 1 }
```

Optionally install `grub-efi-ia32` in the target so the system manages its own
bootloader — but the static config above works and has fewer moving parts.

## Console notes

**Do not use `nomodeset` on these machines.** It is the usual advice and it is
wrong here: the 32-bit firmware hands the kernel no GOP framebuffer, so there
is nothing for `efifb`/`vesafb` to fall back to. You get `nouveau` loaded but
idle, no `/dev/fb0`, and a console showing a single blank line. Boot **without**
`nomodeset` and let nouveau do KMS — that is what actually produces a working
console. Keep the entry as a fallback only.

**The boot picker needs a Mac-EFI card.** The firmware draws it, so a plain PC
GPU stays dark until the OS loads a driver, no matter what you install. Only a
card with Apple EFI in its ROM shows POST, the picker, or panics. Keeping one
alongside the compute card is worth it.

**Installing the NVIDIA driver blacklists nouveau**, which kills the console on
a dual-GPU machine. See [`../systemd/README.md`](../systemd/README.md).

## Other things worth knowing

**These CPUs are x86-64-v1.** The Xeon 5150 has `ssse3` and no SSE4 or AVX.
Ubuntu 26.04's kernel still runs, but CUDA userspace, PyTorch wheels and
prebuilt llama.cpp binaries will die with illegal-instruction. Build without
AVX.

**Two NICs, one cable = a 2-minute boot delay.** `systemd-networkd-wait-online`
waits for every managed interface. Mark the unused one `optional: true` in
netplan.

**FireWire may hang a kworker for ~2 minutes** during boot
(`fw_device_workfn` blocked in `read_config_rom`). Harmless; blacklist
`firewire_ohci` if it bothers you.
