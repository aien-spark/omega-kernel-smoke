#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Fetch the pinned toolchain named in toolchain.lock into .okt/ and refuse any file
# whose sha256 differs. Sources are GitHub only: raw files of aien-dev/omega-mv at an
# exact commit, and the PoCL release asset of this template repository.
# INTERIM host glue (POSIX sh + curl). Successor: an Omega fetch-and-verify unit once
# seeded Omega has an HTTPS client word; until then curl moves bytes and sha256sum,
# not curl, decides what is accepted.
# usage: sh tools/fetch.sh [all|omega|pocl]
set -eu
cd "$(dirname "$0")/.."
. ./toolchain.lock
what=${1:-all}
D=.okt
mkdir -p "$D/tools"

get() {
    url=$1; out=$2; want=$3
    if [ -f "$out" ] && [ "$(sha256sum "$out" | cut -d ' ' -f 1)" = "$want" ]; then
        echo "fetch: cached $want $out"
        return 0
    fi
    curl -fsSL --retry 3 -o "$out.part" "$url"
    got=$(sha256sum "$out.part" | cut -d ' ' -f 1)
    if [ "$got" != "$want" ]; then
        rm -f "$out.part"
        echo "fetch: REFUSED $url sha256 $got, pinned $want" >&2
        exit 1
    fi
    mv "$out.part" "$out"
    echo "fetch: OK $want $out"
}

if [ "$what" = all ] || [ "$what" = omega ]; then
    raw=https://raw.githubusercontent.com/$OMEGA_MV_REPO/$OMEGA_MV_COMMIT
    get "$raw/seed/out/omc" "$D/tools/omc" "$OMC_SHA256"
    chmod +x "$D/tools/omc"
    get "$raw/seed/spirv/irspirv.om" "$D/tools/irspirv.om" "$IRSPIRV_OM_SHA256"
    get "$raw/seed/spirv/opencl-host.c" "$D/tools/opencl-host.c" "$OPENCL_HOST_C_SHA256"
    get "$raw/seed/spirv/parity.py" "$D/tools/parity.py" "$PARITY_PY_SHA256"
fi

if [ "$what" = all ] || [ "$what" = pocl ]; then
    [ "$POCL_SHA256" != PENDING ] || { echo 'fetch: PoCL asset not yet published (POCL_SHA256=PENDING)' >&2; exit 3; }
    get "https://github.com/$POCL_REPO/releases/download/$POCL_TAG/$POCL_ASSET" \
        "$D/$POCL_ASSET" "$POCL_SHA256"
    rm -rf "$D/pocl"
    mkdir -p "$D/pocl"
    tar -xzf "$D/$POCL_ASSET" -C "$D/pocl" --strip-components=1
    [ -f "$D/pocl/$POCL_LIB" ] || { echo "fetch: $POCL_LIB missing from $POCL_ASSET" >&2; exit 1; }
    echo "fetch: PoCL unpacked at $D/pocl"
fi
