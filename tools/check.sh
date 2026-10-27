#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Check kernel.om three ways on one AArch64 Linux CPU:
#   1. AArch64: the pinned omc compiles every case to a native program and runs it.
#   2. SPIR-V:  omc emits OSC IR, the Omega translator irspirv.om (compiled by the same
#               omc) turns it into a SPIR-V OpenCL kernel module, spirv-val checks it, and
#               PoCL runs it on the CPU device. No GPU is involved anywhere.
#   3. Reference: reference.py computes the expected value of every case.
# A case passes only when AArch64 equals the reference and SPIR-V equals AArch64 bit for bit.
# Exit 0 PASS, 1 FAIL, 3 NOT_RUN (required tool or platform missing).
# Everything is written under build/; build/receipt/ is the uploaded receipt.
# INTERIM host glue (POSIX sh). Successor: an Omega conductor unit in the style of
# omega-mv seed/qualification/om2/gate.om, once it can spawn the OpenCL host per case.
set -eu
cd "$(dirname "$0")/.."
. ./toolchain.lock
B=build
R=$B/receipt
T=${OKT_TOOLS:-.okt/tools}
PYTHON=${PYTHON:-python3}
CC=${CC:-cc}
SPIRV_VAL=${SPIRV_VAL:-spirv-val}
SPIRV_DIS=${SPIRV_DIS:-spirv-dis}
POCL_LIBRARY=${POCL_LIBRARY:-$PWD/.okt/pocl/$POCL_LIB}
rm -rf "$B"
mkdir -p "$R"
: > "$B/check.log"
say() { printf '%s\n' "$*" | tee -a "$B/check.log"; }
report() { "$PYTHON" tools/cases.py report "$B" || return $?; }
stop() {
    # stop VERDICT STAGE MESSAGE: record why the check ended early, then write the receipt.
    printf '%s\t%s\t%s\n' "$1" "$2" "$3" > "$B/stop.tsv"
    say "$1 $2: $3"
    set +e
    report
    rc=$?
    set -e
    [ "$1" = NOT_RUN ] && exit 3
    [ "$rc" = 0 ] && exit 1
    exit "$rc"
}

command -v "$PYTHON" > /dev/null || { echo "NOT_RUN requires $PYTHON" >&2; exit 3; }
[ "$(uname -s)-$(uname -m)" = Linux-aarch64 ] || stop NOT_RUN platform "requires AArch64 Linux, found $(uname -sm)"
command -v "$CC" > /dev/null || stop NOT_RUN toolchain "requires a C compiler ($CC) for the OpenCL host"
command -v "$SPIRV_VAL" > /dev/null || stop NOT_RUN toolchain "requires spirv-val ($SPIRV_VAL)"
case "$POCL_LIBRARY" in /*/libpocl.so*) ;; *) stop NOT_RUN toolchain "POCL_LIBRARY must be an absolute libpocl.so path" ;; esac
[ -f "$POCL_LIBRARY" ] || stop NOT_RUN toolchain "PoCL library $POCL_LIBRARY missing; run sh tools/fetch.sh"

pinned() {
    [ -f "$1" ] || stop NOT_RUN toolchain "$1 missing; run sh tools/fetch.sh"
    [ "$(sha256sum "$1" | cut -d ' ' -f 1)" = "$2" ] || stop FAIL toolchain "$1 differs from its pin in toolchain.lock"
}
pinned "$T/omc" "$OMC_SHA256"
pinned "$T/irspirv.om" "$IRSPIRV_OM_SHA256"
pinned "$T/opencl-host.c" "$OPENCL_HOST_C_SHA256"
pinned "$T/parity.py" "$PARITY_PY_SHA256"
OMC=$T/omc
say "PINS omega-mv@$OMEGA_MV_COMMIT omc=$OMC_SHA256 pocl=$POCL_LIBRARY"
say "PLATFORM $(uname -srm)"

# Toolchain built from pinned sources: the Omega translator and the OpenCL CPU host.
"$OMC" < "$T/irspirv.om" > "$B/irspirv" 2> "$B/irspirv.compile.err" || stop FAIL toolchain 'omc failed to compile irspirv.om'
[ ! -s "$B/irspirv.compile.err" ] || stop FAIL toolchain 'omc wrote stderr while compiling irspirv.om'
chmod +x "$B/irspirv"
# shellcheck disable=SC2086 # flag lists are intentionally word-split
"$CC" -std=c11 -O2 -Wall -Wextra ${OPENCL_CFLAGS:-} "-DIRSPIRV_POCL_LIBRARY=\"$POCL_LIBRARY\"" \
    "$T/opencl-host.c" -o "$B/opencl-host" ${OPENCL_LIBS:--lOpenCL} > "$B/opencl-host.build.log" 2>&1 ||
    { cat "$B/opencl-host.build.log"; stop NOT_RUN toolchain 'OpenCL host build failed (is ocl-icd-opencl-dev installed?)'; }

# 1. Kernel to OSC IR, then to SPIR-V, then validation.
[ -f kernel.om ] || stop FAIL input 'kernel.om missing'
{ printf 'ir\n'; cat kernel.om; } > "$B/kernel.input"
explain() {
    # omc reports "omc: line N: error C" for its input; kernel.om line = N - $1 (prepended lines).
    line=$(sed -n 's/.*line \([0-9][0-9]*\): error \([0-9][0-9]*\).*/\1 \2/p' "$2" | head -n 1)
    [ -n "$line" ] || { tr '\n' ' ' < "$2"; return; }
    set -- "$1" $line
    case $3 in
        3) why='unknown name or word not available here' ;; 4) why='line ends early' ;;
        5) why='extra words on the line' ;; 6) why='bad number' ;; 7) why='type mismatch' ;;
        8) why='value out of range' ;; 9) why='word not allowed here (IR mode refuses struct, global, text, floats, host words)' ;;
        10) why='duplicate name' ;; 11) why='ambiguous literal: give the type, e.g. uint 1' ;;
        12) why='register pool exhausted' ;; 13) why='variable name longer than 8 bytes' ;;
        14) why='capacity' ;; 15) why='missing end' ;; 16) why='syntax' ;; 17) why='line after a path end' ;;
        18) why='missing return' ;; 19) why='aliasing' ;; 20) why='readonly' ;; 21) why='constant index' ;;
        22) why='text literal' ;; 23) why='size capacity' ;; *) why='see omega-mv seed/README.md R30' ;;
    esac
    printf 'kernel.om line %s: error %s (%s)' "$(($2 - $1))" "$3" "$why"
}
if ! "$OMC" < "$B/kernel.input" > "$B/kernel.ir" 2> "$B/kernel.compile.err"; then
    stop FAIL compile "omc refused $(explain 1 "$B/kernel.compile.err")"
