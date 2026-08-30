# Power limiting and persistence

The Tesla P40 is a **250 W passively-cooled** card. If it is fed from the Mac
Pro logic board's 6-pin aux connectors (~75 W each) rather than a PSU-fed
supply, it must be capped or you risk cooking the board connectors under load.

```bash
sudo nvidia-smi -pl 150      # 125 W is the card's minimum, 250 W the default
```

Pascal loses very little throughput in the 150–180 W band.

## The limit will not stick without persistence

`nvidia-smi -pl` writes to driver state. When the **last client closes the
GPU**, the driver tears that state down and the limit reverts to 250 W. On an
idle headless box that is immediately, so the setting appears to "not work".

Three things have to be true, and each failed separately here.

### 1. `nvidia-persistenced` must be running

It is `static` (no `[Install]` section), so `systemctl enable` does not work on
it directly. `p40-powerlimit.service` declares `Requires=`, which pulls it in.

### 2. It must not be started with `--no-persistence-mode`

Ubuntu ships the unit with `--no-persistence-mode`, so the daemon starts,
registers the GPU and reports healthy without ever enabling persistence.
`nvidia-smi` says *"persistence mode is disabled"* while `systemctl is-active`
says `active`.

```bash
systemctl cat nvidia-persistenced | grep ExecStart    # check for the flag
sudo systemctl edit nvidia-persistenced
```

```ini
[Service]
ExecStart=
ExecStart=/usr/bin/nvidia-persistenced --user nvidia-persistenced --verbose
```

The empty `ExecStart=` is required — it clears the original before the
replacement is applied.

### 3. Its runtime directory must be writable by its user

If `/run/nvidia-persistenced` is left over from a previous run with the wrong
ownership, the daemon starts, registers the device, then takes `SIGTERM`
immediately:

```
device 0000:08:00.0 - registered
Local RPC services initialized
Received signal 15
The daemon no longer has permission to remove its runtime data directory
```

Fix it permanently with a tmpfiles rule:

```bash
echo 'd /run/nvidia-persistenced 0755 nvidia-persistenced nvidia-persistenced -' \
  | sudo tee /etc/tmpfiles.d/nvidia-persistenced.conf
sudo systemd-tmpfiles --create
```

## Install

```bash
sudo cp p40-powerlimit.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart nvidia-persistenced
sudo systemctl enable --now p40-powerlimit.service
nvidia-smi --query-gpu=persistence_mode,power.limit --format=csv
```

Expected:

```
persistence_mode, power.limit [W]
Enabled, 150.00 W
```

## Notes

- **Reloading the nvidia driver resets the limit.** After any
  `rmmod nvidia` / `modprobe nvidia`, re-run
  `sudo systemctl restart p40-powerlimit.service`.
- The exposure from a lapsed limit is smaller than it looks: the reset only
  happens when *no client holds the GPU*, which is exactly when the card is
  idle at ~45 W. Once a CUDA process attaches, the limit in force applies for
  that process's lifetime. Getting this right still matters for unattended
  jobs, but nothing is at risk while the card sits idle.
- If persistence keeps fighting you, applying `-pl 150` from whatever wrapper
  launches your workload is a perfectly serviceable alternative.

## `mask-gpe11.service`

Unrelated to the GPU but required on MacPro1,1: ACPI GPE 11 fires continuously
and stalls disk I/O. Loading a 16 GB model thrashes and can OOM with it
unmasked. The mask resets every boot.

```bash
sudo cp mask-gpe11.service /etc/systemd/system/
sudo systemctl enable --now mask-gpe11.service
cat /sys/firmware/acpi/interrupts/gpe11     # should show "masked"
```
