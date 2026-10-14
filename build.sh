#!/bin/sh
# Build the self-contained matchbiotmp ELF.
# The ZKFinger SDK shared libraries are embedded into the binary with objcopy.
set -e

here=$(cd "$(dirname "$0")" && pwd)
libdir="$here/ZKTecoSDK/SDK/lib-x64"
out="$here/matchbiotmp"
build="$here/build"

if [ ! -d "$libdir" ]; then
    echo "error: SDK libraries not found at $libdir" >&2
    exit 1
fi

mkdir -p "$build"

# name:search-dir (libusb-1.0 is not shipped by the SDK, use the system one)
find_lib() {
    name="$1"
    if [ -f "$libdir/$name" ]; then printf '%s\n' "$libdir/$name"; return 0; fi
    for d in /usr/lib/x86_64-linux-gnu /usr/lib64 /usr/lib /lib/x86_64-linux-gnu /lib64 /lib; do
        if [ -f "$d/$name" ]; then printf '%s\n' "$d/$name"; return 0; fi
    done
    return 1
}

libs="libusb-1.0.so.0 libusb-0.1.so.4 libcrypto.so.0.9.8 libsqlite3.so.0 \
      libiomp5.so libidkit.so.2 libsilkidcap.so libzkfinger10.so libzkfp.so"

objs=""
for f in $libs; do
    src=$(find_lib "$f") || { echo "error: cannot locate $f" >&2; exit 1; }
    echo "embed: $f  <-  $src"
    cp "$src" "$build/$f"
    ( cd "$build" && objcopy -I binary -O elf64-x86-64 -B i386:x86-64 "$f" "$f.o" )
    objs="$objs $build/$f.o"
done

gcc -O2 -Wall -Wextra -o "$out" "$here/src/matchbiotmp.c" $objs -ldl

echo "built: $out"