fi
if ! "$B/irspirv" < "$B/kernel.ir" > "$B/kernel.spv" 2> "$B/kernel.translate.err"; then
    stop FAIL spirv "irspirv refused the kernel IR: $(tr '\n' ' ' < "$B/kernel.translate.err")"
fi
[ ! -s "$B/kernel.translate.err" ] || stop FAIL spirv 'irspirv wrote stderr'
"$B/irspirv" < "$B/kernel.ir" > "$B/kernel.again.spv"
cmp -s "$B/kernel.spv" "$B/kernel.again.spv" || stop FAIL spirv 'irspirv output is not deterministic'
"$SPIRV_VAL" --target-env opencl2.1 "$B/kernel.spv" > "$B/spirv-val.log" 2>&1 ||
    { cat "$B/spirv-val.log"; stop FAIL spirv 'spirv-val rejected kernel.spv'; }
if command -v "$SPIRV_DIS" > /dev/null; then "$SPIRV_DIS" "$B/kernel.spv" -o "$B/kernel.spvasm"; fi
say "PASS kernel.om -> kernel.ir -> kernel.spv (deterministic, spirv-val opencl2.1)"

# 2. Cases: cases.tsv plus deterministic edge and random inputs, with reference values.
"$PYTHON" tools/cases.py expand "$B" > "$B/expand.log" 2>&1 ||
    { cat "$B/expand.log"; stop FAIL cases "$(tail -n 1 "$B/expand.log")"; }
cat "$B/expand.log" | tee -a "$B/check.log"

# 3. AArch64: one native program per case, so a trap in one case cannot hide another.
tab=$(printf '\t')
: > "$B/parity-table.tsv"
while IFS="$tab" read -r i fn ret typed omargs expect_bits expect_status src; do
    [ "$omargs" != - ] || omargs=
    case_om=$B/case-$i.om
    cat kernel.om > "$case_om"
    cat >> "$case_om" <<'DUMP'
global dumpbuf uchar 8
fn ir_dump x ulong to void
for j ulong 8
put dumpbuf j uchar and shr x mul j 8 255
end
p = ulong 0
while lt p 8 bound 8
k = write 1 dumpbuf p sub ulong 8 p
if le k 0
exit 90
end
p = add p ulong k
end
end
fn main to int
DUMP
    case "$ret" in
        bool) printf 'if %s %s\nir_dump ulong 1\nelse\nir_dump ulong 0\nend\n' "$fn" "$omargs" ;;
        void) printf '%s %s\nir_dump ulong 0\n' "$fn" "$omargs" ;;
        byte|short|int|long) printf 'ir_dump bitcast ulong long %s %s\n' "$fn" "$omargs" ;;
        *) printf 'ir_dump ulong %s %s\n' "$fn" "$omargs" ;;
    esac >> "$case_om"
    printf 'ret 0\nend\n' >> "$case_om"
    "$OMC" < "$case_om" > "$B/case-$i.native" 2> "$B/case-$i.compile.err" ||
        stop FAIL aarch64 "case $i ($fn $omargs): omc refused the native case program: $(tr '\n' ' ' < "$B/case-$i.compile.err")"
    chmod +x "$B/case-$i.native"
    set +e
    timeout 10 "$B/case-$i.native" > "$B/case-$i.out"
    rc=$?
    set -e
    printf '%s\n' "$rc" > "$B/case-$i.status"
    printf 'kernel\t%s\t%s\t%s\t%s\n' "$fn" "$ret" "$typed" "$rc" >> "$B/parity-table.tsv"
done < "$B/plan.tsv"
say "PASS AArch64 native programs ran for $(wc -l < "$B/plan.tsv") cases"
objdump -d "$B/case-1.native" > "$B/kernel-aarch64.s" 2>/dev/null || :

# 4. SPIR-V on PoCL CPU: pinned omega-mv parity.py compares every case bit for bit with AArch64.
set +e
"$PYTHON" "$T/parity.py" "$B" "$B/parity-table.tsv" "$B/opencl-host" case > "$B/parity.log" 2>&1
rc=$?
set -e
tee -a "$B/check.log" < "$B/parity.log"
[ "$rc" = 0 ] || [ "$rc" = 1 ] || stop FAIL spirv-run "parity.py could not run every case (exit $rc): $(tail -n 1 "$B/parity.log")"

# 5. Verdict against reference.py, job summary and receipt.
report
