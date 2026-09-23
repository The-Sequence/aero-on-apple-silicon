#!/bin/bash
#
# Build everything from source instead of using the release runtime:
#   - QEMU 9.2.2 with the vmvga device and all the macOS fixes
#   - DXVK (with dxbc-spirv) for macOS, as dxvk-native dylibs
#   - the WSI shim that lets DXVK talk to QEMU's SDL window
#   - the guest auto-resize agent (needs mingw-w64; optional, a prebuilt
#     copy is already in guest/QemuResAgent/)
#
# Everything lands in ../runtime, which is what the launchers use.
#
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="${WORK:-$ROOT/build/work}"
RUNTIME="$ROOT/runtime"
JOBS="$(sysctl -n hw.perflevel0.logicalcpu 2>/dev/null || sysctl -n hw.ncpu)"

QEMU_VER=9.2.2
QEMU_TARBALL_URL="https://download.qemu.org/qemu-$QEMU_VER.tar.xz"
DXVK_REPO="https://github.com/doitsujin/dxvk.git"
DXVK_COMMIT="40e01640396d03f9fddeb697f5ee00ae893ecb75"
DXBC_SPIRV_COMMIT="bf14419e5fa7eacb817b7b632f03cb61d61bbad7"

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

command -v brew >/dev/null || die "Homebrew is required."
say "Build dependencies"
brew install meson ninja pkgconf glib pixman sdl2-compat gnutls libpng jpeg-turbo \
             zstd libslirp libusb molten-vk vulkan-loader python@3 || true

mkdir -p "$WORK" "$RUNTIME/bin" "$RUNTIME/lib"

# ----------------------------------------------------------------- QEMU
say "QEMU $QEMU_VER"
cd "$WORK"
[ -f "qemu-$QEMU_VER.tar.xz" ] || curl -fL --progress-bar "$QEMU_TARBALL_URL" -o "qemu-$QEMU_VER.tar.xz"
if [ ! -d "qemu-$QEMU_VER" ]; then
    tar xf "qemu-$QEMU_VER.tar.xz"
    say "Applying the vmvga + macOS patch"
    ( cd "qemu-$QEMU_VER" && patch -p1 < "$ROOT/patches/qemu-9.2.2-aero.patch" )
fi
if [ ! -f "$WORK/qemu-build/build.ninja" ]; then
    mkdir -p "$WORK/qemu-build"
    ( cd "$WORK/qemu-build" && "$WORK/qemu-$QEMU_VER/configure" \
        --extra-cflags="-DQEMU_VERSION_MAJOR=9 -ffile-prefix-map=$WORK=." \
        --target-list=x86_64-softmmu \
        --enable-sdl --enable-cocoa --enable-slirp --enable-hvf --disable-werror )
fi
ninja -C "$WORK/qemu-build" qemu-system-x86_64 qemu-img
cp "$WORK/qemu-build/qemu-system-x86_64" "$WORK/qemu-build/qemu-img" "$RUNTIME/bin/"

# ----------------------------------------------------------------- DXVK
say "DXVK (macOS / dxvk-native)"
if [ ! -d "$WORK/dxvk" ]; then
    git clone --recursive "$DXVK_REPO" "$WORK/dxvk"
    ( cd "$WORK/dxvk" && git checkout "$DXVK_COMMIT" && git submodule update --init --recursive )
    ( cd "$WORK/dxvk/subprojects/dxbc-spirv" && git checkout "$DXBC_SPIRV_COMMIT" )
    say "Applying the macOS/MoltenVK patches"
    ( cd "$WORK/dxvk" && patch -p1 < "$ROOT/patches/dxvk-macos.patch" )
    ( cd "$WORK/dxvk/subprojects/dxbc-spirv" && patch -p1 < "$ROOT/patches/dxbc-spirv-dref.patch" )
    ( cd "$WORK/dxvk/subprojects/libdisplay-info" && patch -p1 < "$ROOT/patches/libdisplay-info-macos.patch" ) || true
fi
if [ ! -f "$WORK/dxvk/build-native/build.ninja" ]; then
    ( cd "$WORK/dxvk" && meson setup build-native \
        --buildtype release -Dnative_sdl2=enabled -Dnative_glfw=disabled \
        -Dc_args=-I"$(brew --prefix)/include" -Dcpp_args=-I"$(brew --prefix)/include" )
fi
ninja -C "$WORK/dxvk/build-native"
# Only D3D9 is used (the Windows 7/Vista VMware driver speaks D3D9-class
# SVGA3D). Shipping d3d11/dxgi as well makes QEMU initialise a second DXVK
# instance for nothing.
cp "$WORK/dxvk/build-native/src/d3d9/libdxvk_d3d9.0.dylib" "$RUNTIME/lib/"

# ------------------------------------------------------------- WSI shim
say "WSI shim"
clang -O2 -dynamiclib -o "$RUNTIME/lib/libvmsvga3d_wsi.dylib" \
      -install_name @rpath/libvmsvga3d_wsi.dylib \
      "$ROOT/src/wsishim/vmsvga3d_wsi_shim.c"

codesign -f -s - "$RUNTIME/lib/"*.dylib "$RUNTIME/bin/qemu-system-x86_64" 2>/dev/null || true

# ------------------------------------------------------- guest agent (opt)
if command -v x86_64-w64-mingw32-gcc >/dev/null 2>&1; then
    say "Guest auto-resize agent"
    x86_64-w64-mingw32-gcc -O2 -s -Wall -mwindows -nostdlib -ffreestanding \
        -fno-tree-loop-distribute-patterns -e AgentStartup \
        -o "$ROOT/guest/QemuResAgent/qemu-res-agent.exe" \
        "$ROOT/src/guest-agent/qemu-res-agent.c" -lkernel32 -luser32 -lgcc
else
    echo "  (skipping the guest agent: mingw-w64 not installed; the prebuilt one is used)"
fi

say "Done - runtime/ is ready"
echo "Now run Setup.command to build guest-tools.iso, then the Start-*.command launchers."
