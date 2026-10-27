#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Case planner and verdict writer for tools/check.sh (verification side only).

It never generates IR, SPIR-V or native code. It reads kernel.om signatures,
cases.tsv and reference.py, plans the cases, and after the runs compares the
AArch64 and SPIR-V results with reference.py, then writes the receipt.

INTERIM host glue (Python). Successor: an Omega conductor unit; the reference
oracle stays a separate language on purpose, so it is independent of omc.

usage: cases.py expand BUILD_DIR | cases.py report BUILD_DIR
"""
import datetime
import hashlib
import json
import os
import platform
import random
import re
import shutil
import subprocess
import sys

MASK = (1 << 64) - 1
WIDTH = {'uchar': 8, 'ushort': 16, 'uint': 32, 'ulong': 64,
         'byte': 8, 'short': 16, 'int': 32, 'long': 64}
SIGNED = {'byte', 'short', 'int', 'long'}
RETURNS = set(WIDTH) | {'bool', 'void'}
SIGNATURE = re.compile(r'^fn\s+(\w+)((?:\s+\w+\s+\w+)*)\s+to\s+(\w+)\s*$')
RANDOM_CASES = int(os.environ.get('RANDOM_CASES', '32'))
SEED = os.environ.get('CASE_SEED', 'omega-kernel-template')
NOT_RUN = ['any GPU (no GPU is used by this check)', 'sm_121 SASS', 'CUDA++ runtime']


class Trap(Exception):
    """Raise Trap(n) in reference.py where the kernel executes `trap n`."""

    def __init__(self, code):
        super().__init__(code)
        if not 1 <= int(code) <= 14:
            raise ValueError(f'trap code {code} outside 1..14')
        self.code = int(code)


class Bad(Exception):
    """An input or reference problem; reported as a FAIL with this message."""


def span(kind):
    if kind == 'bool':
        return 0, 1
    bits = WIDTH[kind]
    if kind in SIGNED:
        return -(1 << (bits - 1)), (1 << (bits - 1)) - 1
    return 0, (1 << bits) - 1


def signatures(path='kernel.om'):
    funcs = {}
    with open(path, encoding='utf-8') as handle:
        for number, line in enumerate(handle, 1):
            if not line.startswith('fn '):
                continue
            match = SIGNATURE.match(line.strip())
            if not match:
                raise Bad(f'kernel.om:{number}: cannot read the signature "{line.strip()}"')
            name, raw, ret = match[1], match[2].split(), match[3]
            params = list(zip(raw[0::2], raw[1::2]))
            for pname, kind in params:
                if kind not in WIDTH and kind != 'bool':
                    raise Bad(f'kernel.om:{number}: {name} parameter {pname} has type {kind}; '
                              'the SPIR-V path at this pin takes integer and bool scalars only')
            if ret not in RETURNS:
                raise Bad(f'kernel.om:{number}: {name} returns {ret}; '
                          'the SPIR-V path at this pin returns integer, bool or void only')
            if len(params) > 6:
                raise Bad(f'kernel.om:{number}: {name} has {len(params)} parameters; the OpenCL host passes at most 6')
            if name in ('main', 'ir_dump'):
                raise Bad(f'kernel.om:{number}: the name {name} is reserved by the check harness')
            funcs[name] = (params, ret)
    if not funcs:
        raise Bad('kernel.om defines no fn')
    return funcs


def parse_value(kind, text, where):
    if kind == 'bool':
        if text not in ('true', 'false'):
            raise Bad(f'{where}: bool argument must be true or false, got {text}')
        return text == 'true'
    if not re.fullmatch(r'-?[0-9]+', text):
        raise Bad(f'{where}: {kind} argument must be a decimal integer, got {text}')
    value = int(text)
    low, high = span(kind)
    if not low <= value <= high:
        raise Bad(f'{where}: {value} is outside {kind} range {low}..{high}')
    return value


def edges(kind):
    if kind == 'bool':
        return [False, True]
    low, high = span(kind)
    return sorted({0, 1, high, high - 1, low, low + 1} | ({-1} if kind in SIGNED else set()))


def load_reference(path='reference.py'):
    space = {'Trap': Trap, '__name__': 'reference'}
    with open(path, encoding='utf-8') as handle:
        code = compile(handle.read(), path, 'exec')
    exec(code, space)  # noqa: S102 - the developer's own oracle, run on their own runner
    return space


def expect(reference, name, params, ret, args):
    shown = f'{name}({", ".join(str(a).lower() if isinstance(a, bool) else str(a) for a in args)})'
    try:
        value = reference[name](*args)
    except Trap as trap:
        return 0, 100 + trap.code
    except Exception as error:  # noqa: BLE001 - every reference fault is a named FAIL
        raise Bad(f'reference.py {shown} raised {type(error).__name__}: {error}')
    if ret == 'void':
        if value is not None:
            raise Bad(f'reference.py {shown} returned {value!r}; {name} returns void, so return None')
        return 0, 0
    if ret == 'bool':
        if not isinstance(value, bool):
            raise Bad(f'reference.py {shown} returned {value!r}; {name} returns bool')
        return int(value), 0
    if isinstance(value, bool) or not isinstance(value, int):
        raise Bad(f'reference.py {shown} returned {value!r}; {name} returns {ret}, an integer')
    low, high = span(ret)
    if not low <= value <= high:
        raise Bad(f'reference.py {shown} returned {value}, outside {ret} range {low}..{high}; '
                  'reduce it the way the kernel should (for example % 2**32)')
    return value & MASK, 0


def expand(build):
    funcs = signatures()
    reference = load_reference()
    missing = [name for name in funcs if not callable(reference.get(name))]
    if missing:
        raise Bad(f'reference.py has no function for {", ".join(missing)}; every kernel fn needs one')
    planned = []
    seen = set()

    def add(name, args, source):
        key = (name, tuple(args))
        if key in seen:
            return
        seen.add(key)
        planned.append((name, list(args), source))

    if os.path.exists('cases.tsv'):
        with open('cases.tsv', encoding='utf-8') as handle:
            for number, line in enumerate(handle, 1):
                words = line.split()
                if not words or words[0].startswith('#'):
                    continue
                where = f'cases.tsv:{number}'
                name = words[0]
                if name not in funcs:
                    raise Bad(f'{where}: kernel.om has no fn {name}')
                params, _ = funcs[name]
                if len(words) - 1 != len(params):
                    raise Bad(f'{where}: {name} takes {len(params)} arguments, got {len(words) - 1}')
                add(name, [parse_value(k, w, where) for (_, k), w in zip(params, words[1:])], where)
    for name, (params, _) in funcs.items():
        rng = random.Random(f'{SEED}:{name}')
        if params:
            pools = [edges(kind) for _, kind in params]
            for _ in range(16):
                add(name, [rng.choice(pool) for pool in pools], 'edge')
        else:
            add(name, [], 'edge')
        for _ in range(RANDOM_CASES):
            args = []
            for _, kind in params:
                low, high = span(kind)
                args.append(bool(rng.getrandbits(1)) if kind == 'bool' else rng.randint(low, high))
            add(name, args, 'random')
    rows = []
    for index, (name, args, source) in enumerate(planned, 1):
        params, ret = funcs[name]
        bits, status = expect(reference, name, params, ret, args)
        typed, omargs = [], []
        for (_, kind), value in zip(params, args):
            text = ('true' if value else 'false') if kind == 'bool' else str(value)
            typed.append(f'{kind}:{text}')
            omargs.append(text if kind == 'bool' else f'{kind} {text}')
        rows.append('\t'.join([str(index), name, ret, ','.join(typed) or '-', ' '.join(omargs) or '-',
                               str(bits), str(status), source]))
    with open(os.path.join(build, 'plan.tsv'), 'w', encoding='ascii') as handle:
        handle.write('\n'.join(rows) + '\n')
    counts = {}
    for _, _, source in planned:
        kind = 'cases.tsv' if source.startswith('cases.tsv') else source
        counts[kind] = counts.get(kind, 0) + 1
    print(f'PLAN cases={len(rows)} functions={len(funcs)} ' +
          ' '.join(f'{k}={v}' for k, v in sorted(counts.items())))


def sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def read_tsv(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding='ascii') as handle:
        return [line.rstrip('\n').split('\t') for line in handle if line.strip()]


def show(ret, bits, status):
    if status is None:
        return 'not run'
    if status:
        return f'trap {status - 100}' if 101 <= status <= 114 else f'exit {status}'
    if ret == 'void':
        return '-'
    if ret == 'bool':
        return 'true' if bits else 'false'
    if ret in SIGNED:
        return str(bits - (1 << 64) if bits >> 63 else bits)
    return str(bits)


def command_line(*argv):
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if done.returncode:
        return None
    text = (done.stdout or done.stderr).strip().splitlines()
    return text[0] if text else None


def versions():
    found = {
        'python': platform.python_version(),
        'cc': command_line(os.environ.get('CC', 'cc'), '--version'),
        'spirv-val': command_line(os.environ.get('SPIRV_VAL', 'spirv-val'), '--version'),
    }
    if shutil.which('dpkg-query'):
        for package in ('clang-18', 'libllvm18', 'libllvmspirvlib18.1', 'ocl-icd-libopencl1',
                        'ocl-icd-opencl-dev', 'spirv-tools'):
            found[f'deb:{package}'] = command_line('dpkg-query', '-W', '-f=${Version}', package)
    return found


def report(build):
    receipt_dir = os.path.join(build, 'receipt')
    lock = dict(line.strip().split('=', 1) for line in open('toolchain.lock', encoding='ascii')
                if '=' in line and not line.startswith('#'))
    stop = read_tsv(os.path.join(build, 'stop.tsv'))
    plan = read_tsv(os.path.join(build, 'plan.tsv'))
    parity = {row[0]: row for row in read_tsv(os.path.join(build, 'case-spirv-parity.tsv'))[1:]}
    cases = []
    for index, name, ret, typed, omargs, bits, status, source in plan:
        native_status = native_bits = spirv_status = spirv_bits = None
        if index in parity:
            row = parity[index]
            native_status, native_bits = int(row[4]), int(row[5])
            spirv_status, spirv_bits = int(row[6]), int(row[7])
        expected = (int(bits), int(status))
        aarch64_ok = native_status is not None and (native_bits, native_status) == expected
        spirv_ok = spirv_status is not None and (spirv_bits, spirv_status) == (native_bits, native_status)
        cases.append({
            'case': int(index), 'fn': name, 'returns': ret, 'args': omargs, 'source': source,
            'reference': show(ret, *expected),
            'aarch64': show(ret, native_bits, native_status),
            'spirv_pocl_cpu': show(ret, spirv_bits, spirv_status),
            'aarch64_matches_reference': aarch64_ok, 'spirv_matches_aarch64': spirv_ok,
            'verdict': 'PASS' if aarch64_ok and spirv_ok else 'FAIL',
        })
    failed = [c for c in cases if c['verdict'] != 'PASS']
    if stop:
        verdict, stage, message = stop[0]
    elif not cases:
        verdict, stage, message = 'FAIL', 'cases', 'no cases were planned'
    elif failed:
        verdict, stage = 'FAIL', 'compare'
        message = f'{len(failed)} of {len(cases)} cases differ'
    else:
        verdict, stage = 'PASS', 'compare'
        message = f'all {len(cases)} cases match on AArch64 and SPIR-V (PoCL CPU)'

    os.makedirs(receipt_dir, exist_ok=True)
    copies = ['kernel.om', 'cases.tsv', 'reference.py', 'toolchain.lock',
              os.path.join(build, 'kernel.ir'), os.path.join(build, 'kernel.spv'),
              os.path.join(build, 'kernel.spvasm'), os.path.join(build, 'kernel-aarch64.s'),
              os.path.join(build, 'check.log'), os.path.join(build, 'case-spirv-parity.tsv'),
              os.path.join(build, 'kernel.compile.err'), os.path.join(build, 'kernel.translate.err')]
    for path in copies:
        if os.path.exists(path) and os.path.getsize(path):
            shutil.copy(path, os.path.join(receipt_dir, os.path.basename(path)))
    with open(os.path.join(receipt_dir, 'results.tsv'), 'w', encoding='utf-8') as handle:
        handle.write('case\tfn\targs\tsource\treference\taarch64\tspirv_pocl_cpu\tverdict\n')
        for c in cases:
            handle.write(f"{c['case']}\t{c['fn']}\t{c['args']}\t{c['source']}\t{c['reference']}\t"
                         f"{c['aarch64']}\t{c['spirv_pocl_cpu']}\t{c['verdict']}\n")

    def hashed(path):
        return sha256(path) if os.path.exists(path) else None

    runtime = None
    err = os.path.join(build, 'case-1.spirv.err')
    if os.path.exists(err):
        runtime = ' '.join(open(err, encoding='utf-8', errors='replace').read().split())
    env = os.environ.get
    run_url = None
    if env('GITHUB_RUN_ID'):
        run_url = f"{env('GITHUB_SERVER_URL')}/{env('GITHUB_REPOSITORY')}/actions/runs/{env('GITHUB_RUN_ID')}"
    receipt = {
        'schema': 'omega-kernel-receipt/1',
        'verdict': verdict, 'stage': stage, 'message': message,
        'created_utc': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'where': {
            'repository': env('GITHUB_REPOSITORY', 'local'), 'commit': env('GITHUB_SHA'),
            'ref': env('GITHUB_REF_NAME'), 'run_url': run_url,
            'runner': f"{env('RUNNER_OS', '')} {env('RUNNER_ARCH', '')} {env('ImageOS', '')} {env('ImageVersion', '')}".strip() or None,
            'platform': ' '.join(platform.uname()[:3] + platform.uname()[4:5]),
        },
        'what_ran': {
            'aarch64': 'omc-compiled native AArch64 Linux programs, one per case, on this CPU',
            'spirv': 'Omega irspirv.om translated OSC IR to a SPIR-V 1.0 OpenCL kernel; '
                     'spirv-val checked it; PoCL ran it on its CPU device (one work-item per case)',
            'reference': 'reference.py in CPython on this CPU',
            'not_run': NOT_RUN,
        },
        'pins': {k: v for k, v in lock.items()},
        'built': {
            'irspirv': hashed(os.path.join(build, 'irspirv')),
            'opencl-host': hashed(os.path.join(build, 'opencl-host')),
            'pocl-library': hashed(os.environ.get('POCL_LIBRARY', os.path.join('.okt', 'pocl', lock.get('POCL_LIB', '')))),
        },
        'inputs': {name: hashed(name) for name in ('kernel.om', 'cases.tsv', 'reference.py')},
        'outputs': {name: hashed(os.path.join(build, name)) for name in ('kernel.ir', 'kernel.spv')},
        'opencl_runtime': runtime,
        'versions': versions(),
        'counts': {
            'cases': len(cases),
            'aarch64_matches_reference': sum(c['aarch64_matches_reference'] for c in cases),
            'spirv_matches_aarch64': sum(c['spirv_matches_aarch64'] for c in cases),
            'failed': len(failed),
        },
        'failures': failed[:50],
    }
    receipt_path = os.path.join(receipt_dir, 'receipt.json')
    with open(receipt_path, 'w', encoding='utf-8') as handle:
        json.dump(receipt, handle, indent=2)
        handle.write('\n')
    sums = []
    for name in sorted(os.listdir(receipt_dir)):
        if name != 'SHA256SUMS':
            sums.append(f'{sha256(os.path.join(receipt_dir, name))}  {name}')
    with open(os.path.join(receipt_dir, 'SHA256SUMS'), 'w', encoding='ascii') as handle:
        handle.write('\n'.join(sums) + '\n')

    n = len(cases)
    lines = [f'## Omega kernel check: {verdict}', '', f'**{stage}**: {message}', '']
    if cases:
        lines += ['| check | result |', '|---|---|',
                  f"| AArch64 native == reference.py | {receipt['counts']['aarch64_matches_reference']}/{n} |",
                  f"| SPIR-V on PoCL CPU == AArch64, bit for bit | {receipt['counts']['spirv_matches_aarch64']}/{n} |", '']
    if failed:
        lines += ['### First differing cases', '',
                  '| case | call | reference.py | AArch64 | SPIR-V (PoCL CPU) |', '|---|---|---|---|---|']
        for c in failed[:20]:
            lines.append(f"| {c['case']} ({c['source']}) | `{c['fn']} {c['args']}` | {c['reference']} | "
                         f"{c['aarch64']} | {c['spirv_pocl_cpu']} |")
        lines.append('')
    lines += ['### What ran where', '',
              '- AArch64: `omc` compiled each case to a native Linux program; it ran on this runner CPU.',
              '- SPIR-V: the Omega translator `irspirv.om` emitted a SPIR-V OpenCL kernel, `spirv-val` checked it, '
              'and PoCL ran it on its **CPU** device.',
              '- Not run: ' + '; '.join(NOT_RUN) + '. This is CPU evidence, not GPU qualification.', '',
              '### Pins', '', '| item | value |', '|---|---|',
              f"| omega-mv | `{lock.get('OMEGA_MV_COMMIT')}` |",
              f"| omc sha256 | `{lock.get('OMC_SHA256')}` |",
              f"| irspirv.om sha256 | `{lock.get('IRSPIRV_OM_SHA256')}` |",
              f"| PoCL asset sha256 | `{lock.get('POCL_SHA256')}` |",
              f"| kernel.om sha256 | `{receipt['inputs']['kernel.om']}` |",
              f"| kernel.spv sha256 | `{receipt['outputs']['kernel.spv']}` |",
              f"| receipt.json sha256 | `{sha256(receipt_path)}` |", '',
              'Receipt: the `omega-kernel-receipt` artifact of this run (receipt.json, results.tsv, '
              'kernel.spv, kernel.spvasm, kernel-aarch64.s, SHA256SUMS).', '']
    with open(os.path.join(build, 'summary.md'), 'w', encoding='utf-8') as handle:
        handle.write('\n'.join(lines))
    print(f'{verdict} {stage}: {message}')
    print(f'receipt {receipt_path} sha256={sha256(receipt_path)}')
    return 0 if verdict == 'PASS' else 1


def main(argv):
    if len(argv) != 3 or argv[1] not in ('expand', 'report'):
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    try:
        if argv[1] == 'expand':
            expand(argv[2])
            return 0
        return report(argv[2])
    except Bad as error:
        print(f'FAIL {error}' if argv[1] == 'report' else str(error))
        return 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))
