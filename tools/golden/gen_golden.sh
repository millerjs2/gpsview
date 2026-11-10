#!/usr/bin/env bash
# Regenerate tests/golden_fmt.tsv from the real gpsd / cgps C code.
#
#   tools/golden/gen_golden.sh [path-to-gpsd-source]
#
# The functions are copied out of the gpsd tree with awk (so the C that is
# tested is the C that ships), compiled with gcc, and run on fixed inputs.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
GPSD="${1:-${GPSD_SRC:-$HOME/dev/gpsd}}"
GEN="$(mktemp -d)"
trap 'rm -rf "$GEN"' EXIT

# Copy one C function: from the line starting with the literal text $2 (in file $1)
# to the first line that is exactly "}".
extract() {
    awk -v pat="$2" '
        index($0, pat) == 1 { on = 1 }
        on { print }
        on && $0 == "}" { exit }
    ' "$1"
}

{
    extract "$GPSD/libgps/gpsdclient.c" 'char *deg_to_str2('
    echo
    extract "$GPSD/libgps/gpsdclient.c" 'const char *maidenhead('
    echo
    extract "$GPSD/clients/cgps.c" 'static const char *int_to_str('
    echo
    extract "$GPSD/clients/cgps.c" 'static const char *tenth_to_str('
    echo
    extract "$GPSD/clients/cgps.c" 'static char *dop_to_str('
    echo
    extract "$GPSD/clients/cgps.c" 'static char *ep_to_str('
    echo
    extract "$GPSD/clients/cgps.c" 'static char *ecef_to_str('
} > "$GEN/extracted.c"

awk -v f="$GEN/extracted.c" '
    /@@BEGIN_EXTRACTED@@/ { print; while ((getline line < f) > 0) print line; skip = 1; next }
    /@@END_EXTRACTED@@/   { skip = 0 }
    !skip { print }
' "$HERE/harness.c" > "$GEN/harness.c"

gcc -std=gnu11 -O0 -Wall -Wno-unused-function -o "$GEN/harness" "$GEN/harness.c" -lm
"$GEN/harness" > "$ROOT/tests/golden_fmt.tsv"
echo "wrote $(wc -l < "$ROOT/tests/golden_fmt.tsv") cases to tests/golden_fmt.tsv"
