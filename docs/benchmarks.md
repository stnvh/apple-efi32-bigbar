# Tesla P40 (MacPro1,1) — llama.cpp metrics

Machine `192.168.0.98` (macpro). Measured 2026-08-30.

## Hardware / stack
- Tesla P40, 24 GB, **power cap 150 W** (motherboard limit — do not change)
- MacPro1,1: Xeon 5150 (x86-64-v1, **no AVX**), 7.9 GB RAM, ~3 GB free disk, 2006 SATA
- Ubuntu 26.04, driver 580.173.02, CUDA 12.4 toolkit
- llama.cpp `build-cuda`: `-DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=61 -DGGML_NATIVE=ON`
  (NATIVE=ON is mandatory — generic baseline emits SSE4+ → `Illegal instruction`)

## Model
`Qwen3.8-27B-Uncensored-Q4_K_M.gguf` (16 GB, copied from M2 asahi, byte-verified).
Has embedded MTP/NextN layer (`blk.64.nextn.*`), activated with `--spec-type draft-mtp`.
Fits entirely in the 24 GB VRAM all-local (`-ngl 99`); no RPC.

## Generation benchmark
All-local, `-c 8192`, 256-token greedy, `-fa off`, prompt "Explain how a transformer…".
cold = first gen after load, warm = second gen (representative).

| Config | tg warm | tg cold | draft acceptance |
|---|---:|---:|---|
| Baseline (no spec) | **12.54** | 10.22 | — |
| **MTP depth 3** | **17.76** | 13.26 | 61.6% (165/268) |

- **MTP gives +42%** on this single GPU. MTP wins here at ~61% acceptance (which the
  distributed M2 cluster found sub-break-even) because a lone fast GPU makes draft
  verification cheap — no RPC hop.
- For context: P40 baseline ≈ 2× the distributed M2 cluster (6.6 tok/s); MTP ≈ 2.8×.

## Live in-chat (real prompts, observed)
Typical **14–18 tok/s** on normal replies (e.g. 787 tokens @ 14.90 tok/s, 48% draft
acceptance; 202 @ 13.92; 25 @ 17.82). High-acceptance/cached stretches burst far higher
(692 tokens @ 136.95 tok/s). Acceptance varies with content, so real-chat tg sits between
the baseline and best-case MTP benchmark figures. Endpoint binds `192.168.0.98` only
(loopback refuses — use the LAN IP for health checks).

## Operational notes
- **`gpe11` ACPI IRQ storm must be masked** or disk I/O stalls and the ~5 min cold load
  thrashes/OOMs: `echo mask > /sys/firmware/acpi/interrupts/gpe11` (root). Resets on
  reboot — persist via `mask-gpe11.service`.
- Cold load ≈ 5 min (16 GB over slow SATA, 7 GB RAM); SSH goes sluggish during it.
- Chat service: `llama-p40.service` → llama.cpp web UI + OpenAI API at
  `http://192.168.0.98:8080`, alias `Qwen3.8-27B-Uncensored-MTP`, MTP on, persistent
  (model pinned in VRAM, no auto-unload).
