#!/bin/sh
# Build the self-contained matchbiotmp ELF.
#
# The ZKFinger SDK shared libraries are embedded into the binary with objcopy.
# They are committed under build/ so the build needs nothing but gcc + objcopy
# (no external SDK distribution).
set -e

here=$(cd "$(dirname "$0")" && pwd)
srcdir="$here/build"
out="$here/matchbiotmp"

libs="libusb-1.0.so.0 libusb-0.1.so.4 libcrypto.so.0.9.8 libsqlite3.so.0 \
      libiomp5.so libidkit.so.2 libsilkidcap.so libzkfinger10.so libzkfp.so"

objs=""
for f in $libs; do
    if [ ! -f "$srcdir/$f" ]; then
        echo "error: missing $srcdir/$f" >&2
        exit 1
    fi
    echo "embed: $f  <-  $srcdir/$f"
    ( cd "$srcdir" && objcopy -I binary -O elf64-x86-64 -B i386:x86-64 "$f" "$f.o" )
    objs="$objs $srcdir/$f.o"
done

gcc -O2 -Wall -Wextra -o "$out" "$here/src/matchbiotmp.c" $objs -ldl

echo "built: $out"
