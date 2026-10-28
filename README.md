# omega-kernel-template

Write a small kernel in [Omega](https://github.com/aien-dev/omega-mv), push, and get a receipt:
the same `kernel.om` compiled by the pinned Omega compiler, run **natively on AArch64** and as
**SPIR-V on PoCL's CPU OpenCL device**, and checked case by case against a Python reference.
It runs on GitHub's free `ubuntu-24.04-arm` runners. No GPU, no CUDA toolkit, no local install.

## Quickstart

1. Click **Use this template** → **Create a new repository**
   (or: `gh repo create my-kernel --template aien-dev/omega-kernel-template --public --clone`).
2. Edit `kernel.om`, and give each `fn` its expected answer in `reference.py`
   (optional: pin your own inputs in `cases.tsv`).
3. Commit and push.
4. Open the run under **Actions**: the summary says PASS or FAIL per case, and the
   `omega-kernel-receipt` artifact holds `receipt.json`, `kernel.spv`, the AArch64 disassembly and `SHA256SUMS`.

## What one run does

| step | tool | where it runs |
|---|---|---|
| `kernel.om` → OSC IR | `omc`, the Omega compiler, fetched from aien-dev/omega-mv at a pinned commit and sha256 | runner CPU |
| IR → SPIR-V 1.0 OpenCL kernel | `irspirv.om`, the Omega SPIR-V translator, compiled by that same `omc` | runner CPU |
| validate | `spirv-val --target-env opencl2.1`; the translator must also be byte-for-byte deterministic | runner CPU |
| run SPIR-V | PoCL 7.2 CPU device (one work-item per case) | runner CPU |
| run native | `omc` compiles each case to an AArch64 Linux program | runner CPU |
| expected values | `reference.py` in CPython | runner CPU |

A case passes when the AArch64 result equals `reference.py` **and** the SPIR-V result equals the
AArch64 result bit for bit (a trap must be the same trap on both). The check runs your `cases.tsv`
lines plus edge values and 32 seeded random inputs per `fn`, so the same commit always checks the
same cases. A wrong kernel fails: the branch
[`demo/wrong-kernel`](https://github.com/aien-dev/omega-kernel-template/tree/demo/wrong-kernel)
uses the textbook `(a + b) / 2`, which overflows `uint`, and its run is red.

## Honest scope

- This is **CPU evidence**. SPIR-V runs on PoCL's CPU device, not on a GPU. Nothing here runs on a
  GPU, emits sm_121 SASS, or uses the CUDA++ runtime; the receipt lists those as `not_run`.
- Kernels are scalar functions: integer types (`uchar ushort uint ulong byte short int long`) and
  `bool`, at most 6 parameters, returning one of those or `void`, with `while … bound N` loops,
  `if`/`else` and `trap N`. IR mode refuses `struct`, `global`, `text`, floats and host words
  (error 9 or 3). The names `main` and `ir_dump` are reserved by the check.
- Each case is a separate kernel launch with one work-item; this is a correctness check, not a
  speed test.

## Files you edit

- `kernel.om`: your Omega functions. The demo has `avg` (overflow-free average), `gcd` and `clamp`
  (which traps 3 on an empty range).
- `reference.py`: one Python function per `fn`, returning the exact answer, or `raise Trap(n)` where
  the kernel runs `trap n`. Results must fit the return type; signed values are plain negative ints.
- `cases.tsv`: optional fixed inputs, one per line: `fn arg1 arg2 …` (decimal, `true`/`false`).

## Run it on your own AArch64 Linux machine

Ubuntu 24.04 on arm64, from a clean checkout:

```sh
sudo apt-get install -y --no-install-recommends clang-18 libllvm18 libllvmspirvlib18.1 \
  ocl-icd-libopencl1 ocl-icd-opencl-dev spirv-tools curl
sh tools/fetch.sh
sh tools/check.sh
```

`check.sh` exits 0 for PASS, 1 for FAIL and 3 for NOT_RUN (wrong platform or missing tool), and
writes `build/summary.md` and `build/receipt/`. On x86-64 it reports NOT_RUN: the native half
needs AArch64.

## Pins and receipts

`toolchain.lock` pins the omega-mv commit, the sha256 of `omc`, `irspirv.om`, `opencl-host.c` and
`parity.py`, and the sha256 of the PoCL release asset. `tools/fetch.sh` refuses any file whose
hash differs. The PoCL asset is built from source on the same runner image by
`.github/workflows/toolchain.yml` and self-tested with the demo kernel before it is published.
`receipt.json` (schema `omega-kernel-receipt/1`) records the verdict, run URL, commit, pins, hashes
of the built translator, host, `kernel.ir` and `kernel.spv`, the Ubuntu package versions, the
OpenCL device PoCL reported, and every differing case.

## What is interim

The kernel compiler and the SPIR-V translator are Omega. The glue around them is not yet:

- `tools/fetch.sh` (POSIX sh + curl): successor is an Omega fetch-and-verify unit once seeded Omega
  has an HTTPS word.
- `tools/check.sh` (POSIX sh): successor is an Omega conductor in the style of omega-mv
  `seed/qualification/om2/gate.om`.
- `tools/cases.py` (Python) plans cases and writes the receipt; same Omega conductor successor.
  `reference.py` stays Python on purpose: an oracle independent of `omc`.
- `opencl-host.c` and `parity.py` come from omega-mv `seed/spirv/`, where their status is tracked.

## Human-Directed Agentic Engineering

Current and future development follows Human-Directed Agentic Engineering (HDAE):
humans define goals, architecture, constraints, acceptance criteria, and stopping
points; agents implement, execute checks, diagnose failures, and revise within
those boundaries. Human review retains responsibility for claims, acceptance,
and release decisions. This describes the development method, not kernel runtime
behavior or a retrospective claim about earlier receipts.

For the documented method and a systems-software case study, see Gregory M.
Kurtzer (2026), [*Ginux: Building an Independent Kernel That Runs Linux Userspace
through Human-Directed Agentic Engineering*](https://doi.org/10.5281/zenodo.23682548).

## License

Apache-2.0; see `LICENSE` and `NOTICE`.
